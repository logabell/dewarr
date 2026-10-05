# Supplemental metadata and discovery sources

Hardcover remains the default work catalog. Additional sources have specific jobs:

| Source | Added value | Automatic behavior |
| --- | --- | --- |
| Open Library | Missing book description, publication year, cover, ebook editions | Existing bounded, unique-match enrichment |
| Audible | Recording ASIN, narrators, language, abridgment, runtime, recording release date, recording series | Enrich an explicit Hardcover ASIN; exact-ASIN fallback during audio import; search fallback when the primary catalog has no results |
| Audnexus | Missing recording details on the same ASIN | Fill missing narrator/runtime-related details after Audible; never counted as independent identity corroboration |
| Official award organizations | Why a work or performance was recognized | Bundled, verified collections; no account needed |
| New York Times Books API | Ranked bestseller collections | Current lists load on demand and refresh daily while followed; requires an administrator API key |
| Custom regional endpoint | Language/market coverage absent from the primary catalog | Optional fallback search, or explicit selection; never an automatic identity override |

Existing Libro.fm upcoming and library enrichment remains in place.

## Recording safety

The enrichment worker checks the work identity, source revision, settings, reader access,
and operation claim again after network I/O. Rejected sources stay rejected. Missing
values can be filled, but locked editions and populated narrator/language/abridgment
fields are preserved. Conflicting recording facts appear as an edition needing review.

An exact ASIN can reuse one accepted catalog edition. Two versions claiming an ASIN
are not silently merged. A recording release date is stored on the edition, not as
the book's first publication year. Runtime is in minutes. Audiobook import fallback
requires an ASIN and compatible file evidence, including narration. This does not
make a title-only match sufficient for a recording.

Audible's catalog endpoint is not a guaranteed public API. Timeouts, bounded responses,
shared request budgets, cached reads, and failure preservation apply. Discovery reads
bounded storefront selections, then verifies those ASINs through the catalog API.
Recent/upcoming shelves validate dates locally because the catalog API silently ignores
some sort/date parameters. An unrecognized storefront layout preserves the last snapshot;
it never silently publishes an empty list.

## Discovery and awards

Discover → Awards supports organization, year, category, audience, and language filters.
Each collection shows its original source and coverage. Books show winner/finalist/honor
status, rank when supplied, and relevant illustration or narration credits.

Initial official coverage is deliberately scoped: 2025 Hugo Best Novel, 2024 Nebula Best
Novel (2025 ceremony), the 2025 Booker shortlist and Pulitzer Fiction finalists, 2026
Newbery and Caldecott medal/honor books, and 2025 Audie Audiobook of the Year finalists.
Each snapshot links to its evidence in `services/app/data/discovery/official-awards.json`.
“Complete” describes the named category/shortlist, not the organization's entire archive.
Official snapshots update with app releases; following them does not imply live scraping.

NYT supports combined fiction/nonfiction, hardcover fiction, young adult hardcover,
audio fiction, and audio nonfiction. Lists display their publication date. Audible has
popular, recent recording releases, and upcoming recording shelves in the configured market.
Unavailable live sources retain the last successful snapshot and display a warning.

Pinning controls the For You layout. Following controls refreshes. Neither creates a
request or downloads books. Bulk download actions remain separate and explicitly invoked.
Recognition of a specific recording or illustrated edition requires edition selection;
it cannot silently become a generic work request. New sources do not opt readers into
future automatic downloads.

## Optional deployment configuration

Set these in the API **and worker** environments and restart those processes. Secrets
are never returned in capabilities, snapshots, or job payloads.

| Environment variable | Value |
| --- | --- |
| `BOOK_AUDIBLE_REGION` | `us` (default), `uk`, `au`, `ca`, `de`, `es`, `fr`, `in`, `it`, `jp` |
| `BOOK_NYT_API_KEY` | Books API key from the New York Times; enable Books API access on that key |
| `BOOK_METADATA_PROXY_URL` | Optional HTTP(S) forward proxy; user/password may be included |
| `BOOK_CUSTOM_METADATA_URL` | Optional administrator-managed HTTP(S) endpoint, including local services; no URL credentials/query/fragment |
| `BOOK_CUSTOM_METADATA_TOKEN` | Optional bearer token sent only to that endpoint |

The metadata proxy applies to Hardcover, Open Library, Audible, Audnexus, NYT, custom
metadata, and catalog-cover downloads. A proxy failure never falls back to a direct
connection. Library/download connections and existing Goodreads/Libro.fm scrapers have
their own network behavior; this setting does not reroute the whole application.
Marketplace, language, and proxy are independent choices. Custom cover hosts remain
restricted to the existing vetted image-host allowlist.

The custom adapter implements the Audiobookshelf-style `GET /search?query=...` response:

```json
{"matches":[{"title":"A book","author":"An author","language":"ru","isbn":"978...","description":"...","narrator":"A narrator"}]}
```

At most 100 matches are accepted. Search results expire after one hour; selecting a
stale result asks the reader to search again. The adapter does not execute plugins,
fetch caller-supplied URLs, or infer a stable author identifier from a display name.
Operators can bridge regional services they are entitled to use. No built-in LitRes
partnership or Yandex Books access is claimed.

## Further source admission

Add official facts to versioned manifests with stable source-scoped identities,
original evidence URLs, category/year, coverage, status, and subject (`work`, `recording`,
or `edition`). Keep illustrator credits separate from authors and narrators. Do not
turn short-fiction nominations or author-career awards into full-book requests.

Add another automatic metadata provider only after a representative corpus demonstrates
incremental coverage or better accuracy for a specified field. Author portraits, chapter
exports, automatic series/author identity merging, a full historical awards archive,
and provider-specific regional integrations remain separate work; none are assumed to
be more accurate simply because another app supports them.
