# Phase 2 evaluation audit

This audit accompanies `phase2_evaluation_report.json`. The checked-in Phase 1
report, `day3_evaluation_report.json`, remains the historical baseline and was
not regenerated or edited. Production matching does not read the manual ground
truth file.

## Observed result

| Metric | Phase 1 baseline | Phase 2 live run |
|---|---:|---:|
| Accepted verified licenses | 1 / 8 | 1 / 8 |
| Precision | 1 / 1 (100%) | 1 / 1 (100%) |
| Recall | 1 / 8 (12.5%) | 1 / 8 (12.5%) |
| Recovered through registry legal name | n/a | 0 |
| Recovered through official DBA/trade/fictitious name | n/a | 0 |
| Recovered through explicit registry principal | n/a | 0 |

The observed live run produced no increase in accepted licenses. It did not
lower the exact-name policy or accept a fuzzy/partial near miss.

Across the primary unique valid evaluation cases, source paths recorded one
`not_found`, zero `ambiguous`, one `captcha_blocked`, eight `unreachable`, and
eight `skipped` results. Two of the skipped results had `issue_kind` set to
`unsupported`. These are source-path counts, so one case can contribute more
than one outcome. Six verified licenses across P08, P09, and P11 are Texas
records; Texas intentionally remains outside Phase 2 registry expansion.

## Each verified reference license

| Case | Verified license | Result | Concrete observed reason |
|---|---|---|---|
| P03 | DPOR 2710061674, JOTIS LAMAR BROWNLEE | Not recovered | VA SCC name search stopped at reCAPTCHA, so no legal-name or principal relationship was established. DPOR was then `unreachable` because regulant list 2705 A contained malformed rows. |
| P04 | MD Electricians 14003, BELVIN DOUGLAS MITCHELL | Not recovered | MD Business Express was `skipped` because the protected Turnstile flow has no configured public automation contract. The electrician board path was also `skipped` with `no_safe_adapter`; no official business-to-holder relationship reached matching. |
| P08 | TSBPE 20628 | Not recovered | TX is outside Phase 2 registry expansion. The existing TSBPE CSV request returned HTTP 403 and was `unreachable`. |
| P08 | TDLR 30557 | Not recovered | TX is outside Phase 2 registry expansion. The TDLR source was actually searched with the Places name and returned `not_found`. |
| P08 | TDLR TACLA00135747C | Not recovered | TX is outside Phase 2 registry expansion. The same TDLR search completed as `not_found`; no candidate carrying this number was retrieved. |
| P09 | TDLR 33423 | **Accepted** | The existing TX path retrieved FOX SERVICE COMPANY. Exact normalized business name plus exact official address and compatible trade evidence supported acceptance; the conflicting board phone remained explicit. |
| P09 | TDLR TACLB00112806E | Not recovered | TX is outside Phase 2 registry expansion. TDLR returned license 33423 for the base-name query, but it did not return this HVAC reference, so matching had no candidate to evaluate. |
| P11 | TSBPE 45226 | Not recovered | TX is outside Phase 2 registry expansion. The existing TSBPE CSV request returned HTTP 403 and was `unreachable`. |

None of these misses indicates an acceptance-policy defect. They result from
official-source access limits, absent retrieved candidates, or the deliberate
Texas scope boundary. The report does not treat a blocked or empty lookup as an
unlicensed conclusion.

## Deliverable evidence

The four files below are direct observed pipeline audits. None uses a synthetic
fixture or claims manual evidence was used:

- `../examples/phase2_va_brownlee_audit.json`
- `../examples/phase2_md_powerworks_audit.json`
- `../examples/phase2_dc_wl_gary_audit.json`
- `../examples/phase2_ca_roto_rooter_audit.json`

Synthetic and sanitized fixtures under `tests/fixtures/registry_sources/` are
test inputs only. They are not counted as evaluation recoveries. The documented
Virginia and Maryland manual workflows describe how future official captures
can be supplied with timestamps, source URLs, raw fields, and capture hashes;
no such manual capture was used in this live evaluation.
