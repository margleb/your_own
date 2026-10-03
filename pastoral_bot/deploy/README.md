# Isolated Docker deployment

The Compose project is named `pastoral`. It has its own database and state volumes;
it does not mount the original application's checkout, credentials or data.
The application has no inbound port and uses Telegram long polling.

Keep a release checkout under `~/pastoral-bot/releases/COMMIT` and an isolated
environment file outside it, for example `~/pastoral-bot/pastoral.env` with mode
0600. Copy `pastoral_bot/.env.example` and set the new bot and OpenRouter keys,
distinct database passwords, a Fernet backup key and the campaign allowlist.
Use randomly generated hexadecimal or URL-safe alphanumeric database passwords:
Compose interpolates the application password into the connection URL.

Also add these deployment variables to that environment file:

```dotenv
PASTORAL_ENV_FILE=/home/margleb/pastoral-bot/pastoral.env
PASTORAL_IMAGE=pastoral-bot:COMMIT
```

From the release checkout, use the same environment file for every command:

```bash
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml build app
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml up -d db
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml run --rm bootstrap
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml run --rm ops init-db
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml run --rm ops doctor
```

The app and operational commands use UID/GID 10001. Bootstrap creates and sets
permissions on their isolated state volume. To preload public model weights,
copy them only into that volume's `embedding-cache` directory and rerun bootstrap
after the copy. Hugging Face's auxiliary cache also stays inside this volume.

Import and review the corpus before starting the public bot:

```bash
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml run --rm --entrypoint python ops -m pastoral_bot.import_sources preview --version 2026-10-03 --output /var/lib/pastoral-bot/corpus-preview.json
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml run --rm --entrypoint python ops -m pastoral_bot.import_sources apply /var/lib/pastoral-bot/corpus-preview.json --approve REVIEWED_SHA256
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml run --rm ops doctor
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml run --rm ops backup
docker compose --env-file ~/pastoral-bot/pastoral.env -f pastoral_bot/deploy/compose.yaml up -d app backups
```

Database port 5543 is published only on loopback. Container commands override
`PASTORAL_DATABASE_URL` with `db:5432`; leave `PASTORAL_BUDGET_DATABASE_URL` empty
for the normal public service. For evaluation, supply an isolated `_eval` DSN
and explicitly set the shared budget DSN to the public `pastoral` database.

The image installs CPU-only PyTorch and PostgreSQL 16 client binaries. Resource
limits are 1200 MiB / one CPU for the app, 256 MiB / half a CPU for PostgreSQL,
and 256 MiB / quarter of a CPU for the backup scheduler. The scheduler prunes
hourly and creates an encrypted backup daily after 03:00 UTC, including missed
work after restart. Check failures with `docker compose ... logs backups`.

Do not run `down --volumes`: the state volume contains deletion and mode journals
that must survive database restore. For rollback, change `PASTORAL_IMAGE` to the
previous committed release and recreate `app` and `backups`, keeping both volumes.
Read the main README for OpenRouter account privacy settings and the required
manual review of real model answers before advertising.
