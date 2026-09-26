# HireNimbus Take-Home — License Discovery Agent

**Candidate:** Jahnavi Bollineni  
**Focus:** Backend + algorithms (no frontend / UI polish)  
**Budget:** ~3 hours/day for 3 days (~9 hours total)  
**Due:** Monday, Sep 28, 2026, end of day ET  
**Submit:** Reply to the HireNimbus email thread with a git repo link (preferred) or zip.

---

## Context (a few lines)

HireNimbus is a home-services marketplace: homeowners find and book vetted pros (plumbers, electricians, HVAC, handymen, renovation) via [app.hirenimbus.com](https://app.hirenimbus.com), [pro.hirenimbus.com](https://pro.hirenimbus.com), a ChatGPT plugin, and an MCP server at [mcp.hirenimbus.com](https://mcp.hirenimbus.com). Live markets today: **DC Metro, SF Bay Area, Austin**.

Before ops calls a pro — or an AI agent recommends one — we need a **trustworthy, evidence-backed view of who they are and what licenses they hold** (active *and* inactive). Agents already scrape and assemble public presence; your exercise is the **phone → identity → license-search** spine of that pipeline.

---

## The problem

**Input:** a US business phone number (messy formatting OK).

**Output:** a structured JSON result (and a thin tool/API wrapper) that:

1. **Resolves identity** — Does this phone map to a real business? Legal name, DBAs, owner/principal if public, address or service area tip, state(s), confidence, evidence URLs.
2. **Finds trade/contractor licenses** — Across the boards relevant to that identity’s state(s): active, expired, suspended, revoked, inactive. Every hit must carry board, number, type/class, holder name, normalized status, dates, match confidence, evidence URL, `fetched_at`.
3. **Stays honest** — Never invent fields or license numbers. Missing stays `null`. Distinguish **no license found** from **search failed / board unreachable**. Prefer official sources; respect ToS and rate limits; **do not bypass CAPTCHAs or bot protections**.

This mirrors how we keep pro profiles truthful in production: provenance on every claim, conflicts surfaced rather than silently “fixed,” and incomplete runs that do not overwrite known-good results.

---

## Suggested 3-day plan (~3h each)

### Day 1 — Identity resolution (~3h)

- Normalize phones to **E.164** (`+1XXXXXXXXXX`); reject/flag invalid inputs.
- Resolve phone → business via **Google Places API** (Find Place from phone number / Text Search + Place Details) and optionally other public sources (business website, state corp registries: VA SCC, MD SDAT, DC CorpOnline / BOSS, CA SOS, TX SOS / Comptroller).
- Score **identity match confidence** (exact phone on Place, name+phone corroboration, closed/moved, multiple candidates, tracking/call-forwarding risk, service-area businesses with hidden street address).
- **CLI deliverable:** `lookup_identity <phone>` → JSON `{ found, confidence, identity, evidence[], notes }`.

### Day 2 — License search (~3h)

- Given the Day 1 identity, search the boards that apply for that market:
  - **DC Metro:** DC DLCP / BOSS; MD Dept of Labor (MHIC + trade boards); **VA DPOR**
  - **California:** **CSLB**
  - **Texas:** **TDLR** (electricians, HVAC/ACR) and **TSBPE** (plumbers)
- Include **active and inactive** statuses; keep the board’s raw status string alongside your normalized `active | inactive`.
- Fuzzy-match across legal name, DBAs, and owner/principal; normalize LLC/Inc/Corp punctuation and whitespace.
- Prefer **official bulk downloads / open data** where they exist (see `data/boards.md`) over brittle HTML scrapes. If a board is CAPTCHA-gated, **document it** and implement what is possible without bypassing protections.
- Unit tests for name normalization, status mapping, and matching heuristics.
- Light caching + polite rate limiting.

### Day 3 — Tool interface, eval, write-up (~3h)

- Expose `find_licenses(phone)` (HTTP JSON endpoint **or** a simple MCP-style tool function — your choice).
- Idempotent re-runs: cache by normalized phone; do not clobber a prior good result with a failed partial run (version or keep-last-good).
- Run against `data/phones.csv`; report identity accuracy and license precision/recall vs. your own notes (full expected answers are held for reviewers).
- Short write-up: design decisions, failure modes (common names, DBA vs legal name, owner-only licenses, multi-state pros, trades that don’t require a statewide license), what you’d do with more time.
- README: how to run, env vars (API keys), tests.

Trim ruthlessly if any day drifts past ~3 hours. A sharp Day 1+2 with a thin Day 3 beats a sprawling unfinished UI.

---

## Deliverables

1. Git repo (preferred) or zip of your code.
2. `README.md` — setup, how to run CLI + API/tool, design decisions, trade-offs, “with more time.”
3. Working pipeline: phone → identity → licenses → JSON.
4. Tests (at least for phone normalize, name normalize, matching / status mapping).
5. Example output JSON for 2–3 phones from `data/phones.csv` (checked into the repo).

### Allowed

- Any backend language (**Python or TypeScript** suggested).
- AI coding assistants — **you must still explain the design** in the README (what *you* decided vs. what the model suggested).
- Your own **Google Places / Cloud API key** (free-tier credit is enough for this exercise). If you cannot obtain one, reply to the email and we will provide a capped key.

### Do **not** spend time on

- Frontend / fancy HTML / design systems.
- Mobile apps, auth, payments, booking flows.
- Circumventing CAPTCHAs, WAFs, or Google/board bot protections.
- Inventing license numbers, hours, or NAP to “look complete.”

### Submission

Reply in the existing **“Next steps with HireNimbus”** email thread with the repo link (or zip) by **Monday, Sep 28, 2026, end of day ET**. Questions welcome anytime.

---

## Data provided

| File | Purpose |
|------|---------|
| `data/phones.csv` | ~28 test phone inputs (format variants, real public business lines, invalid, `555-01xx` fakes) |
| `data/boards.md` | Board URLs, bulk/open-data notes, CAPTCHA warnings |
| `data/category_taxonomy.json` | Small fixed trade taxonomy to map GBP/place types → library categories (keep raw type alongside) |

All phones in `phones.csv` are **business main lines** taken from public websites / directory listings for home-service companies in DC Metro, SF Bay, or Austin — **not** personal cell numbers. Live GBP and license data **drift**; treat the CSV as a starting harness, not a frozen oracle.

---

## Honesty contract (non-negotiable)

- Every license object includes `evidence_url` (or explicit `evidence: null` with reason) and `fetched_at`.
- `search_status` per board: `ok | not_found | unreachable | captcha_blocked | skipped`.
- Never equate `not_found` with “this business is unlicensed” without a caveat (wrong name form, wrong board, owner-only license, threshold exemptions for handymen, etc.).
- Places API returns only a **handful of reviews** and often **no service-area polygon** — document limits; do not fabricate them.

Good luck — we’re excited to see how you think.
