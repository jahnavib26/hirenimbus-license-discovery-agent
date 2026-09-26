# HireNimbus License Discovery Agent — Day 1

Day 1 resolves a messy U.S. phone number to an explainable business identity.
It deliberately does **not** implement contractor-license discovery, state-board
adapters, persistence, or an HTTP service.

## Setup

Python 3.12 is required.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
# Edit .env and set GOOGLE_PLACES_API_KEY to your restricted key.
```

Enable **Places API (New)** for the Google Cloud project associated with the
key. Keep the key out of source control and restrict it in Google Cloud. The CLI
loads `.env` from the current working directory without overriding an already
exported environment variable. `.env` is ignored by Git.

## CLI

After installation, use the required command:

```bash
lookup_identity "(512) 555-1234"
```

The module form is equivalent:

```bash
python -m app.cli lookup-identity "(512) 555-1234"
```

Both write JSON containing `found`, qualitative `confidence`, normalized
`input_phone`, `identity`, source `evidence`, resolver `assessments`, `notes`, and
`error` (`null` for non-errors). Exit codes are `0` for a completed lookup
(including a legitimate no-result), `1` for configuration or provider failure,
and `2` for invalid input.

## Day 1 approach

1. A narrow syntax check rejects malformed/junk-bearing input before
   `phonenumbers` parses and validates it as a U.S. number. The base is formatted
   as `+1XXXXXXXXXX`; an extension is preserved separately.
2. Invalid input and the supplied reserved fictional `555-01xx` cases return
   immediately, before any provider call.
3. Places API (New) Text Search receives the E.164 number, `regionCode=US`, and
   `includePureServiceAreaBusinesses=true`. Search requests only Place IDs.
4. The resolver fetches a deliberately limited identity field set from Place
   Details for each candidate.
5. At least one returned national or international base phone must normalize to
   the input base using the same function. Search order is never treated as
   verification, and conflicting returned phones veto a match.
6. The resolver either selects the only exact-phone candidate or leaves the
   result unresolved. It preserves provider facts and their provenance.
7. Raw Google types are preserved. Confident matches against
   `data/category_taxonomy.json` populate separate normalized category IDs.

Google currently has no separate phone-search method in Places API (New).
Phone lookup is documented as a Text Search using a country-code-prefixed query,
followed here by Place Details verification. The implementation uses the current
`places:searchText` and `places/{place_id}` endpoints, required field masks,
`pageSize` rather than deprecated `maxResultCount`, `pureServiceAreaBusiness`,
and `movedPlaceId`.

Only identity-relevant fields are requested: Place ID, display name, phones,
address/components, business status, website URI, types, service-area status,
moved Place ID, and Google Maps URI. Reviews, photos, ratings, and hours are not
requested.

## Confidence rules

Confidence is deterministic and intentionally qualitative:

| Outcome | Result |
| --- | --- |
| One exact-phone Google candidate with a business name and no meaningful conflicts | `found=true`, `medium` |
| Medium conditions plus meaningful confirmation from a trustworthy independent source | `found=true`, `high` |
| No exact phone, more than one exact-phone listing, or exact phone with no business name | `found=false`, `low` |

No independent corroboration source is integrated in Day 1, so the current CLI
does not emit `high`. Address, website URI, and category fields repeated within
the same Google record are not treated as independent corroboration.

Closed and moved listings can still be identified; their status and moved Place
ID remain visible. A pure service-area listing is not penalized for hiding its
street address. Missing fields remain `null`/empty. Multiple exact matches are
ambiguous even if one ranks first.

The Google display name is stored as `business_name`, never as `legal_name` or
`dba`. Legal name, DBA, and owner/principal stay `null` because Places does not
establish them.

## Evidence and errors

Every inspected Place Details record produces a `google_places_details`
evidence item with the Place ID, Maps URL when supplied, and only observed
claims. Raw and normalized returned phone values may both appear, but exact
match/mismatch/conflict is resolver inference and therefore lives in a separate
candidate assessment. A nonmatching candidate remains evidence of candidate
evaluation, not evidence of the resolved identity.

These outcomes remain distinct:

- invalid input: `error.kind=invalid_input`, no provider call;
- valid no-result: `found=false`, no error;
- ambiguous or unverified candidates: `found=false`, `confidence=low`, evidence retained;
- network/API/response failure: `error.kind=lookup_failure`, not disguised as no-result.

## Tests

```bash
pytest
```

Unit tests use an injected fake Places client or `httpx.MockTransport`; normal
tests never call Google. The complete 28-row `data/phones.csv` normalization
harness is part of the suite. Coverage includes normalization/equivalence,
extension preservation, malformed-input rejection, distinct phone preservation, invalid
input and call prevention, exact returned-phone verification, Google-only medium
confidence, ambiguity, no-result, provider failure, closed/moved and service-area
businesses, missing fields, provenance/inference separation, explicit category
mapping, the HTTP request contract, and CLI failures.

## Assumptions and known limitations

- The supplied taxonomy is used conservatively: known aliases map to its stable
  category IDs, while unknown Google types remain raw-only rather than becoming
  `other` automatically.
- The search inspects the first page of up to 20 results. It does not request
  subsequent pages; exact phone queries are expected to be narrow, but this is a
  documented recall/cost boundary.
- Website URLs are retained as Google Places facts; this version does not fetch
  or validate website content. No state registry is queried.
- There is no independent corroboration source, so `high` confidence is
  intentionally unavailable. A fraudulent but internally coherent Places
  listing cannot be detected reliably by the current Day 1 flow.
- Tracking/forwarding/shared numbers surface as ambiguous only when they produce
  multiple exact-phone candidates. There is no external line-type database.
- Live API behavior and account configuration require a valid billable Google
  API key and are not exercised by unit tests.
