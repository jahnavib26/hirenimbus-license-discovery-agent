# License boards & public data (reference for candidates)

Live data and portals change. Prefer official sources. **Do not bypass CAPTCHAs.**

## California — CSLB (Contractors State License Board)

- Instant check / search: https://www.cslb.ca.gov/
- **Public Data Portal (License Master CSV/XLS):** https://www.cslb.ca.gov/onlineservices/dataportal/ContractorList  
  Free download of currently renewed + *expired-but-renewable* licenses. Cancelled / revoked / expired-non-renewable are **not** in the free master — note that gap if you rely on it.
- Paid full historical files exist; **out of scope** for this exercise unless you already have them.
- Phone fields exist on many License Master rows — useful for phone→license after identity.

## Texas — TDLR (electricians, HVAC/ACR, etc.)

- License search: https://www.tdlr.texas.gov/LicenseSearch/
- **Open Data — TDLR All Licenses** (updated ~daily): https://data.texas.gov/dataset/TDLR-All-Licenses/7358-krk7  
  Socrata API example: `https://data.texas.gov/resource/7358-krk7.json?$limit=50000`
- Official download page also linked from TDLR search (`licfile.asp`).

## Texas — TSBPE (plumbers)

- Free licensee lists: https://tsbpe.texas.gov/free-licensee-list/  
  Includes Responsible Master Plumber (RMP) / company CSVs.
- Interactive search: https://vo.licensing.hpc.texas.gov/datamart/selSearchType.do

## Virginia — DPOR

- Online license lookup: https://www.dpor.virginia.gov/
- **Regulant lists (free tab-delimited TXT, updated ~every 5 business days):** https://www.dpor.virginia.gov/RegulantLists  
  Contractor classes (e.g. 2705 A/B/C) and tradesman combined lists (2710) are the ones most relevant to home services.
- Excellent for offline fuzzy name search without CAPTCHA.

## Maryland

- **MHIC** public query: https://www.dllr.state.md.us/cgi-bin/ElectronicLicensing/OP_search/OP_search.cgi?calling_app=HIC::HIC_qselect  
  Often usable for active contractors; may be fragile / rate-limited.
- MD State Board of Electricians / other trade queries: frequently **CAPTCHA-gated**. If blocked, set `search_status: captcha_blocked` and stop — do not automate CAPTCHA solving.
- MD SDAT business entity search (identity step): https://egov.maryland.gov/BusinessExpress/EntitySearch

## District of Columbia

- DLCP / licensing moving toward **BOSS** (Business One Stop Solution). Start from https://dlcp.dc.gov/ and current BOSS portal links.
- Corp registration (identity): DC CorpOnline / BOSS entity search.
- Expect thinner bulk dumps than VA/CA/TX; document whatever you find.

## Google Places (identity step)

- Find Place from Phone Number / Text Search + Place Details.
- Useful fields: `name`, `formatted_phone_number` / `international_phone_number`, `formatted_address`, `types`, `business_status`, `url`, `website`, `opening_hours` (optional for this exercise).
- Reviews: API returns only a small sample — document the limit.
- Service areas: often **not** exposed — return `null` + note rather than inventing ZIPs/cities.

## Suggested approach (not mandatory)

1. Day 1: Places (and optional SOS/SCC) → identity JSON.  
2. Day 2: Download/cache VA DPOR regulant list + TDLR open data + CSLB License Master (and TSBPE CSVs when needed) → local index → fuzzy match. Use interactive board search only when bulk data cannot answer.  
3. Always record `fetched_at` and the exact evidence URL or file id/version.
