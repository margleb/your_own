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
uses long polling and exposes no HTTP port. PostgreSQL port 5543 is restricted
to server loopback. The database volume is `pastoral_pastoral_pgdata`; application
state is `pastoral_pastoral_state`, mounted at `/var/lib/pastoral-bot`.

Encrypted snapshots are stored in the state volume's `backups/` directory for
seven days. The scheduler runs daily after 03:00 UTC (06:00 Moscow time).
Retain the deletion and mode journals in the state volume when restoring;
follow the main README's restore procedure with the bot stopped. Never remove
either volume during an application update.

For a new release, upload `git archive COMMIT` to a new release directory,
build its image, select that release in the wrapper, run `ops doctor` and live
evaluation before recreating `app` and `backups`. Evaluate only public synthetic
scenarios in a separate `_eval` database with the production budget ledger.
Preserve both volumes and the external environment file during rollback.
