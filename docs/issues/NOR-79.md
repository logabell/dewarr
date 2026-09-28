# NOR-79: make library setup an access check

Issue: https://linear.app/northernlogic/issue/NOR-79/libraries-for-audiobooks-folder-verification-failed

## Finding

The common architectural problem is treating connection setup as certification
of the entire publication/recovery mechanism. A usable share could fail a
synthetic exercise that did not match an ordinary import. Each filesystem-specific
fix then added more probe machinery and more ways for setup to fail.

| Report | Requirement or assumption involved |
| --- | --- |
| NOR-28 | NFS rejected native no-replace rename; a fallback was added. |
| NOR-56 | DrvFs handles behaved differently after moving a probe directory. |
| NOR-61 | Probe directory identity changed across a rename. |
| NOR-66 | Staging placement required access outside a valid library-only mount. |
| NOR-67 | Windows SMB refused a directory rename with an open child file. |
| NOR-78 | Backend marker visibility was made a condition of local import readiness. |
| NOR-79 | A staging operation returned EACCES; the message omitted its actual path. |

NOR-78 already removed remote marker verification. NOR-79 still encountered the
local probe, which created private test files/directories, tested journal claims,
file locking, native/fallback rename collision semantics, and marker identities.
Its media tests requested 0600/0700 despite actual media publication using
0666/0777 subject to umask and ACLs. That distinction matters on shares with
mapped ownership and group access.

The NOR-79 report proves an EACCES during the old staging phase, not the
reporter's underlying ACL, mount, or ownership configuration. We have not accessed
that deployment. A genuinely unwritable library will still require an access fix.

## Comparison with the arr applications

Inspected upstream source on 2026-09-28:

- [Radarr root folders](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/RootFolders/RootFolderService.cs),
  [Sonarr root folders](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/RootFolders/RootFolderService.cs), and
  [Lidarr root folders](https://github.com/Lidarr/Lidarr/blob/develop/src/NzbDrone.Core/RootFolders/RootFolderService.cs)
  validate an existing root and writability.
- [Radarr's writability check](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Common/Disk/DiskProviderBase.cs)
  writes and deletes a small file.
- [Radarr path mappings](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/RemotePathMappings/RemotePathMappingService.cs)
  translate remote paths into local paths. They do not establish shared storage
  through a media-server challenge.
- [Sonarr imports](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/MediaFiles/EpisodeFileMovingService.cs)
  request hardlink-or-copy during the actual transfer. Filesystem capabilities
  are exercised by the work that needs them.

The useful principle is separation: API authentication, local folder access,
and importing a completed download are different operations. Setup should only
require what it can usefully establish at that point.

## Applied changes

- Replace the simulated publication with small create/write/delete checks in
  library and staging folders. No setup lock, fsync, journal-claim, directory
  rename, collision, inode-rebinding, or marker-cleanup protocol remains.
- Use ordinary media permissions, honoring umask and inherited share ACLs.
- Require only download readability. An optional temporary source file checks
  hardlink availability; read-only downloads and failed hardlinks select copy.
- Remove the `no_replace` receipt requirement from route activation and release
  selection. Existing successful receipts remain valid; no database migration,
  compatibility mode, or new user setting is needed.
- Put new Audiobookshelf staging inside the chosen library. Grimmory can use this
  when its watcher is disabled; Bookdrop and watched libraries retain external
  staging. Existing valid locations and explicitly configured overrides remain.
- Report the failed operation, folder path, errno, and worker identity.
- Delete tests devoted to the removed simulation. Keep actual import tests for
  source preservation, occupied destinations, interruption, and network behavior.

Actual import recovery remains in the import implementation. This change removes
its duplicated setup rehearsal; it does not rewrite saved import journals or
change the handling of an interrupted real import. Journals still use Dewarr's
application storage for new routes, rather than private permissions on media.
A connection check is no longer a promise that every later filesystem operation
will succeed. A later import failure belongs to that import and is reported there.

Dewarr still chooses hardlink/copy from the access check, unlike Sonarr's per-file
hardlink-or-copy transfer. This change does not claim to replace the full transfer
engine or eliminate all historical recovery complexity.

For a previously failed folder, use **Save & verify folder** after installing the
change. No database reset or recursive chmod/chown is part of this fix.

## Validation

285 focused checks passed through `python3 scripts/check.py`: 170 filesystem and
publication checks, 95 API/worker setup and import checks, and 20 automatic
acquisition, Bookdrop, and changed-configuration checks. They cover SABnzbd,
qBittorrent, slskd, read-only downloads, group-mapped media, shared roots, successful
setup without lock/rename support, actionable write/delete failures, existing
receipts, and actual source-preserving imports. Retired tests only exercised the
removed setup protocol; actual publication/recovery tests remain.

Ruff lint/format checks and `git diff --check` passed. Database journeys ran
serially against a dedicated disposable PostgreSQL database, removed afterward.
The reporter's NAS and a real NFS/SMB server have not been tested directly.

## Follow-up review and root-cause fixes

A deeper review reproduced three P2 issues. No P0 or P1 was identified in the
reviewed connection/publication paths. All three now have targeted fixes:

1. **Actual hardlink failures fall back to copying the affected file.** A setup
   sample cannot establish whether downloader-owned files are linkable. Linux's
   [protected-hardlink rules](https://docs.kernel.org/admin-guide/sysctl/fs.html#protected-hardlinks)
   are one example: readable files can still reject a hardlink with EPERM.
   Publication now copies on permission, cross-device, unsupported-operation,
   or link-count-limit errors. Unexpected I/O, capacity, and collision errors
   remain visible. Files that successfully link retain that optimization.

   The existing receipt records the per-file choice so interruption, resume,
   verification, and cancellation use the correct transfer semantics. The
   existing capacity reservation grows before copying; completed copies do not
   count again as outstanding work. If space is unavailable, the import waits
   and retries. No source permissions or ownership are changed.

2. **Direct setup uses the same mount validation as the folder picker.** The
   worker calls the existing library-route validator, preventing an explicitly
   configured or remounted staging path on another mount from being marked
   ready. Publication still needs staging and library on one mount for its
   final folder rename. No setup rename/collision rehearsal was restored.

3. **Setup and import agree about legacy journal storage.** That shared
   validator also resolves the false-ready state for custom legacy staging
   with co-located journals and permissive modes. Existing private journal
   requirements are checked before setup reports success. New routes continue
   to keep journals in protected application storage and accept ordinary shared
   permissions on media staging. Existing journals are not moved or discarded.

Regression coverage includes actual-file EPERM after successful verification,
mixed hardlink/copy publication, copy admission with insufficient space and
subsequent retry, interruption at copy creation/staging/publication,
cancellation without touching downloads, and direct setup with mismatched
mounts or invalid legacy journal permissions. Permission and mount failures are
simulated; the reporter's NAS has not been tested directly.

Validation passed through the bounded runner: 142 filesystem/publication checks,
144 publication/cancellation/conversion checks, 71 setup/execution journeys, and
31 capacity/cancellation/Bookdrop journeys (these selections overlap). Ruff and
`git diff --check` passed. The dedicated PostgreSQL test database was removed.
