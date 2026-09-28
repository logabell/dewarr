# Native library import workflows

Dewarr offers native workflows in **Settings → Libraries → Choose folder**. A download client's save folder remains separate from library and intake folders.

| Workflow | Metadata and naming | Availability |
| --- | --- | --- |
| Audiobookshelf library scan | Dewarr organizes the initial files and writes OPF. ABS scans them. Keep `absMetadata` last (highest priority), with OPF after folder and audio tags. | Confirmed after ABS reports the complete item. |
| Grimmory direct library import | Dewarr organizes files and submits initial metadata. Grimmory's global **Move files to library pattern** must be off. Choose **Use independent copies** when Grimmory writes embedded metadata for that medium. | Confirmed after Grimmory reports the item and accepted metadata. |
| Grimmory Bookdrop review | Dewarr copies one EPUB, preserving its original filename and contents, into a unique intake subfolder. No OPF, cover export, final naming template, or metadata update is applied. Grimmory owns review, final library selection and organization. | Delivery remains pending review until an administrator links the actual imported library copy. |

Grimmory's organization mode is chosen when creating a library. Direct imports require a library created with **Book per folder**. Dewarr reads compatibility settings during route verification and before publication, and checks persistence again before its metadata update. It does not change Grimmory or ABS settings. Avoid changing backend organization settings during imports or assigning competing organizers to the same physical file tree.

## Bookdrop setup and review

Select **Bookdrop review** for the Grimmory connection and enter the Bookdrop folder configured on that server. If Dewarr sees it under another mount, choose the matching local path. Keep the intake separate from downloads and final libraries. Staging must be outside the watched intake, on the same filesystem for atomic publication; mount their common parent or configure `BOOK_IMPORT_STAGING_ROOT` appropriately. Even a hidden staging folder inside Bookdrop is unsupported.

Activating Bookdrop changes the administrator's personal ebook destination. It preserves the installation's shared library default so member downloads can continue using their accessible libraries. Settings displays the signed-in administrator's effective destination.

The connection needs access to Bookdrop and its queue. Folder verification checks local publication access, the selected backend configuration, and queue readability; it does not prove that the two services share a filesystem. Grimmory does not expose its configured Bookdrop root through this API, so Dewarr cannot prove that the chosen path is watched until a delivered EPUB appears in the queue. **Refresh review status** checks for that exact intake path and size.

Review metadata and approve the import in Grimmory. Sync the resulting library in Dewarr, then use **Link reviewed copy** in the download's import review. Candidates must be complete, matched copies of the selected edition on the original Grimmory server. Resolve an edition mismatch in library review before linking. Queue disappearance alone does not establish whether a file was imported, discarded or moved.

**Mark rejected** retains the receipt and suppresses another handoff; it does not delete anything in Grimmory. A confirmed or rejected handoff is never automatically resent. Publication receipts also protect against a restart after Grimmory consumes a file but before Dewarr records delivery. Uncertain outcomes require review rather than automatic replacement. Pending human review does not occupy a worker or trigger repeated scans.

This first Bookdrop workflow supports administrator-owned, single-EPUB books. Audiobooks, multi-file books, other ebook formats and collections remain on direct import or manual review. Member downloads require a final library whose access can be checked. Bookdrop uses the existing inspection, copy, capacity reservation and journal machinery; it does not introduce a second metadata editor or automatically finalize Bookdrop imports.

## Direct import recovery

Before submitting metadata to Grimmory, Dewarr records the exact detected book in the import receipt. After a lost response or restart, it reads that book again and checks its library, filenames, completeness and metadata. Independently copied formats that Grimmory was authorized to rewrite must remain present and nonempty; other files still require their original hashes. Grimmory may report cached file sizes after a metadata write, so recovery does not depend on those sizes being refreshed. A successful metadata update is not sent again. An undelivered update can be retried only while the original media hashes still match. Unrecorded changes or a moved or mismatched book remain held for review.
