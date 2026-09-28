# NOR-69: Prowlarr download redirects

Reviewed 2026-09-27: [NOR-69](https://linear.app/northernlogic/issue/NOR-69),
[GitHub #32](https://github.com/logabell/dewarr/issues/32), and related
[NOR-71](https://linear.app/northernlogic/issue/NOR-71) /
[GitHub #34](https://github.com/logabell/dewarr/issues/34), including their comments.

## Findings

The confirmed acquisition failure is in `ProwlarrClient.request`: all 3xx
responses were rejected, including binary resolution through the validated
`/{indexerId}/download` endpoint. A successful connection test only checks
`api/v1/system/status`; it does not exercise release retrieval. Prowlarr search
can therefore succeed while every NZB acquisition fails before reaching SABnzbd.
Automatic selection also replaced the adapter's safe explanation with a generic
inspection failure, and source-result feedback linked to `/requests` even when
there was no saved request ID.

The [official Prowlarr indexer documentation](https://github.com/Servarr/Wiki/blob/master/prowlarr/indexers.md)
states that redirects are mandatory for Usenet/Newznab and optional for torrent
indexers. The failure consequently affects NZBs and HTTP(S) torrent redirects.
Magnet redirects are a separate capability and remain unsupported here.

The limited search results in #32 and absence of torrents in #34 are not proven
to share this root cause. Search runs against enabled, searchable, nonexcluded
Prowlarr indexers with book/audio capabilities, at most 20 indexers, using ebook
7020/audio 3030 categories and bounded pages. It does not select indexers based on
the configured download client. The book query plan can also differ from a manual
Prowlarr search. Replacing SABnzbd with qBittorrent cannot convert an NZB into a
torrent or add torrent indexers. No user installation logs or indexer inventory
were available to establish a separate search defect. Do not broaden categories
or bypass protocol matching based solely on these reports.

## Implementation decision and plan

1. Keep search-reference validation restricted to the exact configured Prowlarr
   proxy and leave status/search API redirects disabled.
2. Resolve redirects only during binary acquisition. Follow 301/302/303/307/308,
   allowing at most three external fetches under one 50-second resolution budget.
3. Use a separate HTTP client, with no inherited API key, authorization, cookies,
   referrer, or environment proxies. Preserve only the target URL's own query.
4. Validate every redirect: HTTP(S), no userinfo, no fragment, no HTTPS downgrade,
   and exclusively public IPv4/IPv6 DNS results. Pin the actual connection to a
   validated address while retaining the original Host and TLS server name. Try
   up to four validated addresses on connection failures; never retry a partial
   response. Disable connection reuse so different hostnames sharing an IP each
   receive their own TLS verification. Private configured Prowlarr endpoints remain valid, but redirects
   cannot expand that trust to arbitrary private services.
5. Retain the 8 MiB artifact limit; reject encoded external responses, propagate
   Retry-After cooldowns, and suppress credential-bearing download logs. Keep
   diagnostics free of URLs, response bodies, and transport exception text.
6. Feed downloaded bytes into the existing NZB/torrent inspection, encrypted
   artifact persistence, generation checks, protocol-compatible selection and
   idempotent dispatch. Do not pass unchecked URLs to SABnzbd or bypass inspection.
7. Preserve safe adapter explanations in automatic-selection decisions; show the
   reason in release details and link to Requests only when a request ID exists.

This applies [OWASP's SSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)
by handling redirects explicitly and validating all resolved addresses, including
subsequent destinations. It also accounts for [HTTPX client-level headers and
cookies](https://www.python-httpx.org/advanced/clients/), which must not be reused
for an indexer's credential-bearing download URL. Merely enabling
`follow_redirects=True` on the authenticated Prowlarr client would not meet these
requirements.

## Verification

Focused adapter tests cover redirect status codes for both protocols, relative
redirects, original Host/TLS identity, credential/cookie isolation, log privacy,
private and mixed DNS answers, invalid URLs, redirect loops, size limits,
compression, timeout, DNS failure and rate-limit propagation.

API/worker journeys cover Prowlarr search through inspection and a single
qBittorrent/SABnzbd submission, both proxied and redirected. Invalid redirected
bytes must not create artifacts. Automatic-selection tests retain the adapter's
specific reason when falling back to another candidate. The existing sources UI
journey checks visible failure explanations and the absence of a misleading
Requests link, including compact/mobile rendering.

Verified using the bounded runner:

- `unit tests/unit/test_prowlarr.py`: **61 passed**.
- `backend tests/integration/test_prowlarr_sources.py tests/integration/test_sabnzbd.py tests/integration/test_automatic_selection.py::test_rejected_first_candidate_falls_back_to_next_and_retains_decisions`:
  **20 passed** using `BOOK_TEST_DATABASE_URL` for the disposable database below.
- `ui quick-add-sources.spec.ts`: **1 passed**, including the TypeScript check
  and production frontend build.
- Focused Ruff, Prettier and whitespace checks passed.

All tests ran through `python3 scripts/check.py`. The existing `book_search_test` database has a stale migration
reference (`0049_storygraph_accounts`), so this task uses a fresh disposable
`dewarr_nor69_test` database on the existing local PostgreSQL server.

Real authenticated indexer traffic and the reporter's deployment remain untested;
these regressions use controlled HTTP responses and the application's real
persistence/selection/dispatch paths with mocked download clients.
