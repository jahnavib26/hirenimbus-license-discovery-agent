# Registry fixture provenance

## Synthetic fixtures

Files prefixed with `synthetic_` were manually constructed for parser and pipeline
tests. They are not Virginia SCC captures, and their companies, entity IDs,
people, URLs, tokens, and DPOR records are fictional. They must not be used as a
deliverable registry example or cited as official evidence.

## Sanitized official capture

`sanitized_real_va_scc_detail_11004293.html` retains the relevant nested field
markup and displayed values from a legitimate public response captured from:

`https://cis.scc.virginia.gov/EntitySearch/BusinessInformation?businessId=11004293`

- Captured: 2026-10-01 UTC
- Source: Virginia SCC Clerk's Information System
- SHA-256 of the complete raw response before sanitization:
  `292622bc9a81d8055e0f32dfa5f14d5f7793e56d4461b235976ef7ebf746c87f`
- Sanitization: navigation, scripts, styles, unrelated panels, and blank layout
  elements were removed. The tested field labels, values, nesting, and CSS class
  structure were retained.

This known-ID detail response is parser evidence. It does not prove that SCC name
search can be automated and does not replace the manual candidate-selection
record required for a deliverable example.

## Manual official-source examples

A deliverable example must follow `docs/virginia_manual_registry_evidence.md` and
include its own lookup metadata and retained official evidence. No completed
manual example is stored in this fixture directory.

## Maryland and California manual-import fixtures

`synthetic_manual_md_sdat_evidence.json` and
`synthetic_manual_ca_sos_evidence.json` exercise the explicit manual-evidence
import contract. Their companies and identifiers are fictional, and the files
are not raw government responses. Field names are limited to facts documented
by the respective official public registries. They must never be presented as
real registry captures.

Maryland live search and entity-detail submission use Cloudflare Turnstile.
Production code does not submit those protected forms. A deliverable using the
manual import path must retain its real lookup timestamp, search term/mode,
official entity URL/identifier, and the corresponding screenshot or raw-capture
hash outside these synthetic test fixtures.
