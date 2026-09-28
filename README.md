# HireNimbus License Discovery Agent

This project takes a messy U.S. business phone number and returns an evidence-backed business identity and relevant contractor or trade-license information.

The main design principle is simple: in a marketplace recommendation workflow, attaching the wrong identity or license is worse than returning an incomplete result. The pipeline therefore favors strong evidence over guessing.

## At a glance

| | |
|---|---|
| Input | U.S. business phone number |
| Output | Business identity + relevant license results + evidence |
| Identity sources | Google Places + first-party website verification |
| License sources | Official state licensing boards / open data |
| Interfaces | `lookup_identity` CLI + `find_licenses(phone)` |
| Tests | 177 passing |

## Setup

Python 3.12+ is required.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Enable **Places API (New)** in Google Cloud and add your key to `.env`:

```dotenv
GOOGLE_PLACES_API_KEY=replace-with-a-restricted-google-places-api-key
```

`.env` and runtime cache files are ignored by Git.

An optional `HIRENIMBUS_CACHE_PATH` can be used to override the default cache location:

```text
.cache/license_results.json
```

## Usage

### Identity lookup

```bash
lookup_identity "(512) 943-7070"
```

The CLI returns JSON containing the normalized phone, resolved identity when supported, confidence, evidence, candidate assessments, notes, and errors.

### Full license pipeline

```python
from app import find_licenses

result = find_licenses("(512) 943-7070")
print(result.model_dump_json(indent=2))
```

To force a fresh provider and board search:

```python
result = find_licenses("(512) 943-7070", refresh=True)
```

`find_licenses(phone)` is the thin tool interface for the full pipeline. The implementation uses the function option rather than a separate HTTP endpoint or full MCP server.

## Architecture

```mermaid
flowchart TD
    A[Phone Input] --> B[Normalize + Validate]
    B -->|Invalid| X[Validation Error]
    B -->|Valid| C[Google Places]

    C --> D{Exact Phone Match?}

    D -->|Yes| E[Resolved Identity]
    D -->|No| F[First-Party Website Verification]

    F -->|Verified| E
    F -->|Not Verified| Y[Unresolved Identity]

    E --> G[Select Relevant Licensing Boards]
    G --> H[Search Official Sources]
    H --> I[Normalize + Match License Candidates]
    I --> J[Evidence-Backed PipelineResult]

    H -->|Blocked / Unreachable| K[Partial Result with search_status]
    K --> J

    J --> L[Cache / Preserve Last Successful Result]
```

## Identity resolution

Phone numbers are parsed as U.S. numbers, validated, and normalized to E.164 (`+1XXXXXXXXXX`). Invalid or implausible numbers are rejected before external provider calls.

Google Places API (New) is the primary identity source:

1. Search using compact E.164 format.
2. If that returns zero candidates, retry once using a spaced country-code format such as `+1 7034779016`.
3. Retrieve Place Details.
4. Prefer a candidate whose returned phone exactly matches the normalized input.

The Google display name is stored as the public business name. It is not automatically treated as a legal name, DBA, or owner name.

### First-party website verification

Sometimes Google Places returns the correct business but lists a different location, office, or tracking phone.

In that case, the system may verify the candidate using only the first-party website returned by Google Places.

The candidate is accepted only when the page contains:

- the exact input phone;
- the same business identity; and
- the exact full Places address.

Exactly one candidate must satisfy the rule.

These matches remain `medium` confidence, and the original Places phone mismatch stays visible in the result.

The system does not perform general web search, crawl websites, or accept third-party directories as identity evidence.

## License discovery

After resolving the business identity, the system selects boards using the observed state and trade categories.

Board routing is implemented for the supplied Texas, Virginia, California, Maryland, and DC sources where safe automated access is available.

- Texas — TDLR and TSBPE
- Virginia — DPOR
- California — CSLB
- Maryland — available MHIC / electrical mappings
- District of Columbia — represented conservatively where the supplied source guidance is ambiguous

Official bulk downloads and open data are preferred where available.

CAPTCHAs, Cloudflare/WAF challenges, and other access protections are never bypassed.

Each accepted license preserves the board, license number, type/class, holder name, raw and normalized status, available dates, match confidence, evidence URL, and `fetched_at`. Missing values remain `null` rather than being inferred.

### Matching

A board result is first treated as a license candidate, not automatically as a license belonging to the business.

Matching considers:

- business name
- legal name / DBA when known
- owner or principal when known
- phone
- address
- trade/category consistency

Names are normalized for case, punctuation, whitespace, and common legal suffixes.

Fuzzy or partial name similarity alone is not enough to accept a license. A conflicting board phone can only be overridden when the business name and a full official address both match exactly. The phone conflict still remains visible in the result.

Active and inactive records are both eligible when the official source exposes them. Raw board status is preserved alongside normalized status.

## Board search statuses

Every selected board produces one of these outcomes:

| Status | Meaning |
|---|---|
| `ok` | Source was reached and returned candidate rows |
| `not_found` | Source was reached but returned no candidates for the search |
| `unreachable` | HTTP, download, parsing, or schema failure prevented a reliable search |
| `captcha_blocked` | Human CAPTCHA interaction was required |
| `skipped` | No safe or sufficiently specific automated source was available |

`not_found` does **not** mean the business is unlicensed.

It only means that the specific source and search produced no matching candidate rows.

## Caching

The full pipeline cache is keyed by normalized phone number, so formatting variants share the same entry.

Complete results may be cached.

A partial or failed refresh does not overwrite a previous successful result.

`refresh=True` always performs a fresh attempt while preserving the previous successful cached result if the new run is incomplete.

Board-source requests also use light in-process caching and polite request spacing.

## Evaluation

The supplied `data/phones.csv` contains 28 rows. Of those, 22 contain valid phone inputs. After normalization and deduplication, those represent 17 unique valid phones.

Manual verification is used only for evaluation and is never read by production matching.

### Results

| Metric | Result |
|---|---:|
| Identity coverage | **10/17 (58.8%)** |
| Identity accuracy | **8/8 judgeable predictions (100%)** |
| License precision | **1/1 judgeable accepted licenses (100%)** |
| License recall | **1/8 verified reference licenses (12.5%)** |

License recall is limited mainly by inaccessible official sources, historical records missing from current bulk downloads, and business-to-license relationships that could not be proven from runtime evidence without making unsupported assumptions.

Public Places and licensing-board data can change over time, so the checked-in evaluation report records the observed behavior and timestamps from the evaluation run.

## Example outputs

Three direct pipeline outputs are checked into `examples/`:

- `examples/p09_fox_service_company.json`
  - identity resolved
  - TDLR license `33423` accepted
  - overall result remains partial because another relevant board was unreachable

- `examples/p16_roto_rooter_partial.json`
  - identity resolved using first-party website verification
  - downstream CSLB source was unreachable

- `examples/p23_invalid_phone.json`
  - demonstrates invalid-input handling

The examples are direct `PipelineResult.model_dump_json(indent=2)` outputs and were not manually edited.

## Trade-offs and limitations

The system intentionally prefers precision over speculative recall.

Current limitations include:

- alternate, tracking, or location-specific business phone numbers;
- phones that return no Google Places candidate;
- service-area businesses with limited address information;
- differences between public business names, DBAs, legal entities, and owner-held licenses;
- multi-location or multi-state businesses;
- CAPTCHA/WAF restrictions and board outages;
- current-only official datasets that may omit historical licenses;
- changing source schemas and licensing requirements;
- common or similar business names that cannot be safely matched without stronger corroborating evidence;
- trades or localities where a statewide license may not be required or where licensing exemptions apply.

## With more time

I would focus on:

- broader first-party and state-registry identity discovery when Places returns no candidate;
- stronger legal-name, DBA, parent-company, and owner resolution;
- more resilient access to legitimate official bulk datasets;
- malformed-row-tolerant DPOR parsing with better diagnostics;
- canonical board-specific license formatting;
- richer multi-location and multi-state reasoning;
- a larger manually verified evaluation set.

## AI assistance

AI coding assistants were used for implementation support, test generation, and debugging.

I designed the overall approach and made the final engineering decisions, including:

- the phone → identity → license pipeline;
- conservative identity verification and exact-phone-first matching;
- first-party website verification;
- evidence and confidence thresholds;
- board routing and source selection;
- license matching and acceptance rules;
- conflict and failure handling;
- caching and preservation of the last successful result;
- evaluation methodology and metrics; and
- the final review of the implementation against the take-home requirements.

I also rejected approaches that relied on unsupported inference, weaker provenance, or unsafe assumptions.

Manual evaluation findings remain separate from production behavior. No case-specific evaluation labels, manually verified aliases, or known license numbers are hardcoded into runtime matching.

## Tests

Run the full suite with:

```bash
pytest
```

Current result:

```text
177 passed
```

The suite includes unit and integration tests for phone parsing, Google Places behavior, first-party website verification, board selection, source adapters, name normalization, license matching, status mapping, caching, pipeline behavior, evaluation calculations, and CLI behavior.

Mocks and fixtures are used where appropriate; the 177 tests are not 177 live API calls.
