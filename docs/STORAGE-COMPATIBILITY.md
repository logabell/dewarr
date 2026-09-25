# Storage compatibility: Sonarr/Radarr comparison

Reviewed 2026-09-25. Covers the implemented NAS/library mount behavior, retained legacy routes,
and the boundary between Docker path mapping and filesystem capabilities.

## What the other applications do

Sonarr and Radarr accept existing absolute library paths after a write test. Their
`FolderWritable` implementations write a small test file and delete it. They do
not require the library's reported owner to equal the application's uid or its
directory permissions to be `0700`.

Sources: [Sonarr root folders](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/RootFolders/RootFolderService.cs),
[Sonarr write test](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Common/Disk/DiskProviderBase.cs),
[Radarr root folders](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/RootFolders/RootFolderService.cs),
[Radarr write test](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Common/Disk/DiskProviderBase.cs).

Their Docker guidance favors a shared parent mount and consistent users/groups
for efficient hardlinks and moves. That is a recommended layout, not a reason to
reject every separately mounted download path. Radarr's transfer implementation
attempts a hardlink and falls back to a copy when the link cannot be created.

Sources: [Servarr Docker guide](https://github.com/Servarr/Wiki/blob/master/docker-guide.md),
[Radarr transfers](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Common/Disk/DiskTransferService.cs).

NFS supports mapping requests to a server-side uid/gid, including anonymous
identities. An NFS4 mount reporting staging owner `99:100` while the worker runs as
uid `1000` is consistent with this behavior. Ownership alone does not establish
whether access works, or prove a particular export option is enabled.
Source: [NFS export identity mapping](https://man7.org/linux/man-pages/man5/exports.5.html).

## Dewarr findings

| Area | Current behavior | Assessment |
| --- | --- | --- |
| Numeric ownership | Staging and lock files previously had to match the worker uid. | Fixed: the filesystem authorizes access; lock owners are checked against the staging folder's server-side owner. |
| Saving a folder | The UI previously required a ready downloader before saving. | Fixed in the folder workflow: save first; verify and activate once a client is ready. |
| Download filesystem | Verification attempts hardlinks and falls back to copies. | Already supports downloads on a separate filesystem when publication checks pass. |
| Staging permissions | New routes use normal media permissions and protected app journals. | Synthetic SMB modes no longer need to provide private media staging. Legacy journals keep their original privacy contract. |
| Multiple library mounts | Per-destination staging, with retained historical locations. | Independent shares are supported; each destination tests its own publication route. |
| Mounting only a library | Use a hidden `.book-search-staging` child when sibling staging cannot share the mount. | Supported for Audiobookshelf and Grimmory with watcher disabled plus scan permission. |
| Connections | A compatible Audiobookshelf/Grimmory folder supplies the destination's identity; downloader verification also activates imports. | Keep these distinct in the UI: connected, folder saved, file access verified, imports enabled. A working connection does not prove filesystem access. |

## Implemented NFS behavior

The worker no longer rejects a private staging folder just because NFS reports a
different owner. Route verification still performs writes, reads, locking,
hardlink attempts, and no-replace publication checks. Newly created probe locks
and reused publication/entry locks must have the staging directory's owner, be
regular files, and have one link. Symlinks are not followed. A real permission
denial still fails the operation.

This also applies to import retries, cancellation, and recovery. Recovery scans
remain read-only; they do not create an ownership-test file just to inspect a
journal. No blanket chmod, chown, export changes, or per-mount bypass setting is
introduced.

Tests simulate the server reporting uid `99`, gid `100` while the worker reports
uid `1000`, preserving real file operations and timestamps. They cover route
verification, hardlink/copy publication, existing locks, concurrent exclusion,
retries, cancellation, read-only journal census, and invalid locks. This is not a
live NFS interoperability test.

## NOR-61: probe cleanup failure

The [reporter's traceback](https://github.com/logabell/dewarr/issues/22#issuecomment-5819083856)
ends at the final cleanup exception in `probe_destination`, not at the folder
picker or staging-owner check. The previous code used the same message for an
identity mismatch and any error while deleting the temporary destination folder.
The traceback alone therefore does not identify the underlying filesystem error.

A regression test reproduces the exact `Probe object changed; unrecognized
replacement preserved` exception on the earlier branch with NFS-style deferred
unlink, using real local file operations:

1. The probe retains a duplicate open handle to its marker file.
2. Cleanup unlinks that marker while the handle is still open.
3. A filesystem that retains open, deleted files leaves a hidden temporary file
   in the directory. Linux NFS calls this
   [silly rename](https://kernel.googlesource.com/pub/scm/linux/kernel/git/netdev/net-next/+/8b57b3046107b50ebecb65537a172ef3d6cec673/fs/nfs/unlink.c).
4. Removing the containing directory fails with `ENOTEMPTY`.
5. Cleanup incorrectly labels that error as an unrecognized replacement.

The fix closes retained test-file handles after checking ownership and before
unlinking. It covers both native and fallback publication probes, including a
partially written marker. Unknown or replaced files are still preserved. Genuine
cleanup failures now retain the affected path and error code in `cleanup_failures`;
a cleanup failure no longer hides an earlier operation failure or grants import
activation.

Validation: 196 publication, setup, cancellation, recovery, conversion and seeding
unit tests, plus 19 setup-probe integration tests. The deferred-unlink regression
failed before the fix and passes afterward for native and fallback probes. A live
probe using isolated temporary folders on the available macOS SMB mount stopped
at its reported staging mode `0777`, before reaching cleanup; that was not an NFS
interoperability test. No existing library files were touched.

This is a confirmed code defect at the reported failure location, not proof of
the reporter's exact mount behavior. NOR-61 still needs their mount details or a
successful retest of the updated build before being called confirmed resolved.

### Follow-up evidence after v0.3.1

The [updated report](https://github.com/logabell/dewarr/issues/22#issuecomment-5820160619)
fails earlier, opening `source_root` in `probe_download_folder`, with `ENOENT`
for the `downloads` component. It does not reach library publication or the
previous cleanup failure. The screenshot maps slskd's `/media/downloads` to
Dewarr's `/soulseek/downloads`; the full configured path and container mounts
still need confirmation on that installation. A successful connection test checks
the client API; a saved mapping does not establish local filesystem access.

The [later comment](https://github.com/logabell/dewarr/issues/22#issuecomment-5821329392)
reports successful library verification with qBittorrent and SABnzbd, followed
by download-selection and UI problems. These are separate from the cleanup bug:

- Missing download roots and mapped save subfolders reproduced the new traceback.
  Probe failures now identify the full mapped download path, retain `ENOENT`,
  and direct the administrator to the download-client mapping. Failed probes
  still cannot enable imports.
- The custom select popover was outside the modal's DOM subtree. Chromium also
  blocked pointer clicks there; this is not solely a Zen-browser issue. Menus
  now render inside their owning dialog (or the document body outside dialogs).
- Download feedback in a 1%-width action column could have only 9 pixels of text
  width. Feedback now reserves readable width, and shows the selected candidate's
  existing rejection reasons from the operation response.

The displayed "No eligible release" message is an automatic-selection hold,
not evidence of another missing filesystem path. Its specific rejection reasons
were not provided by the reporter. Eligibility rules and mount mappings are not
changed to guess around missing evidence; NOR-61 remains open pending retesting.

Validation: 55 focused setup, Soulseek-connection and filesystem tests, plus nine
browser journeys. Regression checks cover real pointer selection in a modal,
missing source roots/save subfolders, and readable feedback at 1236px and 390px.

## Proposed storage model — not implemented yet

1. Keep authoritative recovery journals in Dewarr's protected application data
   or database. Media-share directory modes should not determine journal trust.
2. Resolve media staging per destination filesystem. Persist that location in
   each frozen import specification so retries and recovery use the original
   staging area even when another library is configured later.
3. Extend library-only mounts beyond Audiobookshelf once other library servers
   provide reliable scanner and watcher exclusions. NOR-66 implements the
   Audiobookshelf path; hidden names alone are not sufficient for every backend.
4. Use actual operations to determine write, copy, hardlink, rename, and lock
   support. Label hardlinks as an optimization. Report the failing operation and
   path when verification fails, with NFS-specific or SMB-specific guidance only
   when the filesystem is known.
5. Present ordinary setup as library folder plus optional path translation.
   Discover staging automatically, keep advanced paths optional, and preserve
   saved configuration when a connection or capability test fails.

The first two changes must cover planning, publication, cancellation, conversion,
backup/restore, and recovery together. Existing receipts and unfinished imports
must remain recoverable through migration. Removing the current permission or
filesystem checks in isolation would leave the journal design unchanged and
would not provide reliable support for these layouts.

Acceptance coverage for that redesign should include a Docker worker on NFS4
with server-side uid mapping, group-shared NFS directories, SMB with synthetic
permissions, separate ebook/audiobook mounts, downloads on another filesystem,
library-only mounts, disconnect/reconnect, concurrent workers, and process death
before and after publication. Existing books must never be overwritten, and
unknown or replaced files must never be removed during recovery.


## NOR-66: library-only Audiobookshelf mounts

Root cause: the folder picker rejected every automatic staging choice whose
library parent was `/`. Staging selection always fell back to a sibling;
route probes, publication specifications, and combine specifications also
rejected all staging/library overlap. This made an ordinary NAS bind mount such
as `/volume1/CalibreLibrary:/library` impossible to configure.

Automatic selection now uses the exact reserved child `.book-search-staging`
when the library is a mount root, sits directly below `/`, or its parent cannot
host staging. Linux mount information distinguishes bind boundaries even when
`st_dev` is equal. Working staging locations remain in place; existing journal
and unfinished-import guards still prevent silent relocation. Library selection
checks journal privacy and rejects symlinks or unexpected files without replacing them
or changing their permissions. Downloads remain outside library and staging
roots. Publication and part-combine paths cannot address the reserved staging
namespace. Atomic publication and recovery retain the frozen journal location.

Audiobookshelf's [watcher](https://github.com/advplyr/audiobookshelf/blob/d22c468215882dda4511bd1d5b7dd82b260d63bd/server/Watcher.js#L65)
and [scanner file filtering](https://github.com/advplyr/audiobookshelf/blob/d22c468215882dda4511bd1d5b7dd82b260d63bd/server/utils/fileUtils.js#L140)
explicitly exclude dot-prefixed path components. Grimmory's
[scanner](https://github.com/grimmory-tools/grimmory/blob/839e02fc9abca9861385540ae752e981154b1e1a/backend/src/main/java/org/booklore/service/library/LibraryFileHelper.java#L179)
skips hidden directories, but its
[watch registration](https://github.com/grimmory-tools/grimmory/blob/839e02fc9abca9861385540ae752e981154b1e1a/backend/src/main/java/org/booklore/service/monitoring/LibraryWatchService.java#L235)
and event processing do not provide equivalent exclusion. In-library staging is
therefore requires the Grimmory watcher to be disabled and scan permission to be
available. Other software watching the share must exclude the reserved folder.

Focused checks exercise real local filesystem probes, interrupted hardlink/copy
publication, read-only journal census, retry, cancellation, API selection and
activation, full import confirmation, and part combining/separating under both
staging layouts. Bind mount boundaries are simulated; this is not a live NAS
or library-server interoperability test. New routes use the independent storage
implementation described below; legacy routes retain their journal permissions.

## NOR-66: independent media and journal storage

The failure was a path policy, not an invalid NAS setup: the picker rejected `/library`
and assumed writable sibling staging under `/`. A single global staging directory also
made an unrelated library share constrain the selected destination.

Migration `0074_destination_storage` adds per-destination routes without moving files.
Frozen import specifications include the journal root for new routes; legacy fingerprint
and lock behavior are retained. New journals and locks are in protected app storage,
while media preparation and the final no-replace publication remain on the library mount.
Shared `0775`/`0664` media modes follow configurable Docker `UMASK=002`; hardlinked source
inodes are never chmodded. A `022` mask retains group read-only defaults when desired.

Recovery and backup enumerate configured and retained locations. Backups deduplicate
shared journal stores and preserve combine receipts as well as ordinary import receipts;
conflicting receipt identities fail the backup. Restored journals remain in the protected
restore output for reconciliation; this does not relocate media or silently rewrite frozen
paths. Keep every referenced journal store and media mount when restoring.

This aligns with the arr-stack distinction between container paths, root folders, remote
path mappings, and underlying filesystem capabilities. A downloader API connection or a
path mapping does not mount NAS data. A common parent mount enables hardlinks where
supported; separate mounts work through copy fallback. Dewarr retains its stronger
publication ownership, no-overwrite, source preservation, and backend confirmation checks.

Automated checks use real local files with injected mount boundaries and network-style
identity behavior. They are regression coverage, not certification of every NAS export,
CIFS option, or library-server release. The setup probe validates the installed stack.
