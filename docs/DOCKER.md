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

To use another external port, change `8000:8000` to, for example, `8085:8000`, then set `PUBLIC_URL=http://your-server:8085`.

Most configuration belongs in the app: connect libraries, reading accounts, sources, and download clients through **Settings**. To permit download dispatch after configuring routes and policies, add `BOOK_DOWNLOAD_DISPATCH_ENABLED: "true"` to Dewarr's environment and recreate the container.

## Folders

| Mount | What it stores |
| --- | --- |
| `./config:/config` | Dewarr's encryption key. Keep it with database backups. |
| `./data:/data` | Shared downloads and library files. Replace `./data` with your existing media parent folder. |
| `postgres:/var/lib/postgresql` | PostgreSQL 18 data in a persistent Docker volume. |

Dewarr initializes `/config` and runs the app as `PUID:PGID`. It does not change ownership of existing media. Give that user/group access to your shared folders. Groups added with Compose `group_add` are kept, so folders shared through another group also work.

Mount a parent folder, not the library folder itself. Dewarr keeps a private `.book-search-staging` folder beside the library folder and moves finished imports from it into the library, so both must be on the same filesystem. Mount `/media:/mnt` and choose `/mnt/audiobooks`, rather than mounting `/media/audiobooks:/mnt/audiobooks`. Dewarr uses one staging folder for every library, so ebook and audiobook library folders must also share that filesystem. Dewarr checks these when you choose a folder. The staging folder keeps records of past imports, so once it has any, Dewarr will not move it to another filesystem on its own. To move your libraries to a new filesystem, set `BOOK_IMPORT_STAGING_ROOT` to an empty folder there that is owned by `PUID` with mode `0700`.

Use the same paths in Dewarr, qBittorrent, and your library server (Audiobookshelf or Grimmory) when possible. Grimmory's image is `grimmory/grimmory` and listens on port 6060. For example:

```text
/data/downloads
/data/audiobooks
/data/staging
```

Choose those folders in Settings and verify your library routes. If Audiobookshelf sees `/audiobooks` while Dewarr sees `/data/audiobooks`, set that mapping in Dewarr. If qBittorrent sees a different path for the download folder, map that path in **Settings → Download clients**. A shared parent mount allows hardlinks when the filesystem supports them. When the download and library folders are on different filesystems, Dewarr copies the files into the library instead. A library folder can instead ask qBittorrent to rename the seeding files into that folder, so the seeding file and the library file are the same copy. That option stays off unless you turn it on.

### Network shares (NFS and SMB)

NFS and SMB/CIFS libraries work when the staging folder is on the same mounted share as the library. These filesystems do not support the atomic no-replace rename flag, so Dewarr uses a fallback that still never overwrites an existing book or journal. The route test checks that fallback on your actual mount.

SMB/CIFS mounts need a few options, because the share, not Linux, decides ownership and permissions:

- `uid=<PUID>,gid=<PGID>,dir_mode=0700`: the staging folder must be owned by Dewarr's user and private. The route test names the exact uid it expects. These options apply to the whole mount, so containers sharing it (qBittorrent, Audiobookshelf) need the same `PUID`.
- `serverino` (the default): Dewarr tracks files by inode number. With `noserverino` those numbers can change between checks. The route test warns when it sees this.
- `nobrl` on older kernels, if the route test reports that the staging filesystem does not support file locks.

Shares without hardlink support, such as many NAS SMB exports, fall back to copying.

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

Set `BOOK_PROXY_TOKEN` to a long random value. The proxy must send that value in `X-Dewarr-Proxy-Token`, set `X-Real-IP` to the connecting client, and append that client to `X-Forwarded-For`. When a header is repeated, the last value is the one that counts. Sign-in limits use that client when those two addresses agree, or when only one is present. If they disagree, Dewarr keeps the connection's own address, so a visitor-supplied address cannot replace the one the proxy appended. Requests without the token keep that connection address too, including visitors who open port 8000 directly.

```nginx
proxy_set_header Host $host;
proxy_set_header X-Dewarr-Proxy-Token your-long-random-token;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

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

- **Cannot sign in:** make `PUBLIC_URL` exactly match the browser address. Identity provider sign-in uses that same address for its redirect URL; see [OpenID Connect](OIDC.md). Plex sign-in uses it the same way; see [Plex](PLEX.md).
- **Permission denied:** check `PUID`, `PGID`, and shared-folder ownership.
- **Database unavailable:** the log names the failed step. If `DB_HOST` does not resolve, Dewarr and PostgreSQL are not on the same Docker network. Check with `docker network inspect <network>`, then run `docker compose down` followed by `docker compose up -d` to recreate the containers and network. Your data is kept unless you add `-v`. A rejected password means the value differs from the one the database was created with.
- **Existing database / missing key:** restore the original key to `config/app_key`.
- **Migrations waiting:** stop any old API/worker containers using the same database.
- **Inspect startup:** run `docker compose logs --tail=100 dewarr`.

For a local source build, see [Contributing](DEVELOPMENT.md).
