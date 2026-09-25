# Test and workstation resource audit — 2026-09-25

## Findings

The workstation had a substantial resource problem independent of Dewarr: 363
orphaned Paperclip process-session launchers and 11 children were still running
from temporary test workspaces roughly 44 days old. Their summed resident memory
was about 3.1 GiB. A second pass inspected working directories and found another
13 orphaned Paperclip fixture HTTP servers whose inline Node commands did not
contain a Paperclip name. All **387 confirmed obsolete processes** were stopped;
none required SIGKILL. No Paperclip launch agent or crontab was found. The user's
retirement policy is now recorded in global and workspace agent instructions.
Active unrelated services were preserved. Summed RSS is not a measurement of
unique physical memory, and this snapshot cannot prove the cause of past crashes.

Dewarr also had avoidable test overhead and lifecycle risks:

| Finding | Change |
| --- | --- |
| Bare pytest collected 3,508 cases: 1,661 unit, 1,743 integration, 104 contract | Routine selection now has 21 cases, mostly API/worker journeys; full collection is explicit |
| Every browser spec started PostgreSQL-dependent fixtures, three Python processes and foundation setup | 34 mocked specs (74 cases) use static preview with no backend or development API proxy |
| Request ledger mocked its actual data but still bootstrapped/signed in | Converted to a standalone mocked journey, retaining the existing assertions and in-progress user changes |
| Local pytest only dumped a stack after five minutes; it did not enforce a timeout | 120-second timeout per test including fixtures; bounded runner also limits the whole run |
| No protection against concurrent local suites | One advisory lock per checkout; one test worker; lower process priority |
| No aggregate memory/watchdog limit | Default five-minute deadline and 2 GiB sampled RSS budget, including discovered detached browser groups |
| Browser children launched before the cleanup block and through extra `uv` wrappers | Direct interpreter launches inside cleanup scope; any child failure stops the stack; kill is followed by wait |
| Browser worker used production concurrency of four | Fixture worker uses one job at a time with a two-second graceful shutdown limit |
| Database fixture disposal could be bypassed by teardown errors | Engine disposal and cache clearing are in `finally` blocks |
| Every passing browser journey recorded a trace | Trace recording is opt-in; failure screenshots remain on |
| Synthetic audio encoders could choose their own thread counts | Fixture FFmpeg uses one encoder/filter thread and no interactive stdin |
| Frontend unit command manually enumerated files and missed `titleLabels.test.ts` | Discovery includes all six current test files, executed serially through the bounded runner |

Existing logs showed locator failures, interrupted filesystem calls during browser
runs, and a backend restore-test timeout. Those failures are not sufficient evidence
that a particular test exhausted RAM. The confirmed fixes target the observed orphan
processes, unnecessary fixture stack, and missing execution limits. The full backend
suite previously took about six minutes in a saved log; that was a different test
count and is not a controlled before/after benchmark.

## Coverage retained and removed

Removed four implementation-string tests from `test_request_status_filters.py`:
downloading SQL structure, library SQL structure, title-sort SQL structure, and
committed-selection SQL structure. API tests in `test_request_activity.py` exercise
real downloads, library filtering, permissions, and title ordering. Retained the
cheap unit checks for projection pagination and status precedence.

Removed `test_browsing_and_mapping_require_admin` from `test_slskd_connection.py`.
Its sole assertion was identical to `test_download_folder_browser_is_admin_only`
in `test_downloaders.py`, but it unnecessarily constructed an additional fixture.

Removed the opt-in `screenshots.spec.ts`. It tried to bootstrap an account after the
foundation project already created one, waited fixed delays, and captured images
without comparison assertions. Updated the screenshot documentation accordingly.

Moving the 33 originally fully mocked specs changes only their fixture/schema import
paths. The request-ledger conversion is the additional 34th mocked spec. Full browser
coverage is now 74 mocked cases plus 41 live cases; routine checks select nine mocked
and three live journeys. Backend safety, adapter, migration, filesystem, race,
permission, and recovery tests remain available explicitly. They were not deleted
solely to reduce a count: replacing cheap boundary cases with more browsers would
increase resource use and lose useful coverage.

The complete backend remains approximately 3,500 cases. The large reduction is in
what runs routinely, not a claim that thousands of valid regressions were removed.
CI continues to run full backend suites and both complete browser selections. The
three new watchdog tests (four collected cases) protect timeout cleanup,
parent-exit cleanup, detached-child memory accounting, and repeated interruption.

## Routine commands

```sh
# Nine UI journeys, no DB; includes one frontend build.
npm --prefix apps/web test
# Three live journeys: setup/library connection/worker, discovery, reader actions.
BOOK_E2E_DATABASE_URL=... npm --prefix apps/web run test:e2e
# 21 API/worker/security cases.
BOOK_TEST_DATABASE_URL=... python3 scripts/check.py backend
# Focus on the changed area.
python3 scripts/check.py unit tests/unit/test_request_status_filters.py
npm --prefix apps/web run test:ui -- request-ledger.spec.ts --skip-build
```

Use `--list` to inspect selection and `--full` only for a deliberate comprehensive
run. See [Development](DEVELOPMENT.md#checks) for setup and CI details.

## Verification

All validation ran serially against synthetic data. Fresh audit databases were
created separately from existing development/browser databases, then removed.

| Selection | Result | Runner elapsed | Peak sampled RSS |
| --- | --- | --- | --- |
| Initial mocked UI selection, including nine library-setup cases | 17 passed | 71.1 s, plus 6.8 s build | 1,046 MiB; build 880 MiB |
| Final routine UI selection, including isolated request ledger | 9 passed | 35.6 s with current build | 1,049 MiB |
| Routine live E2E selection | 3 passed | 26.1 s with current build | 1,604 MiB |
| Routine backend selection | 21 passed | 13.0 s | 625 MiB |
| Targeted API/import/status and watchdog checks | 11 passed | 13.6 s | 328 MiB |
| Real audio conversion, status/security and initial watchdog checks | 11 passed | 11.5 s | 231 MiB |
| Frontend unit tests, including the formerly omitted title-label tests | 26 passed | 0.5 s | Too short for reliable sampling |
| Final watchdog selection, including repeated interruption | 4 passed | 13.4 s | 201 MiB |

TypeScript checking, changed-file Ruff/Prettier checks, and full test collection were
also checked. A second bounded invocation correctly refused to start during an
active run. Full regression suites were collected but not executed during this audit;
the focused results are not a claim that every retained test currently passes.

The memory watchdog polls every half-second and is not an OS hard limit. Very short
peaks or children that detach and exit/reparent between samples can evade sampling.
An already-running PostgreSQL service and unrelated applications are outside its
budget. Direct runner commands can bypass the wrapper; project instructions and
documented/npm entrypoints use it by default. These POSIX controls target macOS/Linux.

## v0.3.3 release CI alignment

The release workflow explicitly selects `tests/unit`, `tests/contracts`, and all
`tests/integration` cases across four deterministic shards; the small pytest
default selection does not restrict CI. Current collection finds 3,707 backend
cases, 80 mocked UI cases in 38 files, and 41 live browser cases in 26 files.
These are discovery counts, not a claim that all cases passed.

Mocked UI and live browser checks now run as independent jobs with separate
evidence artifacts. The UI job starts only static preview, with no PostgreSQL or
Python backend. Live journeys exclude `tests/ui`, retain foundation setup, and
use the isolated backend/database fixture. Both use the bounded runner with one
worker. The existing `browser` status requires both jobs to pass.

Release validation updated obsolete expectations for short-title queries and
removal of redundant series terminator requests, fixed probe mocks to forward
new journal arguments, isolated cached origin overrides, and installed the fake
browser clock before application timers. No failures were skipped or given
longer timeouts. The same checks found a real missing ORM flush in capacity
aggregation and a migration comparison bug for literal regex/index expressions;
both were fixed, retaining a check that real index drift is detected.
