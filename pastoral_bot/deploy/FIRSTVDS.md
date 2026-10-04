# FirstVDS operations

The isolated deployment lives at `/home/margleb/pastoral-bot`. Its `current`
symlink selects a committed release under `releases/`. The executable `compose`
wrapper selects the matching image and reads `/home/margleb/pastoral-bot/.env`
(mode 0600). Credentials are outside the release checkout.

Use the configured SSH alias from the development machine:

```bash
ssh firstvds '/home/margleb/pastoral-bot/compose ps'
ssh firstvds '/home/margleb/pastoral-bot/compose run --rm ops doctor'
ssh firstvds '/home/margleb/pastoral-bot/compose run --rm ops metrics'
ssh firstvds '/home/margleb/pastoral-bot/compose logs --tail 50 app backups'
ssh firstvds '/home/margleb/pastoral-bot/compose run --rm ops backup'
```

Compose services `app`, `db`, and `backups` restart automatically. The public bot
uses long polling. Its Mini App shares the same application process and exposes
HTTP on `127.0.0.1:8091` only. PostgreSQL port 5543 is restricted
to server loopback. The database volume is `pastoral_pastoral_pgdata`; application
state is `pastoral_pastoral_state`, mounted at `/var/lib/pastoral-bot`.

The public Mini App URL is `https://bot-ams.margleb.ru/pastoral/`. Set these values
in the external environment file before recreating `app`:

```dotenv
PASTORAL_WEB_ENABLED=true
PASTORAL_WEB_HOST=0.0.0.0
PASTORAL_WEB_PORT=8091
PASTORAL_WEB_PUBLIC_URL=https://bot-ams.margleb.ru/pastoral/
```

The existing system Nginx terminates this domain's HTTPS on loopback port 8443;
the public 443 listener routes TLS by SNI. Add only the `/pastoral/` location
shown in the deployment README to `/etc/nginx/sites-available/bot-ams.conf`.
Preserve `/r`, the root health check, certificates and the SNI default service on
9443. The public path proxies to `http://127.0.0.1:8091/` and disables access logs.
Do not bind another container to public ports 80 or 443. Check `nginx -t` before
reload and retain a copy of the original virtual host for rollback. Configuration
changes and reload require root privileges; the SSH user has no passwordless sudo.

Encrypted snapshots are stored in the state volume's `backups/` directory for
seven days. The scheduler runs daily after 03:00 UTC (06:00 Moscow time).
Retain the deletion and mode journals in the state volume when restoring;
follow the main README's restore procedure with the bot stopped. Never remove
either volume during an application update.

This server has limited available RAM. Build frontend assets once into the
application image rather than running a Node development server or a second
embedder/model process. Container resource limits remain unchanged.

For a new release, upload `git archive COMMIT` to a new release directory,
build its image, select that release in the wrapper, run `ops doctor` and live
evaluation before recreating `app` and `backups`. Evaluate only public synthetic
scenarios in a separate `_eval` database with the production budget ledger.
Preserve both volumes and the external environment file during rollback.

Before rolling back to an image older than Mini App support, stop `app` and
cancel unfinished web jobs in one database transaction. Older releases do not
recognize delivery channels and could otherwise send web replies to Telegram.
Use the application database role and acquire the same transaction advisory
lock as the running service; abort rollback if the lock cannot be acquired.
Do not print the database URL or its password:

```sql
BEGIN;
DO $pastoral_guard$
BEGIN
    IF NOT pg_try_advisory_xact_lock(7261032026) THEN
        RAISE EXCEPTION 'pastoral_service_still_running';
    END IF;
END;
$pastoral_guard$;
WITH cancelled AS (
    UPDATE pastoral_jobs
    SET status = 'cancelled', error_code = 'legacy_rollback'
    WHERE channel = 'web' AND status IN ('pending', 'running', 'generated')
    RETURNING update_id
), receipts_updated AS (
    UPDATE pastoral_telegram_receipts SET status = 'cancelled'
    WHERE update_id IN (SELECT update_id FROM cancelled)
    RETURNING update_id
)
SELECT (SELECT count(*) FROM cancelled) AS cancelled_web_jobs,
       (SELECT count(*) FROM receipts_updated) AS cancelled_receipts;
COMMIT;
```

Commit this cleanup before selecting the old image. Telegram jobs and completed
web history remain intact. Retain the additive database columns, both volumes
and the original journals; remove the Mini App menu button when disabling it.
