# Library folders and mixed-format libraries

Architecture review for NOR-66, 2026-09-25.

## Decision

A destination is a format-specific route into an existing library. Ebook and audiobook routes may use one root and one staging location, or independent mounts. Their verification, supported formats, import preferences and catalog identities remain separate. No new storage service or database table is needed.

User-chosen directory names have no semantic meaning. `/collection`, `/Reading Room`, and `/shelf-42` are equally valid library mount points. A top-level container mount is not the host filesystem root. Paths reported by another container are a separate namespace: `/remote-collection` in a library server can map to `/shelf-42` in Dewarr. The existing create/observe/remove challenge proves that mapping; matching path strings alone does not.

An import still follows one pipeline:

1. Discover the downloader's completed files through its saved path mapping.
2. Inspect file formats and match catalog versions; do not infer medium from root names.
3. Freeze and preview names, source identities and metadata.
4. Prepare a complete version folder on the destination mount, hardlinking when the verified route supports it and copying otherwise.
5. Publish without replacing an existing folder, then ask the library server to scan and confirm the exact files/version before declaring ownership.

A read-only download mount is valid for copy imports. Seeding rename remains an explicit advanced operation with different write requirements.

## One root versus one book folder

These are distinct capabilities. [Audiobookshelf supports audio and ebook files in one book folder](https://audiobookshelf.org/docs/documentation/libraries/book-library/ebooks/). It selects one primary ebook, with other ebooks supplementary. [Its directory model](https://audiobookshelf.org/docs/documentation/libraries/book-library/directory-structure/) uses an item folder to group a book's files. [Grimmory's organization modes](https://grimmory.org/docs/library/organization-modes/) likewise distinguish grouping by file from grouping by folder. [Servarr's import model](https://github.com/Servarr/Wiki/blob/master/radarr/quick-start-guide.md) separates completed downloads from the final media library and imports via copy, hardlink or move.

The recommended immediate Dewarr behavior is a shared root with distinct version folders:

```text
/shelf-42/
  .book-search-staging/       # only if sibling staging is unavailable
  Alex Morgan/
    First Harbor (Ebook)/
      First Harbor.epub
      metadata.opf
    First Harbor (Audiobook)/
      First Harbor.m4b
      metadata.opf
```

The existing naming profile still supplies author, series, year, narrator and edition details. When an enabled ebook route and audio route share a local root, the naming planner adds Ebook/Audiobook labels to new version folders targeting that root. This also appears in naming and import previews. It prevents cross-format collisions when the title/author are identical and edition or narrator metadata is absent. Automatic imports already know their destination; manual review asks for it before planning only when available destinations require different folder names. The selected destination carries forward into publication. Existing files are not renamed, and unrelated libraries and pending plans keep their original names. A plan must be reviewed again if its actual destination changes between shared and separate storage.

This preserves the existing whole-directory publication, recovery and cancellation contract. Existing mixed-format folders remain readable by the backend and Dewarr's existing discovery; this change does not split them or append files to them. Different editions/recordings retain their catalog identities. An occupied destination is held for review, never overwritten.

Automatically adding a missing format to an existing book folder is a separate future capability. Matching a title or matching generated names is insufficient authorization to merge. It needs a verified work/version association, a lock shared by both routes, file-level receipts and rollback, deterministic sidecar/cover ownership, no-overwrite writes, backend rescanning, and preservation of primary ebook/reading state. Multiple recordings, abridgments, translations and supplementary PDFs make automatic title-based merging ambiguous. Implementing that inside the current whole-folder publisher would undermine the requested stability; the shared-root feature does not claim that behavior.

## Staging and mount boundaries

Staging must share the library's mount so complete folders can be published safely. Prefer a writable sibling. If the selected directory is a mount root, its parent is unavailable, or the parent is on another mount, use Dewarr's reserved direct hidden child. Only this private namespace is name-specific; user library and download names are unrestricted. New routes keep journals/locks in application storage, independent of media permissions.

Two routes using the same local root reuse the same valid staging route. A saved automatic staging choice that now overlaps the selected library incorrectly is reselected. An explicit `BOOK_IMPORT_STAGING_ROOT` override remains authoritative and receives a path-specific error if invalid. Unfinished imports continue to block changing their saved storage; historical journals are retained.

Re-saving an existing route preserves its journal location, including legacy journals kept in staging. Another route using the same staging folder must not silently switch that recovery protocol.

Dewarr must not publish into downloads or let downloads contain staging. Checks report both paths and configuration keys, with the relevant setting to inspect. Mount boundaries and permissions are verified by actual operations. A sibling library folder with a similar name is not an overlap. Linux bind mounts can share a device number and still disallow rename/hardlink operations, so device identity alone is insufficient.

For internal staging, Audiobookshelf's hidden-folder exclusion is the supported route. Grimmory's existing integration guard still requires its watcher to be disabled and scan access enabled, or staging outside the watched root. This change does not relax that guard.

## NOR-66 evidence and diagnostic limit

The latest comment reports first-save rejection with `/library` and `/downloads`, both destinations unset, and no explicitly described staging override. In the current code, `/library`, `/downloads`, and `/library/.book-search-staging` pass the overlap predicate. The comment alone therefore does not establish which additional effective path caused the reported 422. Do not claim the reporter's exact installation is repaired without that evidence.

The old error collapsed every configured download-root and staging conflict into one sentence and did not log the compared paths. The new error and warning log identify the actual conflicting locations, including persisted/environment roots not visible as a selected destination. This is preferable to guessing that a NAS folder name needs special treatment or silently deleting saved configuration.

### Latest-comment checklist

| Reported point | Coverage and remaining evidence |
| --- | --- |
| First save, both destinations unset | API journey removes the initial destination and saves both routes from scratch. |
| Arbitrary NAS/container library folder name | Validation and staging follow paths and mount boundaries. Tests use unrelated names, including spaces. No `/library` or `/audiobooks` allowlist. |
| `/downloads` mapped directly to `/downloads`, read-only | Route verification and both-format publication cover a downloader saving directly at its mapped root; copy succeeds and preserves source bytes. |
| Dewarr and Audiobookshelf use different paths | Existing mapping challenge and scan confirmation validate different local/backend paths. The UI reuses both values together. |
| No user-created staging or incoming directory | Staging is chosen automatically, including the reserved direct child at a mount root. No user-created incoming folder is required. |
| Could another configured path be compared? No useful log | Rejection identifies both effective paths and keys and logs the reason, including a regression for a hidden configured download root. |
| One ABS library/root for ebooks and audiobooks | Supported, with shared status, one staging route and separate version folders to preserve atomic publication. Appending into the same existing book folder remains outside this change. |
| The exact v0.3.3 422 on the reporter's NAS | Still unconfirmed: the reported paths alone pass validation. The effective conflicting configuration and a successful save on that deployment remain necessary evidence. |

## Setup experience

Keep two format cards because format eligibility and automation settings can differ. Offer “Use the same folder as …” when the other format's backend library supports this medium. Label both saved cards “Shared · Ebooks + Audiobooks.” Show the library-server path and Dewarr's mapped path where they differ. Put automatically managed staging under an expandable detail, not a required setup field. Keep per-client verification/copy status and actionable path-specific errors.

## Verification

Focused API/worker journeys cover first saves at arbitrary named mount roots, different backend/local names, a shared root, one shared staging route, both media with colliding base metadata, hardlink and read-only/copy routes, exact backend confirmation, and unchanged source bytes. Existing route-picker, mount/publication and naming checks cover independent mounts, forbidden overlaps, watcher restrictions, interruption, cancellation and source preservation. UI checks cover reusing the other format's mapping and displaying shared status.

The follow-up reviews found and corrected three P2 regressions: installation-wide shared naming affected unrelated libraries/plans, re-saving a legacy route could select a different journal location, and manual review's refresh action retained stale destination choices after a configuration change. Regression journeys cover destination-specific naming, preservation of legacy recovery storage, and recovery from changed destinations. No P0 or P1 finding was identified in the reviewed changes. This is local validation, not verification of the reporter's deployment.

The UI recovery follows [TanStack Query's guidance to explicitly refresh known-stale server data](https://tanstack.com/query/latest/docs/framework/react/guides/query-invalidation). The storage design follows [Servarr's distinction between folder structure, container mappings and mount boundaries](https://raw.githubusercontent.com/Servarr/Wiki/master/docker-guide.md). The UI review uses [Web Interface Guidelines](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md): semantic controls, keyboard focus, contained modal scrolling, restrained status styling and wrapping long paths. Screenshots cover 1440px desktop and 390px mobile layouts, shared-folder setup, expanded staging and long path names. Dewarr currently has one dark theme; native control styling is checked under both system light and dark preferences.

Local review validation: 184 distinct focused backend/unit and release-pipeline checks and 12 browser checks passed, along with the production frontend build, Ruff and whitespace checks. The browser journeys include carrying the chosen shared destination from manual naming review into the import request. Backend checks reject switching to a destination that needs different names before publication, and eight automatic acquisition journeys reach confirmed library ownership. All tests ran serially through `scripts/check.py`; no full suite was launched.
