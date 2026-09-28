# NOR-78: simplify library folder verification

Issue: https://linear.app/northernlogic/issue/NOR-78

## Findings and design

Local `dev` and fetched `origin/dev` pointed to `2027df5` when this investigation
started. Both already included the five-second visibility retry from `de7cd35`.
The underlying requirement remained: setup and each publication had to prove a
shared mount through an absent/present/absent remote marker challenge.

That requirement couples local import readiness to remote filesystem-cache
behavior and permissions for an API unrelated to importing local files. ABS's
[path check](https://github.com/advplyr/audiobookshelf/blob/v2.36.1/server/controllers/FileSystemController.js)
requires upload permission. Its [filesystem helper](https://github.com/advplyr/audiobookshelf/blob/v2.36.1/server/libs/fsExtra/path-exists/index.js)
also returns false for access errors. Grimmory's path listing requires additional
library-management access. NAS lookup caches can delay visibility, as described
in the [NFS manual](https://man7.org/linux/man-pages/man5/nfs.5.html).

The report does not provide enough environment detail to distinguish these
installation-level triggers. The architectural defect is making this separate
remote-marker protocol a prerequisite for local import, when Dewarr already
validates local publication and confirms the actual book through the backend.

[Radarr](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Core/RemotePathMappings/RemotePathMappingService.cs)
and [Sonarr](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Core/RemotePathMappings/RemotePathMappingService.cs)
use explicit path translation and local access checks. Their local writability
checks create and delete a small test file
([Radarr](https://github.com/Radarr/Radarr/blob/develop/src/NzbDrone.Common/Disk/DiskProviderBase.cs),
[Sonarr](https://github.com/Sonarr/Sonarr/blob/develop/src/NzbDrone.Common/Disk/DiskProviderBase.cs)).
These checks do not require a separate media server to observe a marker.

## Final implementation

- Remove remote marker creation, observation, deletion polling, and the adapter
  endpoints used exclusively for that protocol. No optional probe mode or new
  setting replaces them.
- Keep the existing local access/publication checks, explicit backend root
  selection, library access, supported media/settings checks, and protection
  against settings or credentials changing during validation.
- New receipts record `configuration_validated`, not an unproven `root_mapping`.
  One compatibility reader accepts previously successful receipts, so working
  installations do not require reconfiguration or a data migration.
- Actual imports retain safe local publication and source preservation. Backend
  detection still determines completion: an undetected book remains pending,
  then is held with mapping/access/scan guidance. Correcting the mapping allows
  detection to resume without publishing a duplicate.
- ABS, Grimmory, and Bookdrop use their existing import/detection workflows.
  Direct backend upload permission is no longer needed solely for setup. Existing
  scan, metadata, Bookdrop, and staging requirements still apply to operations
  that actually need them. Optional qBittorrent seeding-rename checks are unchanged.

A wrong but locally writable mapping can now pass setup and be diagnosed when
the actual book is not detected. Setup therefore means local access and backend
settings are valid; it does not claim the two services share a filesystem.
This is the intentional tradeoff of explicit mappings and actual-item confirmation.

After installing, run **Save & verify folder** for an affected failed route.
Previously failed receipts are not silently approved. Existing successful
receipts remain usable while their configuration and credentials remain current.

## Validation

**151 focused tests passed** through the serial, bounded `scripts/check.py`
runner: 50 adapter/path/receipt checks, 83 setup/publication/acquisition/combine
checks, and 18 Grimmory/Bookdrop checks. PostgreSQL-backed journeys used a
dedicated disposable database, removed afterward.

Coverage includes setup without remote filesystem permissions; invalid library
roots and credentials; local write/copy/hardlink failures; current and legacy
receipts; an actual simulated backend mount mismatch remaining unconfirmed;
mapping correction without republishing; overdue detection guidance; protected
and nested staging; and Bookdrop review/linking and crash recovery. Focused Ruff
lint/format checks and `git diff --check` passed. The reporter's live NAS was not
accessed.
