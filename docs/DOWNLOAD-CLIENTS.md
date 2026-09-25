# Torrent download clients

Settings → Download clients supports qBittorrent, Transmission, and Deluge, alongside
the existing Usenet clients. Save and test a connection, map its download folder to a
folder the worker can read. Torrent and Usenet sources use their respective default
client; Soulseek uses slskd. A sole enabled client of a type is the automatic default.
With multiple clients of the same type, choose the default in Settings → Download clients.

Settings → Libraries selects the final ebook and audiobook folders. These folders are
shared destinations, independent of which client downloaded the content. Saving and
verifying a library folder checks every ready client's download path and does not change
client defaults. Each path must support safe hardlinking or copying into the library.
If one path fails, its named error is shown while other verified paths remain usable.
Successful connection tests and saved download-folder mappings automatically queue checks
for missing or outdated library routes. Settings → Libraries shows each client's path,
verification result, and last successful check. **Verify again** remains available and
rechecks the saved folders directly, without reopening setup or changing import preferences.
A connected client is not marked as library-ready until its folder check succeeds.
Completed downloads are inspected, then hardlinked or copied into the selected media folder;
the client's download folder remains separate.

| Capability | qBittorrent | Transmission | Deluge Web UI |
| --- | --- | --- | --- |
| Versions | 5.x | 3.0+ / 4.x legacy RPC | Connected Deluge daemon |
| Credentials | Web UI username/password | RPC username/password | Web UI password |
| Categories | Native categories | Labels | Existing Label plugin label |
| Attempt identity | Tag + full hashes + path | Label + v1 hash + path | Unique artifact folder + v1 hash + path |
| In-client seeding rename | Supported when enabled | Unavailable in Dewarr | Unavailable in Dewarr |
| Sequential/first-last controls | Not exposed by Dewarr | Unavailable | Unavailable |
| Magnet metadata inspection | 5.2+ | Separate resolver required | Separate resolver required |

Transmission performs the session-id handshake without retrying uncertain submissions.
Deluge must already be connected to its daemon through its Web UI. If a category is
selected, enable Label and create that label in Deluge first. Dewarr reads the label's
completed folder (or daemon defaults) and submits into an isolated `dewarr-<artifact ID>`
subfolder there. Move-completed and automatic management are disabled only for these
new transfers. Existing transfers and global client settings are never rewritten.
The isolated folder also keeps grouped selections for one artifact on the same route.

The new adapters support torrent-file and magnet submission and pause/resume. The
planner currently requires inspectable torrent metadata; a source offering only a
magnet still needs the existing metadata resolver. Full v2 identity verification is
not implemented for these clients, so the planner explicitly refuses v2/hybrid
artifacts instead of dropping a hash claim. Use copy/hardlink destinations with
Transmission and Deluge; seeding-copy rename remains qBittorrent-only.

A durable external-may-exist marker is committed before add. Following a worker crash
or lost response, the worker only observes the recorded transfer and never adds again.
A pre-existing hash, mismatched folder/category, or ambiguous result is held for review.
A Deluge crash before labeling/resuming may therefore leave a paused transfer requiring
review; it does not cause another add. Completed client files still require inspection,
import, and library confirmation before a request is fulfilled.

Adapters are covered by mocked RPC contract tests and database-backed worker lifecycle
tests. These tests do not certify a particular live server installation.

Protocol references: [Transmission RPC](https://github.com/transmission/transmission/blob/4.0.6/docs/rpc-spec.md),
[Deluge Web API](https://deluge.readthedocs.io/en/latest/reference/webapi.html), and
[Deluge core API](https://deluge.readthedocs.io/en/latest/reference/api.html).
