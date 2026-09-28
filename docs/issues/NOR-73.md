# NOR-73: missing audiobook library folders

Issue: https://linear.app/northernlogic/issue/NOR-73/library-folder-option-has-disappeard

## Findings

The v0.3.4 screenshot shows the picker’s “Connect Audiobookshelf or Grimmory”
message, which is rendered when the folder API returns an empty list. It does
not show a folder parsing error. The report contains no server logs or edit
history, so the reporter’s exact trigger cannot be confirmed.

A reproducible cause exists in `api/integrations.py`: every connection update
incremented the credential generation, cancelled the inventory lease, and set
all its libraries to `accessible=False`. This included changing a display name,
changing the browser-facing URL, or submitting unchanged credentials. Folder
discovery only returns accessible libraries. Inventory publication is responsible
for restoring accessibility after a complete sync. Consequently, a harmless
edit hid folders, invalidated destination verification, and could interrupt the
sync needed to bring them back. New connections awaiting their first sync have
the same empty-list presentation.

## Research and implementation plan

1. Scope invalidation to changes that affect access: server URL, actual secret
   values, and enabled state. Preserve the generation, active sync lease, and
   accessibility for display-only edits. Keep the existing locked generation
   check and retain full invalidation when access changes.
2. Read connection state separately in the empty picker. An empty folder list
   does not establish that no server is connected. Audiobookshelf’s current
   [library controller](https://github.com/advplyr/audiobookshelf/blob/master/server/controllers/LibraryController.js)
   filters library discovery by the authenticated account’s permissions, so
   recovery guidance must also account for permission and configuration issues.
3. Reuse the existing idempotent sync API and display operation progress/errors.
   Refresh folder choices, saved folder settings, and connection state after
   completion using targeted query invalidation, following
   [TanStack Query’s guidance](https://tanstack.com/query/latest/docs/framework/react/guides/invalidations-from-mutations).
   Retain a manual refresh for syncs started elsewhere. Do not make inaccessible
   libraries selectable or manufacture accessibility from a connection test.
4. Verify through focused API and browser journeys using `scripts/check.py`.

## Changes

- Connection edits now invalidate library access only when access inputs change.
- The empty picker distinguishes absent, disabled, and configured connections;
  offers per-server sync and refresh; displays sync failures; and reloads folder
  options after completion.
- A one-line `RouteFields` type alignment accepts the nullable library ID already
  introduced by the concurrent native-import changes, allowing the frontend to
  build without changing destination selection behavior.

Existing installations with hidden libraries recover through **Sync** in the
picker. A running worker and a successful inventory sync are still required.
No data migration or production deployment is part of this fix.

## Validation

- Result: **8 API tests and 6 browser tests passed**. The frontend production
  build and focused Ruff checks passed.
- API regression cases cover name/browser URL edits, unchanged/replaced tokens,
  endpoint changes, disabling a connection, lease and route preservation, initial
  discovery, and recovery of previously hidden folders through real inventory
  synchronization against a synthetic Audiobookshelf HTTP fixture.
- Browser cases cover a connected server with missing audiobook folders,
  failed sync followed by successful recovery, and absent/disabled connections.
- Tests use a dedicated disposable PostgreSQL database and the serialized,
  bounded workstation runner. Upstream behavior is simulated; the reporter’s
  Docker installation was not accessed.

## Follow-up QA fixes

The review found two P2 recovery gaps, both now fixed:

- Sync status uses `GET /api/integrations/{integration_id}/sync/{operation_id}`
  instead of searching the current user's latest Activity entries. Only admins
  can read it, and the operation must be a library sync for that connection.
  This supports coalesced jobs owned by another admin and jobs outside the
  latest 100 Activity entries. Activity visibility remains unchanged.
- Status lookup failures stop automatic polling, show recovery guidance, and
  allow refresh or retry. A retry invalidates cached status even when the server
  returns the same shared operation ID.
- If the saved folder is unavailable, the picker displays its selector even
  when only one compatible replacement remains. Saving requires explicit
  selection, and the submitted local/backend paths reflect that selection.

Focused validation: `tests/integration/test_library_connection_edits.py`
(8 passed) and `settings-library-recovery.spec.ts` (6 passed), both through
`scripts/check.py`. This includes shared sync progress/completion, authentication,
admin-only access, wrong-connection and wrong-operation-kind rejection, old jobs,
404/503 recovery, and a real browser save of the sole replacement folder.
The frontend production build, focused lint, and formatting checks passed.
