# Contributing to Dewarr

## Project layout

- `apps/web`: React and TypeScript UI.
- `services/app`: FastAPI backend, integrations, worker, and database migrations.
- `tests`: backend unit, integration, and adapter contract tests.
- `apps/web/tests`: Playwright browser tests.
- `scripts`: installation, API generation, dependency notices, public catalog updates, and isolated test fixtures.
- `deploy`: optional Docker Compose configurations.

## Run locally

Requires Python 3.13, uv, Node.js 24, PostgreSQL 18, and FFmpeg (including ffprobe).

```sh
uv sync --frozen
npm --prefix apps/web ci
python3 scripts/init_env.py --mode native
```

Create a development database and edit `BOOK_DATABASE_URL` in `.env` to match it. Set `BOOK_PUBLIC_URL` to the browser origin you will use. The generated native database URL is a local example, not a provisioned database.

```sh
uv run alembic upgrade head
npm --prefix apps/web run build
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

Run the worker in another terminal:

```sh
uv run python -m app.jobs.worker
```

Open http://localhost:8000 and create the first account. For UI hot reload, use `npm --prefix apps/web run dev` and set `BOOK_PUBLIC_URL=http://localhost:5173` before restarting the API. Vite proxies API requests to port 8000.

For a container development build:

```sh
docker compose -f compose.yaml -f deploy/compose.build.yaml up -d --build
```

This uses the same two-service layout with a locally built app image. The container entrypoint initializes `/config`, waits for PostgreSQL, runs migrations, and supervises the API and worker. Unit tests in `tests/unit/test_container.py` cover startup and process lifecycle behavior.

## Catalog caching and background refresh

Saved books and followed-list snapshots are durable application data. Page loads
read those records locally; expiration of an HTTP response cache does not delete
saved metadata. Titles, artwork and biographies are relatively stable, but can
still receive corrections. They should not be considered permanently immutable.

Provider response freshness is defined in
`services/app/domain/catalog_cache_policy.py`:

| Data | Freshness / behavior |
| --- | --- |
| Book metadata, reader biographies, Open Library works/authors | 1 day |
| Reader ratings and reviews | 1 hour, fetched separately from descriptive details |
| Details for ranked discovery books | 1 hour; reordering the same IDs reuses the response |
| Trending, recent and upcoming discovery queries | 15 minutes |
| Catalog search and library identifier/title matching | 5 minutes |
| Other catalog queries, including author bibliographies | 1 hour |
| Live provider list membership and list choices | Bypass the catalog response cache |
| Goodreads expanded collection pages | 1 day, scoped to reader and collection version |
| Public cover image bytes | 365 days per URL; browser reuse for 1 day |
| Library cover image bytes | 1 day, invalidated by backend generation, cover path or inventory revision |
| Download-source search results | Separate, short-lived selection data; existing 25-minute expiry |

Browse endpoints explicitly opt into stale-while-revalidate. For up to one day
after freshness expires they return saved provider data and coalesce a durable
`catalog.refresh` job. A dedicated worker slot prevents imports and scans from
occupying every refresh slot. Jobs contain query arguments, never credentials;
they check current access, credential generation and restore state before use.
Refresh failures have bounded retries and a five-minute rescheduling cooldown.
Imports and list synchronization retain synchronous provider-read semantics.

Cold and expired synchronous reads share a short database lease across API and
worker processes. Provider I/O runs outside database transactions, with bounded
fill time and ownership checks before writing or deleting cached entries.
Transient catalog failures may use data up to seven days past expiry; explicit
forced refreshes and access failures do not use that fallback. Cleanup preserves
that retention window and deletes at most 200 expired records per fill, enough
to outpace a collection page's 100 book lookups plus page/count records. Cleanup
skips locked rows so simultaneous refreshes do not deadlock each other's writes.

Library cover responses use `private, no-cache` with an ETag: the browser may
retain the image, but must revalidate access before reuse, including a 304
response. Access and backend configuration are checked again after a cache fill.
Unchanged JSON responses update freshness without rewriting their JSON value.

The UI retains browsed queries for ten minutes after becoming inactive, separately
from each query's freshness. It checks pending refreshes while pages are active
and stops polling fresh results; paginated result flattening is memoized. These
are cache retention and work-batch bounds, not library-size limits.

References: [HTTP cache revalidation semantics](https://www.rfc-editor.org/rfc/rfc9111.html)
and [TanStack Query freshness, retention and refetch options](https://tanstack.com/query/latest/docs/framework/react/reference/interfaces/QueryObserverOptions).

See the [10,000-book benchmark](benchmarks/library-10000.md) for measured query
and inventory improvements, the opt-in runner, and the limits of the measurements.

## Fast homelab iteration with `:dev`

Keep running the native development environment above on your workstation. When
you want to test a batch of changes in your existing homelab stack, publish a dev
image through the **Publish dev image** workflow. It builds one native architecture
with Docker layer caching, checks startup, migrations, and the bundled UI against
a disposable PostgreSQL instance, and then updates `ghcr.io/logabell/dewarr:dev`.
It does not run the full application CI suite or create a GitHub release.

One-time setup: the workflow file must be present on GitHub's default branch
(`main`) before it can be dispatched. This is a repository change, not a release.
The branch being built must contain the current Dockerfile and smoke-test script.

After committing the changes you want to test locally:

```sh
git push origin dev
gh workflow run dev-image.yml --ref main -f ref=dev -f arch=amd64
```

Use `arch=arm64` for an ARM homelab. `:dev` contains only the architecture selected
in the most recent successful run; use the same choice for subsequent builds.
For a specific pushed commit, pass its SHA as `ref`. This builds pushed code, not
uncommitted workstation changes. Docker is not required on the workstation.

Follow the build in GitHub Actions, or find the run with
`gh run list --workflow dev-image.yml` and use `gh run watch RUN_ID --exit-status`.
The summary reports a unique `dev-<commit>-<run>.<attempt>-<architecture>` image tag
as well as `:dev`. The app displays the matching dev build identity.

In your existing homelab Compose file, change only the Dewarr image:

```yaml
services:
  dewarr:
    image: ghcr.io/logabell/dewarr:dev
```

Keep your existing environment, volumes, network, ports, and PostgreSQL service.
After a successful build, run this from that stack's Compose directory:

```sh
docker compose up -d --no-deps --pull always --wait dewarr
```

This pulls the image before recreating Dewarr and waits for its health check.
There is no need to stop the entire stack, and `docker compose restart` alone
does not load a new image. If you use the repository's Compose files directly,
`deploy/compose.dev.yaml` provides the image override.

The first image build will take longer while caches fill. Later runs reuse
unchanged dependencies and runtime layers; timing depends on the changes and
GitHub runner availability. Failed builds or startup checks leave `:dev` unchanged.
Stable release publishing still owns `:latest`; the dev workflow only publishes
dev tags. Full checks remain on pull requests, main/release publishing, and manual
**Application checks** runs.

Because this uses your existing database, [back up PostgreSQL and config](DOCKER.md#backups)
before testing a build with schema changes. Startup applies migrations; changing
the image back to `:latest` does not reverse them. Use the saved image tag or digest
with a matching backup when a schema change prevents an image-only rollback.

## Checks

Start with the smallest relevant selection through the bounded local runner:

```sh
# Small mocked browser journeys; builds once, needs no database or Python backend.
npm --prefix apps/web test
# Specific browser area (also no database).
npm --prefix apps/web run test:ui -- library-browsing.spec.ts
# Cheap focused Python regressions.
python3 scripts/check.py unit tests/unit/test_security.py
# Collection only: no build, browsers, migrations, or worker.
python3 scripts/check.py backend --list
```

The runner permits one run per checkout, uses one test worker and lower process
priority, and stops its tracked process tree after five minutes or 2 GiB of sampled
resident memory by default. Memory accounting includes discovered detached browser
process groups; it is a polling watchdog, not an OS hard memory limit. An existing
PostgreSQL server and unrelated apps are outside that budget. It cleans up tracked
process groups on success, failure, timeout, and interruption. Use `--timeout` or
`--memory-mb` only when a measured need justifies a larger budget. Do not launch
parallel full suites or retry an unchanged failure with more workers/time.

API/worker journeys require a separate PostgreSQL database whose name ends in `_test`:

```sh
export BOOK_TEST_DATABASE_URL=postgresql+psycopg://book:example-password@localhost:5432/dewarr_test
python3 scripts/check.py backend
# Target a changed workflow.
python3 scripts/check.py backend tests/integration/test_import_execution.py
```

The default backend selection covers authentication, queue execution/idempotency,
request fulfillment, and secret storage. Bare `pytest` uses this same selection;
`pytest tests` explicitly selects everything. Prefer the wrapper: direct pytest
has a 120-second per-test timeout but no whole-run memory budget or process cleanup.
Unit suites require named files or explicit `--full` in the wrapper.

Install `ffmpeg` and PostgreSQL client tools matching your test server's major version
(`pg_dump` and `pg_restore` are used by restore tests). The URL above is an example;
create a disposable database with your own credentials. Tests clear their dedicated
databases. Never use an installation database.

Run lint and formatting separately when relevant:

```sh
uv run ruff check services tests scripts
uv run ruff format --check services tests scripts
npm --prefix apps/web run format:check
```

See [the test audit](TEST-AUDIT.md) for coverage decisions and resource findings.

### Release checks and test timing

`Publish container` runs application checks and native AMD64/ARM64 container builds
in parallel. Both final runtime images must start successfully against PostgreSQL,
complete migrations, serve their bundled frontend, and contain the packaged catalog.
Release channels change only after every application check, secret scan, and image
smoke test passes. Pull requests and manual check runs use the same
checks and native image smoke tests without publishing. Feature branches are checked
through their pull requests to avoid duplicate push/PR runs. A newer commit cancels
obsolete checks on the same branch or pull request.

Each successful main publish saves a `release-candidate` artifact for 90 days, binding
the image digest to the exact source commit, repository, and package version. A matching
version tag reuses that successful main run's candidate, promoting the same bytes without
repeating builds or tests. If main is still running, the tag waits up to 12 minutes; if
main failed, publication stops. Rerun the tag after repairing/rerunning main. A missing
or expired candidate artifact takes the full checks/build path. API, identity, or manifest
validation errors fail closed.

Container channels have separate owners:

- `dev` is an on-demand, single-architecture homelab build with a startup smoke test.
  It is not a fully validated release candidate. See the fast iteration workflow above.
- `edge` follows the current checked main commit; superseded main runs cannot update it.
- `vX.Y.Z` identifies an immutable stable release. The Git tag must match the backend,
  frontend, and npm lockfile versions. Prerelease versions are not supported by this workflow.
- `latest` follows the newest stable Git release tag. Rerunning an older version does not
  downgrade it. Main builds no longer update `latest` or a shared short-SHA tag.

Only final channel updates are serialized. Promotion verifies that the registry manifest
contains both tested architectures and that every updated tag resolves to the candidate
digest. A version already pointing to another digest is rejected; publish a new version
instead of replacing an existing release. Partial promotion can be retried with the same
candidate. Internal `candidate-<run>-<attempt>` tags retain images for promotion; these are
not installation channels.

Docker uses a separate dependency layer and per-architecture build caches. Frontend
compilation runs on the build host without emulation. Browser checks build the frontend
once per run; CI shares one build between mocked and live journeys. Downloader setup
tests disable only their fake endpoints after
each scenario so recovery scans do not wait on unreachable fixture addresses.

Backend lint, schema, unit, and contract checks run separately from four integration
shards. Each shard uses two pytest workers with independent databases. Shards partition
the collected test IDs deterministically, including parameterized cases, so every case
runs exactly once across the four shards. To reproduce a shard, set
`BOOK_TEST_DATABASE_URL` as above and run:

```sh
uv run pytest tests/integration -q -n 2 --dist=worksteal \
  -p scripts.pytest_shard --ci-shard=1/4 \
  --timeout=120 --timeout-method=thread --max-worker-restart=0 --durations=25
```

CI limits each test (including fixtures) to two minutes. The timeout terminates the
stuck worker and identifies its test; worker replacement is disabled so the shard
fails instead of repeatedly restarting. Integration steps have a ten-minute limit and
their jobs have a twelve-minute limit, leaving time to upload evidence after a step
timeout. Shards finish independently even if another fails, preserving failure evidence.
The `backend` aggregate check requires both the quick checks and every integration shard
to pass. Each shard uploads its own JUnit report and prints its slowest tests in the log.
Local pytest now has the same 120-second per-test limit. The local wrapper adds
a whole-run time and sampled memory budget.

Schema drift is checked by explicitly migrating a fresh database before `alembic check`;
it does not rely on databases created by pytest workers. Dependency caches are keyed by
their lockfiles. Workflow improvements do not suppress failing application tests: a
failed test, timeout, or skipped backend dependency prevents publishing.

Browser tests require a second database ending in `_browser_test`:

```sh
cd apps/web
npx playwright install chromium
BOOK_E2E_DATABASE_URL=postgresql+psycopg://book:example-password@localhost:5432/dewarr_browser_test npm run test:e2e
```

The routine end-to-end command runs three journeys: foundation setup, reader details,
and library discovery. It starts its own API, single-concurrency worker, and mock
integration server. Foundation creates the first account and synthetic connections.
Fully mocked journeys live in `apps/web/tests/ui` and run independently against Vite
preview via `test:ui`; they never start the backend. The remaining live regression
journeys run only when selected or with `test:e2e:full`.

Full suites are explicit and run sequentially:

```sh
python3 scripts/check.py backend --full --timeout 600
npm --prefix apps/web run test:ui:full -- --timeout 600
npm --prefix apps/web run test:e2e:full -- --timeout 600
```

A focused browser rerun may use `--skip-build` if the existing build is current.
Traces are opt-in (`BOOK_TEST_TRACE=1`); failure screenshots remain enabled. Local
Playwright runs stop at the first failure and never retry automatically. CI runs
both complete browser selections and retains the full backend regression suites.
 Goodreads collection pages use bundled snapshots; remote cover images use local responses, so tests do not depend on public services. Fixtures use synthetic credentials. Do not add live tokens, personal reading lists, or private server addresses to fixtures.

## API changes

```sh
uv run python scripts/export_openapi.py
npm --prefix apps/web run generate:api
```

Commit the OpenAPI document and generated TypeScript types together. For database changes, include an Alembic migration and matching tests. Keep API and worker versions aligned.

## Repository hygiene

Commit source, reproducible tests, build tools, and public documentation. Keep installation `.env` files, secrets, database dumps, logs, personal research, generated test output, and internal plans out of Git. Third-party license notices must remain in distributions.
