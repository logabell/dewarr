# GitHub / Linear issue remediation — October 4, 2026

Scope: the 11 open GitHub issues in `logabell/dewarr` that are mirrored in
Linear. Unrelated Linear roadmap work is excluded by Logan's clarification.
Baseline: `449b220`, Dewarr v0.3.7. Three existing modified files covering linked
download identifiers are preserved; they are not attributed to this task.

## Investigation and implementation plan

| GitHub | Linear | Evidence / next action | Closure threshold |
| --- | --- | --- | --- |
| #27 | NOR-64 | Origin validation already accepts the configured HTTPS public origin over HTTP upstream; diagnostics shipped in v0.3.3. Recheck proxy regression and outstanding runtime evidence. | Reporter configuration/header mismatch still unconfirmed; retain if no reproducible new defect. |
| #31 | NOR-68 | Soulseek selected-request language fix shipped in v0.3.3; follow-up is generic failure screenshots. Inspect images and current dispatch/review path; test controlled journey. | Do not equate historical fixes with confirmation of the remaining live failure. |
| #32 | NOR-69 | Prowlarr redirects, SAB completion and NZB import shipped in v0.3.5. Audit/test acquisition and discovery diagnostics. | Limited-results symptom needs same-query/indexer comparison. |
| #33 | NOR-70 | Backend has enabled state; download-client UI lacks control and disabling must preserve observation of current jobs. | Implement toggle plus selection/dispatch and active-job regressions. |
| #34 | NOR-71 | HTTP torrent redirects and missing-indexer diagnostics shipped in v0.3.5. Test actual torrent acquisition path. | Missing torrent inventory still needs reporter evidence. |
| #37 | NOR-74 | Rename/disable and self-service OIDC linking shipped in v0.3.5; account removal/provider unlink remain. | Implement remaining account-management paths without deleting history or locking out admins. |
| #49 | NOR-82 | New opt-in shared-library behavior, escaped braces, narrator metadata fallback. | Track co-located ebook copies safely in each audiobook folder and verify both import orders. |
| #50 | NOR-83 | Identity parser permits only year/retail suffixes; Prowlarr omits language. | Bounded trailing-label parser with positive and false-positive boundary coverage. |
| #51 | NOR-84 | Inspect three attached screenshots and exact book query; compare adapter behavior with v0.3.7. | Reproduce and fix observed cause; distinguish live source access limitations. |
| #52 | NOR-85 | VIP eligibility, EPUB/MOBI manifest handling, and import-review navigation can dead-end. | Fix legitimate paths without bypassing identity, file or authorization safeguards. |
| #53 | NOR-86 | Promotional invitation, not a product defect. | Eligible for closure as not planned; no code change. |

## Execution and validation

Individual implementation agents use GPT-6 Astra at high reasoning. Shared-file
ownership is coordinated, and all tests are serialized by the primary agent using
`python3 scripts/check.py`. Start with focused boundary tests and API/worker
journeys, then exercise changed UI flows. No Paperclip tests, parallel full suites,
or raised budgets. Review all final diffs for P0/P1/P2 issues, fix findings, and
rerun the affected checks. Record exact results and limitations below.

Issue closure is based on complete evidence, not the presence of a related release.
Locally fixed issues remain distinct from released fixes. Production deployment
and release publication are not part of this request.

## Results

### Confirmed root causes

- **#31 / NOR-68:** Soulseek was missing from `resolve_candidate`; its result
  fell through to Prowlarr torrent/NZB resolution. The old Soulseek test mocked
  that resolver, concealing the defect. Added the existing Soulseek file-list
  artifact path and removed that mock. Seven real resolver API journeys pass.
- **#50 / NOR-83:** Only year/retail suffixes were allowed after catalog
  title/author pairs. Added a conservative trailing metadata allowlist and
  explicit unambiguous language extraction. Unknown content qualifiers still
  require review. 163 parser/adapter cases pass.
- **#51 / NOR-84:** The supplied screenshot's error is reproduced by a live
  AudiobookBay results page containing a base64-encoded `.post.re-ab` posting.
  The previous parser aborted the whole page. Static bounded decoding now
  preserves existing link validation, returns nine results including the
  reported book, and passes 54 adapter cases. No site JavaScript is executed.
- **#52 / NOR-85:** VIP authorization depended on an undocumented expiry field;
  an EPUB plus matching MOBI companion was unnecessarily rejected; and a held
  automatic import without a handoff disappeared from Review and lacked its
  existing inspection link. Tracker authorization, narrow companion handling,
  and owner-scoped review routing fix these paths. Held administrator-selected
  source releases also expose the existing manifest review flow directly,
  preserving request context and dedicated collection review. The EPUB/MOBI worker journey
  imports only EPUB and preserves source files. 47 final focused cases pass.
- **#33 / NOR-70:** Existing backend enabled state was absent from the UI;
  ordinary saves also invalidated active transfer bindings. State-only updates
  now retain configuration and credential generation, block new downloads,
  and continue observing existing transfers. 73 client/transfer cases and two
  completed-import approval cases pass. Actual automatic-worker continuation
  passes with enabled and disabled clients; 20 seeding-rename cases pass.
- **#49 / NOR-82:** Escaped-brace naming and selected native-MAM narrator fallback
  are implemented; 33 Python and four frontend boundary cases pass. Tracked
  co-location now uses a durable receipt and bounded worker, preserves ebook
  identity through inventory refresh, supports either import order and multiple
  narrations, and rechecks current destination authorization. Six placement unit
  cases, five integration cases, and nine inventory cases pass. The two
  import-order cases were extended and rerun to verify interrupted-budget
  continuation and refusal to recreate a moved target folder.
  Limits: canonical ebook folders remain; no automatic backfill on enable; one
  file per supported format from the oldest matched standalone ebook edition;
  partial staging files are held for inspection; narrator fallback is from a
  selected native MAM release, without Prowlarr-to-MAM cross-enrichment.
- **#37 / NOR-74:** Added unused-account deletion and OIDC-provider unlink, with
  explicit confirmation, fresh revision/permission checks, alternate sign-in
  validation, session revocation, and self/last-admin safeguards. Accounts with
  history retain it and can instead be disabled or renamed. Existing rename,
  disable, and linking were already released. 24 account/OIDC cases pass; the
  account-management browser journey passes.

### Already released / still unconfirmed

GitHub's current `main` and latest release were rechecked directly: `449b220`
and v0.3.7. None of the older reports below is safe to close solely because a
related fix shipped.

- **#27 / NOR-64:** External HTTPS public-origin login over HTTP upstream works
  with both preserved and rewritten Host. Relevant behavior is unchanged since
  v0.3.3. Four proxy API cases pass. Keep open for actual proxy-to-app Origin,
  effective configuration and a request-correlated rejection diagnostic.
- **#32 / NOR-69:** Validated Prowlarr redirects and SABnzbd acquisition/import
  shipped in v0.3.5. Limited results need a same-query/category/indexer comparison.
- **#34 / NOR-71:** Torrent redirects and missing-indexer diagnostics shipped in
  v0.3.5. Changing download clients does not change the available source protocols.
  Keep open pending enabled/searchable book-capable torrent indexer inventory.
  The current controlled Prowlarr journeys and 28 SAB/discovery scenarios pass.
- **#53 / NOR-86:** Promotional solicitation. Eligible to close as not planned;
  no product remediation needed.

### Review findings fixed during implementation

- An independent review caught a proposed MAM change that would skip explicit
  freeleech protection when VIP expiry was unknown. Reverted that behavior;
  explicit wedge requests retain existing protection.
- The new no-handoff review journey initially returned an empty review queue.
  Fixed the request filter as well as the inspection link, then reran the journey.

- Account deletion initially relied too heavily on restrictive foreign keys;
  added explicit checks for cascade-owned history before deleting unused users.
- Ebook placement now rechecks current routes, graph/version identity, matched
  source/target paths, and physical destination ownership. Conflicting or moved
  targets are held instead of overwritten or recreated. Independent target
  failures do not starve later placements. Hash/copy work has bounded budgets
  and a durable continuation; inventory verification shares a per-item budget.
- Source review could replace the explicitly selected request with an older
  work-scoped receipt and route held collections into single-release review.
  Corrected precedence and extended the existing browser journeys.

### Validation record

All tests ran serially through `python3 scripts/check.py`, using focused selections
with the workstation's normal time, concurrency, and memory limits. No full
suites or Paperclip tests ran. Representative final results:

| Area | Result |
| --- | --- |
| Release identity/language parser and Prowlarr adapter | 163 passed |
| AudiobookBay adapter | 54 passed, plus exact live page replay |
| Soulseek selected requests with real artifact resolver | 7 passed |
| MAM and held automatic import/review paths | 47 passed |
| Download clients, attempts, and Soulseek connection | 73 passed |
| Disabled-client import approvals / worker continuation | 2 + 2 passed |
| Seeding rename | 20 passed |
| Accounts and OIDC | 24 passed |
| Naming and native MAM narrator fallback | 33 passed |
| Co-location placement and integration | 11 passed; 2 extended cases rerun and passed |
| Companion inventory, including credential rotation and deadline | 9 passed |
| SAB and Prowlarr discovery | 28 passed |
| Default bounded backend smoke | 21 passed |
| Initial changed account/client/naming/request/source UI selection | 8 passed |
| Updated naming/source UI selection, fresh production build | 2 passed |
| Final source/collection UI selection, fresh production build | 4 passed |
| Final frontend naming boundary selection | 4 passed |

API schema was regenerated; TypeScript and Vite production builds pass. Python
lint and format checks pass for all 45 changed/new Python files owned by this
task; whitespace checks pass. The original patch for all three pre-existing
modified files was compared byte-for-byte and is preserved exactly.

Tests use controlled external-service responses and a fresh task-owned local
PostgreSQL database. They do not establish the state of reporters' installations.
The AudiobookBay reproduction additionally used the actual public search page.
No live torrent/NZB/Soulseek transfer or authenticated reporter configuration was
used.

### Remaining remediation and closure decisions

- **#27:** Obtain effective public-origin settings and the rejected request's
  Origin/Host/forwarded headers with its rejection diagnostic. Reproduce the
  mismatch against that configuration before further code changes.
- **#32:** Compare the same query, categories, enabled indexers, and result/error
  counts directly in Prowlarr and Dewarr; inspect any failing download's request
  and SAB event. Current evidence does not justify a speculative search rewrite.
- **#34:** Inventory configured torrent-capable book indexers and compare the
  same query/protocol filters directly. qBittorrent configuration alone cannot
  add source results.
- **#31, #33, #37, #50, #51, #52:** Locally fixed and regression-tested; eligible
  for closure after release and confirmation of the reported scenario.
- **#49:** Implemented with the explicit co-location/narrator boundaries above.
  Confirm those boundaries meet the reporter's intended library organization
  before closure; retaining the canonical ebook folder can leave a separate
  Audiobookshelf ebook entry.
- **#53:** Eligible for closure as promotional/non-product content.

Migration `0077_ebook_companions` is required with deployment. Its downgrade
refuses to remove populated placement receipts. New co-location remains opt-in.

No outstanding actionable P0/P1/P2 findings remain in the reviewed changes.
Remote GitHub/Linear statuses, releases, and production deployment were not
changed. All implementation changes are local and uncommitted.

## Second P0/P1/P2 review pass

Requested after the initial implementation report. Independent GPT-6 Astra/high
reviews revisited account lifecycle, review navigation, source parsing, download
state, ebook placement, and inventory identity. Six P2 findings were identified:

1. Ebook placement locked Library before Integration, opposite to inventory and
   ordinary import execution. Lock Integration first, then refresh/lock Library;
   verify concurrent census can still acquire its library lock.
2. Disabled clients were still advertised as needing connection repair by the
   request and download projections. Observation-only checks now disregard the
   enabled flag while retaining credential-generation and route validation.
3. Account removal used the live refetched revision rather than the revision
   reviewed when confirmation began. Freeze the reviewed revision; deliberate
   reload refreshes account details and sign-in methods and resets confirmation.
4. Held-release inspection did not forward an explicitly selected freeleech
   wedge. Carry the explicit choice through the existing checked MAM inspection
   path, rejecting that choice for other sources.
5. A catalog title ending in a language word, such as The Good German, could be
   assigned the wrong language when followed by an Audiobook label. Determine
   the catalog title boundary before interpreting bare language suffixes; keep
   structured source language authoritative.
6. Companion inventory queried current and legacy item IDs together, risking
   uniqueness failures and mixed identity evidence. Prefer the exact current ID
   and use legacy identity only as a fallback.

All six findings are fixed. Final focused runs through the serialized bounded
runner passed:

| Second-pass validation | Result |
| --- | --- |
| Disabled-client observation versus changed credentials | 2 passed |
| Real import context, settings/routes, and census lock contention | 3 passed |
| Language/parser/profile/automatic eligibility | 262 passed |
| Manual acquisition selection | 23 passed |
| Source searches and artifacts, including explicit wedge/cache/retry | 53 passed |
| Current/legacy companion inventory and real rename | 11 passed |
| Account confirmation, held review, and collections browser journeys | 6 passed |

Total: 360 passing checks across these selections. An initial new unit fixture
omitted a required field; it was corrected before the final passing run. Fresh
TypeScript/Vite production build, Python lint/format, and whitespace checks pass.
Schemas were regenerated for the optional inspection wedge parameter. Original
user changes remain byte-for-byte intact. The task-owned PostgreSQL test server
was stopped after validation.

Independent follow-up review and primary review found no remaining actionable
P0/P1/P2 findings in the reviewed changes. This is a scoped code review and
controlled regression validation, not verification of reporters' live services.
All changes remain local and uncommitted; no release, deployment, or remote issue
status was changed.

## Third P0/P1/P2 review pass

A fresh pass reassigned review areas between GPT-6 Astra/high agents and focused
on retry behavior, authorization, inventory-cache reuse, and in-flight UI state.
Eight confirmed P2 findings were fixed:

- Library scan failures after successful companion placement were not retried:
  already-present rows were skipped on the job's next attempt.
- Transient publication-lock contention was treated as a permanent held copy
  instead of using the worker's existing retry policy.
- Explicit MAM wedge inspection lacked the download-approval permission check
  enforced by the normal selected-release download path.
- Inventory cached only the backend's selected primary ebook media, losing
  tracked copies known through libraryFiles on subsequent cached scans.
- Disabling an import destination incorrectly invalidated existing verified
  companion ownership despite unchanged storage and bytes.
- An account deletion/unlink pending inside the profile dialog did not block
  parent cancellation; its late completion could close another account editor.

- Disabled Soulseek connections could show stale failure/attention messages and
  health warnings instead of their intentional disabled state.
- Disabling Soulseek while a provisional enqueue was in flight prevented
  rollback, leaving an unaccepted transfer running. Cleanup now permits a disabled
  connection only when its identity, generation, and endpoint still match the
  actual enqueue connection; changed/replaced/deleted connections are not touched.

The scan-retry fix also covers the original ebook disappearing or its source
route being disabled between a failed scan and retry. Scan-only recovery reads
present target receipts independently of source eligibility; new publication
still requires current authorized source and destination evidence. Temporary
publication-lock contention uses the existing bounded worker retry policy.

The explicit wedge permission check now matches medium-specific download
approval. Tests verify denied callers make no additional tracker request.
Account removal now blocks profile edits, Cancel, and Escape while pending and
guards completion callbacks after unmount. Inventory keeps its existing cache
optimization while recording tracked nonprimary ebook observations; actual file
removal remains detectable.

Final focused validation through `python3 scripts/check.py`:

| Third-pass validation | Result |
| --- | --- |
| Wedge authorization, cached artifact, and repeat-spend boundaries | 4 passed |
| Companion inventory and real census/cache/removal regressions | 17 passed |
| Source-status and account-management browser journeys | 3 passed |
| Soulseek selection and provisional enqueue rollback | 11 passed |
| Ebook placement/import order/lock/scan retries | 9 passed |

Total: 44 passing checks across final selections. The earlier seven-case
placement run also passed before the scan-source edge cases were added; it is
not counted twice. Fresh TypeScript/Vite production build, Python lint/format
for all 51 owned changed/new Python files, and whitespace checks pass. The
original three user-modified files remain byte-for-byte unchanged.

No P0 or P1 issues were identified. Final review found no remaining actionable
P0/P1/P2 findings in the reviewed changes. Live reporter services remain
unverified. All changes remain local/uncommitted; no issue statuses, releases,
or deployments were changed.


## Fourth review and UI/UX walkthrough (October 4–5)

Two additional existing P2 defects were reproduced and fixed:

- Stale downloader/Soulseek settings forms could re-enable a client another administrator had disabled, because enable-only updates correctly preserve credential generation. Saves now compare the reviewed enabled-state baseline under the settings lock. Forms refresh their baseline when the saved state changes; existing transfers keep their generation. Optional API baseline fields preserve compatibility for older API callers.
- Ebook companion placement could retain an earlier destination object after acquiring its database lock. The locked query now refreshes that object's attributes, so concurrent path changes or revoked verification prevent filesystem placement.

The actual application ran against an isolated local PostgreSQL database with generated sample accounts, catalog records, and disabled demo connections. No worker ran and no real external downloads or provider changes were made. The in-app browser walkthrough covered all twelve administrator settings categories, expanded download-client and Soulseek editors, library folders, naming previews, account access/detail dialogs, and book-to-source navigation. Mobile inspection used a 390px viewport; expanded Soulseek, naming, and account dialogs fit without horizontal page overflow.

Implemented UI improvements:

- Visible earlier/more arrows expose overflowing settings categories without adding a navigation dropdown; arrows disappear when all categories fit. Existing deep links and one-row tab styling remain.
- Source/format selectors and sorting appear only when results offer useful choices. Unknown format, seeder counts, and size sorts require corresponding data. Invalidated selections stop filtering immediately and reset safely when results refresh.
- An empty source configuration no longer also suggests trying a different search.
- OIDC/Plex new-account access controls appear when registration uses them. Independent review caught an OIDC exception during this change: group mapping also uses the role for existing passwordless users. That case now retains a clearly labeled Default access control and fallback explanation.
- Unsaved account edits explain why unlink/delete actions are disabled; draft cancellation retains the existing discard confirmation.
- Client capability implementation flags were replaced with concise actionable limitations. Deluge Label-plugin guidance no longer appears for unrelated clients or unknown capability state.
- Soulseek's primary Save action comes before Test/Delete.
- Storage reserve uses the same disclosure styling and lazy mounting as Download recovery, retaining drafts when collapsed.
- The server-wide MP3-to-M4B option appears under Everyone on this server, avoiding a global change under Only me.

Validation through the bounded serialized runner:

| Selection | Final result |
| --- | --- |
| Downloader/Soulseek stale availability saves | 6 passed |
| Concurrent destination edit during real companion placement | 1 passed |
| Settings navigation (admin/member/viewer) | 3 passed |
| Downloader connection/mapping/toggle journeys | 3 passed |
| Source status and Soulseek setup | 2 passed |
| User access, removal, and provider role drafts | 3 passed |
| Naming layouts | 1 passed |
| Source filtering, Quick add, and held download review | 1 passed |
| Collection review variants | 3 passed |

Total: 23 distinct passing checks. Fresh TypeScript/Vite production builds, focused Python lint, frontend formatting, and whitespace checks pass. Early UI runs exposed test setup issues (resize completion, viewer tabs fitting without arrows, missing required mock display name, and an exact label locator); these were corrected without extending timeouts or increasing workers. Final account/source changes were rebuilt and rerun successfully.

Browser screenshots from the final mocked journeys are retained under `/tmp/dewarr-ux-audit-20261004/screenshots/`. Manual review used the real isolated app; source result and held-artifact states were exercised by browser journeys with controlled fixtures because live connections were deliberately disabled. External provider connectivity and reporter-specific deployments remain unverified.

No P0/P1 found; final independent review found no further actionable P0/P1/P2 in this pass's reviewed changes after the OIDC correction. The original three user-modified files remain byte-for-byte unchanged. All work remains local and uncommitted; no GitHub/Linear statuses, releases, or deployments changed.

The temporary browser, API process, and task-owned PostgreSQL instance were stopped after validation.
