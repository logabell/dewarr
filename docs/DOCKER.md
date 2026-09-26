# Docker setup

Start with the [README examples](../README.md#quick-start). A normal install uses two containers: Dewarr and PostgreSQL. The Dewarr image includes the web app, background worker, migrations, and health check.

## Settings

Edit these values directly in Compose or pass them with `docker run -e`.

| Setting | Default | Purpose |
| --- | --- | --- |
| `PUID` | `1000` | User ID that owns your config and media folders. Find it with `id -u`. |
| `PGID` | `1000` | Group ID for those folders. Find it with `id -g`. |
| `TZ` | `Etc/UTC` | Your time zone, such as `America/New_York`. |
| `PUBLIC_URL` | `http://localhost:8000` | The exact browser address, including port. Use your server's hostname or IP for LAN access. |
| `DB_HOST` | `postgres` | PostgreSQL hostname reachable from the container. |
| `DB_PORT` | `5432` | PostgreSQL port. |
| `DB_NAME` | `dewarr` | Database name. |
| `DB_USER` | `dewarr` | Database user. |
| `DB_PASSWORD` | Required | The same password configured for PostgreSQL. |
| `BOOK_DOWNLOAD_DISPATCH_ENABLED` | `true` | Allow downloads after configuring clients, library routes, and policies. Set to `false` to disable downloads server-wide. |

To use another external port, change `8000:8000` to, for example, `8085:8000`, then set `PUBLIC_URL=http://your-server:8085`.

Most configuration belongs in the app: connect libraries, reading accounts, sources, and download clients through **Settings**. Downloads are enabled by default once the required setup is complete; no environment change is needed. If you previously set `BOOK_DOWNLOAD_DISPATCH_ENABLED: "false"`, remove that override or change it to `"true"` and recreate the container.

## Folders

| Mount | What it stores |
| --- | --- |
| `./config:/config` | Dewarr's encryption key and protected import journals. Keep it with database backups. |
| `./data:/data` | Shared downloads and library files. Replace `./data` with your existing media parent folder. |
| `postgres:/var/lib/postgresql` | PostgreSQL 18 data in a persistent Docker volume. |

Dewarr initializes `/config` and runs the app as `PUID:PGID`. Give that user/group read/write access to each library. Download folders can be read-only to Dewarr for copy imports, while the download client uses its own writable mount. Setup normally creates and removes an owned test file to check hardlinks; if source writes are denied, it checks folder readability and destination publication and selects copy mode. Individual completed files are still inspected and verified before import. Seeding-rename routes require source write access. Imports in copy and hardlink modes preserve original downloads. `UMASK` defaults to `002`: new media folders use `0775` and copied/generated files use `0664`, subject to the share's ACLs. Set `UMASK=022` for group read-only files. Use a common `PGID` (or Compose `group_add`) and consistent downloader permissions when several services need write access. Dewarr never recursively changes media ownership or permissions; hardlinks retain the source inode's permissions.

Dewarr chooses staging separately for each destination. It prefers `.book-search-staging` beside the library when that is writable and on the same mount. For a library-only mount such as `/library`, it uses `/library/.book-search-staging`. Ebook and audiobook libraries can live on independent NAS shares. Each staging directory must support safe publication into its own library; downloads can be on another filesystem and use copy mode.

Folder names are arbitrary; the checks use locations, permissions and mount boundaries. Ebooks and audiobooks may share the same library root. Choose **Use the same folder as …** when setting up the second format. Dewarr labels the shared connection and reuses its staging location. New naming plans add Ebook/Audiobook labels to separate version folders; existing books are not moved or automatically merged. See [Library folder architecture](LIBRARY-FOLDERS.md) for the distinction between sharing a root and combining formats inside one book folder.

New routes store journals and locks in `/config/import-journals`, configurable with `BOOK_IMPORT_JOURNAL_ROOT`. Media staging can use the share's normal permissions. Keep the journal folder private (`0700`) on a persistent filesystem supporting file locks; it must sit outside download, staging and library roots. API and worker processes must share this storage. Native installations default to `.local/import-journals` under the working directory; create its parent or set an absolute path with an existing parent.

Existing routes retain their saved staging and journal locations, including the private `0700` requirement for legacy co-located journals. Folder changes do not move or delete historical records. Finish or cancel unfinished imports before changing their storage. `BOOK_IMPORT_STAGING_ROOT` remains an explicit staging override; without it, the picker selects staging for the chosen library. Old journal locations remain part of backup and recovery evidence.

Audiobookshelf ignores the hidden staging child. For Grimmory, library-only mounts require the library watcher to be disabled and Dewarr's account to have scan permission: its recursive scanner skips hidden folders, but its watcher does not reliably exclude them. With the watcher enabled, expose a shared parent and keep staging beside the watched library. Apply the same exclusion to any additional service watching that library.

Use the same paths in Dewarr, qBittorrent, and your library server (Audiobookshelf or Grimmory) when possible. Grimmory's image is `grimmory/grimmory` and listens on port 6060. For example:

```text
/data/downloads
/data/audiobooks
/data/staging
```

Choose those folders in Settings and verify your library routes. If Audiobookshelf sees `/audiobooks` while Dewarr sees `/data/audiobooks`, set that mapping in Dewarr. If qBittorrent sees a different path for the download folder, map that path in **Settings → Download clients**. A shared parent mount allows hardlinks when the filesystem supports them. When the download and library folders are on different filesystems, Dewarr copies the files into the library instead. A library folder can instead ask qBittorrent to rename the seeding files into that folder, so the seeding file and the library file are the same copy. That option stays off unless you turn it on.

### Choosing folders and path mappings

In **Settings → Libraries**, choose the library folder first. If Dewarr sees that folder at a different path, choose **Other folder** to browse the volumes mounted inside Dewarr. The browser shows directories, not files on your computer. Select the corresponding existing folder; this does not move your library. **Save & verify folder** checks filesystem operations and library access before activating the route.

Each download client keeps its own download folder. Finished books use the destination configured under **Libraries**, separately for ebooks and audiobooks. Dewarr automatically uses the only configured destination for the selected library and media type, so an additional destination default is unnecessary. If several destinations remain, choose one in **Settings → Download preferences**. Clearing that override returns to automatic folder selection; it does not disable imports. Disabled, deleted, and inaccessible destinations are excluded, and the selected download-to-library connection must still pass verification. Switching download clients does not select a different final library folder.

Being in the same Compose stack does not guarantee matching paths: each service has its own volume configuration. No translation is needed when both containers see the same files at the same path. A path mapping only translates a path; it cannot mount a missing volume or enable hardlinks.

On WSL2 Windows-backed mounts (DrvFs), hardlinks can be unavailable even when both folders are on the same filesystem. Dewarr automatically uses copy mode when hardlink checks fail and safe publication checks pass. Original downloads remain available for seeding. A failed publication or permission check still blocks activation, and the error identifies the failed operation; changing a path mapping cannot repair filesystem capabilities.

### Soulseek / slskd

Soulseek uses one slskd connection for both searching and downloading. Configure it under **Download clients** or **Download sources**; saving tests the connection automatically. Dewarr reads slskd's completed-download folder and recognizes the same mounted path, including folders already saved through setup. There is no separate “worker import root” to configure for a shared path.

If slskd reports `/media/downloads` but Dewarr sees those files at `/data/downloads`, open Soulseek's **Advanced · path mappings** under Download clients and browse to the corresponding Dewarr folder. Then choose and verify your library folder. Downloads are enabled by default, but connection health and folder access must pass their checks. Recovery mode still pauses dispatch.

### Network shares (NFS and SMB)

NFS and SMB/CIFS libraries require staging on the same mounted share as the library. Dewarr tests native no-replace rename on the actual mount; support depends on the server, client, and filesystem. When the native operation is unavailable, Dewarr tests a fallback. The directory fallback refuses existing targets and relies on ordinary rename refusing non-empty directories; unlike native no-replace, it cannot eliminate a race with another app creating an empty target directory. Keep other writers out of Dewarr's staging area.

On Windows SMB shares, an open child file can prevent its parent directory from being renamed. Dewarr closes its probe marker before publication and reopens it at the destination to verify ownership. Native-capable routes do not need to pass fallback-specific rename tests. A real access denial or busy-file error still fails verification with the operation and error code; it is not treated as evidence that native rename is unsupported.

For a Linux VM mounting Unraid over NFS, pass the VM's mounted media tree into
Dewarr as a Docker bind mount. NFS can map the container's uid to a server-side
owner: a worker running as uid `1000` can legitimately see its staging folder and
new files owned by `99:100`. Dewarr accepts this mapping and verifies actual file
operations. You do not need to change `PUID` just to match the reported NFS owner.
Legacy lock files must belong to the same server-side owner as their journal folder. New routes keep locks in protected app storage.
If access is denied, check the NFS export's permissions and identity mapping;
the `uid` and `dir_mode` options below apply to SMB/CIFS, not NFS.

SMB/CIFS mounts need a few options, because the share, not Linux, decides ownership and permissions:

- Dewarr needs read/write access to the share. Where Unix ownership is not provided by the server, `uid=<PUID>,gid=<PGID>` can make the mount accessible to the container user. Dewarr validates operations rather than requiring that the reported uid equal `PUID`.
- New routes allow ordinary shared media permissions, including synthetic `dir_mode`/`file_mode` settings. Their journals live in `/config/import-journals`; do not impose `dir_mode=0700` on the whole media share for Dewarr. Legacy routes with journals on the share still need private journal-folder permissions.
- `serverino` (the default): Dewarr tracks files by inode number. With `noserverino` those numbers can change between checks. The route test warns when it sees this.
- File locks are tested on the journal filesystem. For new routes, fix `/config` access if this check fails; changing SMB media lock options is unnecessary.

Shares without hardlink support, such as many NAS SMB exports, fall back to copying.

### mergerfs and pooled storage

Mount the pooled parent once, such as `/mnt/user/data:/data`, and choose library and download
folders below `/data` for efficient hardlinks. Separate download and library mounts are supported
with copy fallback, but cannot guarantee hardlinks. Do not mix a mergerfs pool path with one of its underlying branch paths. A single
shared view lets mergerfs place hardlinks on the same branch and matches the path layout recommended
for Sonarr and Radarr.

mergerfs can report a different directory inode after a rename because its default `hybrid-hash`
mode derives directory identities from their paths. Dewarr supports that behavior: route tests and
interrupted imports use private random ownership markers while a directory moves, then record the
identity at its final library path. File hardlinks are still verified independently. See the
[mergerfs inode calculation documentation](https://github.com/trapexit/mergerfs/blob/master/mkdocs/docs/config/inodecalc.md)
for the available policies and their tradeoffs.

## Existing PostgreSQL

Remove the `postgres` service and Dewarr's `depends_on` section, then set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` to your existing database. Dewarr waits for the database and applies migrations before starting.

Create the database/user first. The user must be able to create tables and schemas. Use a hostname reachable from the container; `localhost` refers to the container itself.

## Optional .env

Inline settings are sufficient. To keep the database password in one place, put this in a private `.env` file:

```dotenv
DB_PASSWORD=your-chosen-password
```

Then use `DB_PASSWORD: ${DB_PASSWORD}` in Dewarr and `POSTGRES_PASSWORD: ${DB_PASSWORD}` in PostgreSQL. Leave ports, user IDs, and folders inline. `.env` is ignored by Git.

Existing `BOOK_DATABASE_URL`, `BOOK_SECRET_KEY`, and `BOOK_SECRET_KEY_FILE` overrides remain supported for advanced installations. Prefer the simpler settings above for new installations.

## HTTPS / reverse proxy

Proxy to port 8000 and set `PUBLIC_URL=https://books.example.com`. Secure cookies are enabled automatically for HTTPS. Keep the browser's original Host header. A proxy on the Compose network can use `http://dewarr:8000` as its upstream.

Form submissions accept the request's own origin (scheme, hostname and port) or the configured `PUBLIC_URL`. Origin validation also accepts direct LAN addresses. However, an HTTPS public URL enables Secure cookies, so use HTTPS for sign-in: ordinary HTTP LAN access cannot retain those cookies. For a proxy that terminates HTTPS or rewrites the Host header, set `PUBLIC_URL` to the external browser address and recreate the container. Forwarded host/protocol headers do not authorize additional origins. `PUBLIC_URL` also remains the canonical address for identity-provider redirects and automatic secure-cookie configuration.

For client-IP attribution, set `BOOK_PROXY_TOKEN` to a long random value. The proxy must send that value in `X-Dewarr-Proxy-Token`, set `X-Real-IP` to the connecting client, and append that client to `X-Forwarded-For`. When a header is repeated, the last value is the one that counts. Sign-in limits use that client when those two addresses agree, or when only one is present. If they disagree, Dewarr keeps the connection's own address, so a visitor-supplied address cannot replace the one the proxy appended. Requests without the token keep that connection address too, including visitors who open port 8000 directly.

```nginx
proxy_set_header Host $host;
proxy_set_header X-Dewarr-Proxy-Token your-long-random-token;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

### Proxy identity and sign-in limits

HTTPS termination does not require forwarded-protocol headers. Dewarr accepts the
configured public origin even when the upstream request is HTTP. Keep Uvicorn's
`--no-proxy-headers` setting so Dewarr can identify the actual connection peer;
this also applies to native installations using the options below.

Without trusted client-IP configuration, everyone behind the same proxy shares
its 15-attempt/10-minute password sign-in budget. Successful attempts also count.
A 429 response includes `Retry-After`; a 403 origin rejection is a different problem.

The proxy-token setup is supported from v0.3.2. The following IP/CIDR option
and diagnostic command are available from v0.3.3.

If your proxy cannot inject `X-Dewarr-Proxy-Token`, explicitly trust only its
connection address (or a dedicated proxy network):

```yaml
environment:
  PUBLIC_URL: https://books.example.com
  BOOK_TRUSTED_PROXY_IPS: '["192.168.2.1/32"]'
```

Use the proxy's address as seen by the container, which may differ from its public
address due to Docker networking. The value is a JSON list. Trust is disabled by
default; wildcards and all-address networks are rejected. Avoid a shared Docker
subnet containing unrelated containers. Firewall direct access as appropriate.

The trusted proxy must append its connecting client to `X-Forwarded-For`, or
replace the header with an accurate chain. Dewarr walks that chain from right to
left, stopping at the first untrusted address. Malformed chains fall back to the
connection peer. When `X-Forwarded-For` is absent, one `X-Real-IP` is accepted from
the trusted peer; that header must be overwritten by the proxy. Forwarded host
and scheme never authorize additional origins. If `BOOK_PROXY_TOKEN` is also
configured, token mode takes precedence, including rejection of an invalid token.
This setting cannot recover a client address the proxy does not provide.

### Authentication diagnostics

Use one public URL setting. `BOOK_PUBLIC_URL` takes precedence over `PUBLIC_URL`,
including in Docker. A host `.env` entry has no effect unless Compose passes it
to the container. After changing settings:

```sh
docker compose up -d --no-deps --force-recreate dewarr
```

`docker compose restart` does not apply environment changes. For an image upgrade,
pull the intended release as well. Native installations require a process restart.

From v0.3.3, this operator-only command applies the same Docker aliases
as startup and prints selected configuration without passwords, cookies or tokens:

```sh
docker compose exec -T dewarr python -m app.diagnostics --container
```

Native installations use `uv run python -m app.diagnostics`. A new `docker exec`
process does not inherit environment changes made inside the entrypoint, so simply
calling `get_settings()` without applying those aliases can give misleading output.
The diagnostic reflects the current container configuration; recreate the container
when the desired Compose settings differ. Do not share a full environment dump.

Origin failures log a reason (`missing`, `duplicate`, `malformed`, or `mismatch`),
normalized origins and the response's `X-Request-ID`. Logs are sampled to one event
per minute per reason per process; raw malformed headers, cookies and tokens are
omitted. A reverse proxy should preserve exactly one browser Origin header.
If login succeeds but `/api/auth/me` returns 401, check cookie transport and HTTPS
rather than changing the origin allowlist. HTTPS with explicitly disabled Secure
cookies emits an operator warning.

## Updating

```sh
docker compose pull
docker compose up -d
```

The app stops both services together and runs migrations before restarting. If either service fails, the container exits so Docker can restart it. `pull` and `up -d` only recreate Dewarr, so PostgreSQL keeps its existing network attachment. If you renamed the Compose network or moved Dewarr into another stack since PostgreSQL was created, recreate both together with `docker compose up -d --force-recreate dewarr postgres`. To share a network between Compose projects, create it once with `docker network create <name>` and declare it `external: true` in each project. For manual `docker run` installations, pull the image, stop/remove only the Dewarr container, and recreate it using the same command and volumes.

## Backups

```sh
mkdir -p backups
chmod 700 backups
docker compose stop dewarr
docker compose exec -T postgres pg_dump -U dewarr -d dewarr -Fc > backups/dewarr.dump
cp -R config backups/config
docker compose start dewarr
```

Use your configured database user/name if different. Also back up library media. The encryption key in `config/app_key` is required to decrypt saved integration credentials. See [Recovery](RECOVERY.md) before restoring an old database.

`docker compose down` keeps the database volume. `docker compose down -v` deletes it.

## Upgrade from the original four-service setup

Preserve your existing database and encryption key. Before replacing the old Compose file:

1. Stop the old API and worker: `docker compose stop api worker`.
2. Back up PostgreSQL and `.local/secrets/app_key`.
3. Create `config` and copy `.local/secrets/app_key` to `config/app_key`.
4. Run `docker compose down` without `-v` to remove the old containers while retaining the database volume.
5. Replace Compose with the new two-service example. Keep the old project name and PostgreSQL volume, database name, user, and password. Existing installations commonly use `book` for the database/user. Update the database health-check command to match too.
6. Preserve your public URL and media mounts, then run `docker compose up -d`.

Changing `POSTGRES_PASSWORD` in Compose does not change an existing database password. Reuse the original value. Never replace an existing installation's key with a newly generated one.

## Troubleshooting

- **The request origin is not allowed / cannot sign in:** set `PUBLIC_URL` to the browser's scheme, hostname and port (no path), check for a stale `BOOK_PUBLIC_URL` override, then recreate the container. Confirm the proxy preserves exactly one valid Origin header. Native installations use `BOOK_PUBLIC_URL` and require a restart. This is especially important when an HTTPS proxy forwards requests to Dewarr over HTTP. Identity provider sign-in uses that same address for its redirect URL; see [OpenID Connect](OIDC.md). Plex sign-in uses it the same way; see [Plex](PLEX.md).
- **Permission denied:** check `PUID`, `PGID`, and shared-folder ownership.
- **Database unavailable:** the log names the failed step. If `DB_HOST` does not resolve, Dewarr and PostgreSQL are not on the same Docker network. Check with `docker network inspect <network>`, then run `docker compose down` followed by `docker compose up -d` to recreate the containers and network. Your data is kept unless you add `-v`. A rejected password means the value differs from the one the database was created with.
- **Existing database / missing key:** restore the original key to `config/app_key`.
- **Migrations waiting:** stop any old API/worker containers using the same database.
- **Inspect startup:** run `docker compose logs --tail=100 dewarr`.

For a local source build, see [Contributing](DEVELOPMENT.md).
