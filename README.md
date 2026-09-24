<p align="center"><img src="apps/web/public/assets/dewarr.png" width="88" alt="Dewarr logo"></p>
<h1 align="center">Dewarr</h1>
<p align="center">Your reading lists, audiobook library, and downloads in one place.</p>

![Dewarr For you page](docs/images/for-you.png)

## Features

- **Library integration** — connect Audiobookshelf or Grimmory, see what you own, and import completed downloads into verified library folders.
- **Chapterized M4B** — merge a multi-file MP3 download into one audiobook, with a chapter per file, before library import. The download itself stays unchanged.
- **Goodreads sync** — follow shelves, import CSV exports, and check for new books automatically.
- **Author and series follows** — monitor future books with reviewed back-catalog selection, filters, exclusions, and release-day acquisition.
- **Hardcover sync** — track your lists and lists you follow, including private lists your account can access.
- **Custom Goodreads lists** — add public lists, track changes, and pin them to your discovery page.
- **For you** — personalize shelves with followed lists, recommendations, trending books, and new releases.
- **Auto download** — find and select eligible releases using list policies and approved import routes.
- **Download priorities** — rank sources and formats, apply size and seed preferences, and control download capacity.
- **Book discovery** — browse collections, awards, authors, and series.
- **Download sources** — connect MyAnonamouse, Prowlarr, and AudiobookBay; send transfers to qBittorrent.
- **Shared library** — individual accounts, reading lists, permissions, and download activity.
- **Self-hosted** — Docker, PostgreSQL, and an MIT license.

Early release. Automatic downloads are off by default. Goodreads RSS imports can be partial; CSV import fills historical gaps. See [reading accounts](docs/READING-ACCOUNTS.md) for sync behavior.

## Quick start

Run **Dewarr + PostgreSQL**. Dewarr handles migrations and background jobs automatically. No Git, Python, setup script, or `.env` file is required.

`latest` tracks stable releases; use a `vX.Y.Z` tag to pin a release. The optional `edge` tag follows checked builds of main.

Choose an option below. Set both database passwords to the same value. For access from another computer, change `PUBLIC_URL` to `http://your-server:8000`.

<details open>
<summary><strong>Docker Compose — recommended</strong></summary>

Save this as `compose.yaml`. Adjust `PUID`, `PGID`, and the folders to match your server.

```yaml
name: dewarr

services:
  dewarr:
    image: ghcr.io/logabell/dewarr:latest
    environment:
      PUID: 1000
      PGID: 1000
      TZ: Etc/UTC
      PUBLIC_URL: http://localhost:8000
      DB_HOST: postgres
      DB_NAME: dewarr
      DB_USER: dewarr
      DB_PASSWORD: change-me
    volumes:
      - ./config:/config
      - ./data:/data
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
    restart: unless-stopped
    stop_grace_period: 35s

  postgres:
    image: postgres:18
    environment:
      POSTGRES_DB: dewarr
      POSTGRES_USER: dewarr
      POSTGRES_PASSWORD: change-me
    volumes:
      - postgres:/var/lib/postgresql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dewarr -d dewarr"]
      interval: 5s
      timeout: 5s
      retries: 12
    restart: unless-stopped

volumes:
  postgres:
```

```sh
mkdir -p config data
docker compose up -d
```

</details>

<details>
<summary><strong>Docker CLI — download the Compose file</strong></summary>

```sh
mkdir dewarr
cd dewarr
curl -fsSL https://raw.githubusercontent.com/logabell/dewarr/main/compose.yaml -o compose.yaml
```

Edit the passwords, `PUBLIC_URL`, and folder paths in `compose.yaml`, then start:

```sh
mkdir -p config data
docker compose up -d
```

</details>

<details>
<summary><strong>Docker run — without Compose</strong></summary>

Change both `change-me` passwords to the same value before running these commands.

```sh
mkdir -p config data
docker network create dewarr
docker volume create dewarr-postgres

docker run -d \
  --name dewarr-postgres \
  --network dewarr \
  -e POSTGRES_DB=dewarr \
  -e POSTGRES_USER=dewarr \
  -e POSTGRES_PASSWORD=change-me \
  -v dewarr-postgres:/var/lib/postgresql \
  --restart unless-stopped \
  postgres:18

docker run -d \
  --name dewarr \
  --network dewarr \
  -e PUID=1000 \
  -e PGID=1000 \
  -e TZ=Etc/UTC \
  -e PUBLIC_URL=http://localhost:8000 \
  -e DB_HOST=dewarr-postgres \
  -e DB_NAME=dewarr \
  -e DB_USER=dewarr \
  -e DB_PASSWORD=change-me \
  -p 8000:8000 \
  -v "$PWD/config:/config" \
  -v "$PWD/data:/data" \
  --stop-timeout 35 \
  --restart unless-stopped \
  ghcr.io/logabell/dewarr:latest
```

Already have PostgreSQL? Skip its container and use your existing database host, name, user, and password in the Dewarr command.

</details>

Open [localhost:8000](http://localhost:8000), or your server's address, and create your first account. Connect Audiobookshelf or Grimmory, Goodreads, Hardcover, and download clients in **Settings**.

Before creating a Hardcover token, enable `read:catalog`, `read:me:content`, `read:lists`, `read:library:public`, `read:users`, and `write:lists`. [Create a token with those scopes selected](https://hardcover.app/account/api/keys/new?scope=read:catalog+read:me:content+read:lists+read:library:public+read:users+write:lists). The Metadata setup screen lists what each scope is for. Details are in [reading accounts](docs/READING-ACCOUNTS.md). To let Authentik, Pocket ID, or Authelia manage later sign-ins, see [OpenID Connect](docs/OIDC.md). To let a household sign in with Plex, see [Plex sign-in](docs/PLEX.md).

Dewarr creates its encryption key in `/config` on first start. Keep that folder and the PostgreSQL volume when updating.

[Settings, folders, backups, and upgrades](docs/DOCKER.md) · [Download clients](docs/DOWNLOAD-CLIENTS.md) · [Request quotas](docs/REQUEST-QUOTAS.md) · [Contributing](docs/DEVELOPMENT.md)

## Screenshots

Actual Dewarr UI captured with an isolated demo account. Connected services and library contents depend on your setup.

### Browse books
![Browse discovery shelves](docs/images/browse.png)

### Collections and awards
![Book collections](docs/images/collections.png)
![Reading awards](docs/images/awards.png)

### Reading accounts
![Goodreads and Hardcover connections](docs/images/reading-accounts.png)

### Download preferences
![Download preferences and priorities](docs/images/download-preferences.png)

### Library connections
![Audiobookshelf library settings](docs/images/library-settings.png)



## Updates

```sh
docker compose pull
docker compose up -d
```

Back up `config` and PostgreSQL first. Dewarr applies database migrations automatically. [Backup instructions](docs/DOCKER.md#backups).

## License

[MIT](LICENSE). Third-party libraries and assets retain their own licenses; see [notices](docs/notices/).
