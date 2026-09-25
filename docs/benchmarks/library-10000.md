# 10,000-book benchmark — September 25, 2026

Measured the real FastAPI routes, SQLAlchemy/PostgreSQL queries, and inventory
synchronization code against a disposable local database. These are workstation
measurements, not measurements of a deployed NAS or remote provider.

## Fixture and method

- Apple M5 Pro, 24 GiB RAM, PostgreSQL 16.14 (Homebrew).
- 10,000 works, versions and library assets; 1,000 duplicate title/author pairs
  project to 9,000 cards. Both ebook and audio holdings are represented.
- Each asset has 12 file records and approximately 2,000 characters of descriptive
  metadata. No real credentials, library files or provider accounts are used.
- Fixtures insert in 500-row batches and run ANALYZE before measurements.
- The request harness uses the real ASGI application. Artwork retrieval and the
  inventory backend are synthetic; network latency and image conversion are excluded.
- Two sequential requests per route. The table shows the second request, with
  database buffers warm and the cover response cached. These are individual
  measurements, not p95/p99 estimates or cold-disk results.
- Runs use `scripts/check.py`, one test worker, a 300-second overall budget and
  2 GiB sampled process-group memory budget. The benchmark database also had a
  15-second statement timeout. No full application suite ran alongside them.

## Results

| Workload | Before | After | Change |
| --- | ---: | ---: | ---: |
| Library first page, 40 cards | 1,370 ms | 523 ms | 62% faster |
| Library offset 8,000, 40 cards | 1,364 ms | 475 ms | 65% faster |
| Catalog title search | 1,561 ms | 395 ms | 75% faster |
| Discover library shelf | 1,357 ms | 287 ms | 79% faster |
| Cached library cover | 1,416 ms | 80 ms | 94% faster |
| Unchanged inventory scan, 10,000 items | 12.45 s | 11.01 s | 12% faster |
| Asset UPDATE statements per unchanged scan | 401 | 101 | 75% fewer |
| Total SQL statements per unchanged scan | 1,946 | 1,646 | 300 fewer |

Both inventory runs made 200 summary-page calls (100 census plus 100 verification)
and **zero expanded metadata calls**. All 10,000 cached details were reused and
all assets received the new observation generation. Rechecking the census remains
necessary to avoid publishing an inconsistent inventory.

Two page reads during a separate inventory run took 557 and 469 ms. Private
library browsing as a granted member took 717 ms for the repeated first page and
671 ms at offset 8,000. These supplemental runs have no before counterpart;
their scan timing also varied from the standalone comparison above.

A prepared-query check forced PostgreSQL's generic plan mode and repeated a
single-family lookup 15 times. Bound normalization-rule constants initially
prevented matching the expression index: steady lookups took about 52 ms. Keeping
the fixed rules as SQL constants reduced steady lookups to about 0.1 ms; the
benchmark also checks the generic EXPLAIN plan for the family index. Book titles
and other user input remain bound parameters.

## Changes supported by the measurements

1. Detail and page availability queries restrict presentation grouping to the
   requested title families. All subtitle, author, language and accepted-identity
   conflicts within those families still participate. Canonical redirects retain
   their origin bindings; grouping never becomes acquisition identity evidence.
2. Migration `0073_display_family_index` indexes normalized title families. It
   builds concurrently and replaces an interrupted build on retry. The historical
   expression is frozen in the migration. Expression indexes exchange extra write
   work for faster lookups; the new index targets the repeatedly measured lookup.
   See [PostgreSQL expression indexes](https://www.postgresql.org/docs/16/indexes-expressional.html)
   and [concurrent index builds](https://www.postgresql.org/docs/16/sql-createindex.html).
3. Page identity selection and totals share one query. Large work metadata is
   fetched after pagination, outside the count window. Requests beyond the final
   page still return the correct total through a count fallback.
4. Discover no longer computes a second equivalent grouping relation after its
   scoped holdings query has already selected the displayed work IDs.
5. Availability reads omit file manifests and unnecessary metadata. Primary
   edition selection reads only saved primary-edition preferences.
6. Reused inventory formats receive one conditional update per batch. Removed
   formats remain absent, and intentionally removed assets retain their state.
7. Fixed normalization rules are rendered as safely quoted SQL constants so
   expression indexes remain usable with generic prepared plans.

These changes introduce no library-size limit and no persistent cache of reader
permissions or grouped ownership.

## Reproduce

Create a disposable PostgreSQL database whose name ends in `_test`; the test
fixture truncates its application tables. From the repository root:

```sh
BOOK_RUN_LIBRARY_BENCHMARK=1 \
BOOK_BENCHMARK_LABEL=latest \
BOOK_TEST_DATABASE_URL=postgresql+psycopg:///library_benchmark_test \
python3 scripts/check.py backend tests/integration/test_library_benchmark.py
```

Set `BOOK_BENCHMARK_WITH_READER=1` to include two library-page reads during the
inventory scan. The harness also exercises private-library browsing as a granted
member. JSON reports are written under `.local/benchmarks/`. The heavy fixture is
skipped unless explicitly enabled, including in ordinary focused checks.

The saved `library-10000-before` and `library-10000-prepared-fixed` JSON reports
record the request comparison and the slowest SQL statements; the inventory
comparison uses `inventory-10000-before` and `inventory-10000-final`. Supplemental
reports use `reader-load` and `prepared` prefixes/labels. The before measurements
were captured before these optimizations;
rerunning the current implementation does not reconstruct the prior code.

## Scope of the result

This verifies local query and unchanged-scan improvements at the requested
10,000-book scale. It does not establish initial-import throughput, remote-provider
latency, filesystem copy/hash performance, PostgreSQL 18 deployment timings, or
sustained multi-user capacity. Those depend on actual deployment hardware, data
distribution, storage and backend services. No deployment changes were made.
