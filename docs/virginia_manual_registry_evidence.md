# Virginia manual official-source evidence workflow

Virginia SCC CIS currently requires reCAPTCHA for name-search submission. The
agent stops at that control and reports `captcha_blocked`. A Virginia deliverable
example may use an operator-performed lookup in the normal public CIS interface,
provided the manual acquisition and later automated processing remain explicit
and auditable.

## Evidence acquisition

1. Open the official [Virginia SCC entity search](https://cis.scc.virginia.gov/EntitySearch/Index)
   in a normal browser and complete the public search manually.
2. Record the exact search term, search mode, and lookup date/time with timezone.
3. Preserve the candidate list. Record every candidate relevant to the selection,
   including entity name, entity ID, status, and candidate detail URL.
4. Select an entity only under the Virginia establishment policy:
   - an official fictitious-name relationship exactly normalizes to the Places
     public name; or
   - the SCC legal name exactly normalizes to a base legal/business name and no
     material conflict remains; or
   - one plausible-name candidate remains after an exact principal-office address
     match.
5. Open the official known-ID detail URL and preserve legal name, entity ID,
   status, principal-office address, and registered-agent fields as displayed.
6. Preserve official linked fictitious-name or principal evidence when it is
   available. Keep registered-agent data as evidence only; it never supplies a
   principal search key.

## Required provenance record

Store the following beside the example:

- search term and search mode;
- lookup timestamp and operator/timezone;
- candidate list and selection rationale;
- selected entity ID and official detail URL;
- relevant raw labels and displayed values;
- capture files, such as screenshots or raw HTML, with SHA-256 hashes;
- whether each capture is raw or sanitized and, if sanitized, exactly what was
  removed;
- establishment evidence and any conflicts considered;
- resulting `RegistryEnrichmentResult`, expanded `SearchKey` values, DPOR source
  requests/results, and final match decision.

Suggested metadata shape:

```json
{
  "acquisition": "manual_official_scc",
  "search_term": "<exact term>",
  "search_mode": "<selected CIS mode>",
  "looked_up_at": "<ISO-8601 timestamp with timezone>",
  "candidate_list": [
    {
      "entity_id": "<SCC ID>",
      "entity_name": "<displayed name>",
      "status": "<displayed status>",
      "detail_url": "<official URL>"
    }
  ],
  "selected_entity_id": "<SCC ID>",
  "detail_url": "<official URL>",
  "raw_fields": {"<official label>": "<displayed value>"},
  "captures": [
    {
      "path": "<relative evidence path>",
      "kind": "raw_html_or_screenshot",
      "sha256": "<hash>",
      "sanitization": "<none or exact description>"
    }
  ],
  "establishment_evidence": ["<policy evidence>"],
  "conflicts": []
}
```

## Pipeline handoff

Translate only the retained official fields into a `RegistryEnrichmentResult`.
Once that result is established, `generate_expanded_search_keys()` combines its
legal names, official DBAs, and explicitly labeled principals with the Places
keys. The pipeline passes that same collection to DPOR retrieval and final
matching. Record both the expanded collection and final decision in the example.

Synthetic SCC and DPOR fixtures in the test suite demonstrate this plumbing only.
They are not manual official-source examples and must not be presented as one.
