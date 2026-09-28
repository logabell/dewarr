# NOR-71 and NOR-72: torrent discovery and Deluge connection testing

Investigated 2026-09-27 using the Linear issues and comments, the linked
[qBittorrent report](https://github.com/logabell/dewarr/issues/34), and upstream
protocol sources.

## NOR-72: confirmed RPC mismatch

`DelugeClient.capabilities()` called `daemon.info`. Deluge 2.1.1 exports
[`daemon.get_version`](https://github.com/deluge-torrent/deluge/blob/deluge-2.1.1/deluge/core/daemon.py),
so a real daemon rejects the old method before Dewarr can mark the connection
ready. The existing mocks accepted the nonexistent method and concealed the bug.

The adapter now calls `daemon.get_version`. RPC fixtures use the actual method,
and the connection API regression returns Deluge's `Unknown method` error for
unrecognized calls. It verifies a connected, selectable torrent route and the
persisted daemon version, without submitting a download.

## NOR-71: acquisition fix and missing-source diagnostics

There are two distinct symptoms: downloads fail, and search returns the same NZBs
after changing from SABnzbd to qBittorrent.

The existing uncommitted NOR-69 changes already address rejected Prowlarr HTTP(S)
download redirects for both NZBs and torrent files. The
[Prowlarr documentation](https://github.com/Servarr/Wiki/blob/master/prowlarr/indexers.md)
confirms torrent indexers can redirect. These changes were retained and their
torrent search → artifact inspection → single qBittorrent submission regression
was included in this task's verification. See [NOR-69](NOR-69.md) for the redirect
implementation, security boundaries, and tests. Magnet redirects remain unsupported.

The issue does not supply the reporter's indexer inventory, search responses, or
deployment logs. A separate cause of missing torrents cannot be established from
the report. Search selects enabled, searchable, nonexcluded indexers with book or
audio capabilities; it does not select indexers from the downloader configuration.
Changing clients therefore cannot make an NZB source return torrents. The upstream
[indexer resource](https://github.com/Prowlarr/Prowlarr/blob/develop/src/Prowlarr.Api.V1/Indexers/IndexerResource.cs)
exposes protocol, enablement, search support, and categories independently.

A confirmed diagnostic gap made this hard to discover: indexer discovery reported
success even with no eligible torrent indexers, and the Sources page hid completed
discovery messages. Discovery now reports the selected torrent/Usenet counts and
explains how to enable suitable torrent indexers and check source exclusions when
none qualify. The page shows that summary even when other sources return results.
The existing 20-indexer limit is reported without claiming omitted indexers are
ineligible. Search categories and protocol matching are unchanged.

API regressions exercise enabled, disabled, excluded, and non-book torrent
indexers alongside Usenet with a configured qBittorrent route. They verify which
indexers receive search requests and the resulting diagnostics. The existing
browser journey checks that the guidance is visible.

The reporter's exact missing-results cause still needs their Prowlarr indexer
configuration and a comparison using the same query/categories. These local fixes
do not certify their live installation or imply NOR-71 is fully resolved there.

## Verification

All tests used `python3 scripts/check.py`, with no overlapping runs:

- `unit tests/unit/test_torrent_rpc_clients.py tests/unit/test_prowlarr.py`:
  **69 passed**.
- Backend: **14 distinct scenarios passed** across the additional torrent-client
  lifecycle/connection tests, the four new book-source discovery cases, and the
  two existing proxied/redirected Prowlarr-to-qBittorrent journeys. Focused retries
  corrected new test assertions about the internally stored version and the
  fixture's separate pending MAM worker. Passed cases were not rerun unnecessarily.
- `ui quick-add-sources.spec.ts`: **1 passed**, including TypeScript checking and
  the production frontend build. The desktop screenshot was visually inspected.
- Focused Ruff lint/format, Prettier, and `git diff --check` passed.

Backend checks used the isolated `dewarr_nor71_72_test` database on the existing
local PostgreSQL server. HTTP services were mocked; no live downloader or reporter
deployment was exercised. Existing unrelated workspace changes were retained.
