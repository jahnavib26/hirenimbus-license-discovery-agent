# Maryland manual official-source evidence workflow

Maryland Business Express currently places Cloudflare Turnstile on the entity
search form and on the form that opens entity details. The live registry source
does not submit either protected form. It returns `skipped`, and the normal
board pipeline continues with Places keys.

An auditable example may use a lookup performed manually in the normal public
Business Express interface:

1. Record the exact search term, search mode, lookup timestamp, and timezone.
2. Preserve the returned candidate list and the official Department ID for each
   relevant candidate.
3. Preserve the selected entity's official detail URL, legal/business name,
   status, principal-office address, and raw field labels.
4. Preserve any trade-name record and its exact Department ID relationship.
5. Preserve explicitly labeled filer relationships. Resident agents,
   organizers, and document signers remain evidence only unless the same source
   separately gives the person a qualifying current principal role.
6. Preserve a screenshot or raw response with a SHA-256 hash. If a capture is
   sanitized, record exactly what was removed.
7. Apply the locked establishment policy and record the evidence and conflicts
   considered. Multiple unresolved candidates produce `ambiguous` and no
   registry keys.
8. Store the resulting `RegistryEnrichmentResult`, expanded `SearchKey` list,
   board result, and final match decision together.

The manual JSON import accepted by `MDSDATRegistrySource` must declare
`capture_method` as `manual_official_source` and use an official
`egov.maryland.gov` source URL. Synthetic fixtures exercise this import shape
only; they are not official evidence for a real entity.

Maryland Labor's public query pages include electrician, MHIC, plumbing, and
HVACR searches. The checked electrician, plumber, HVACR, and MHIC name forms
explicitly require a human CAPTCHA before submission. The electrician and MHIC
adapters may fetch the official form, but they do not submit names or solve the
challenge. `BoardSearchResult.search_keys` and the final board audit retain the
actual expanded keys and provenance so the handoff remains inspectable when the
source stops at `captcha_blocked`. This is not a completed live name query.

Plumbing and HVACR are not currently selected by `board_selection.py`. A valid
registry enrichment may therefore lead to an audited electrician or MHIC board
attempt that ends `captcha_blocked` and leaves the overall pipeline `partial`.
