"""Container scheduler: hourly retention and one encrypted snapshot per UTC day."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from pastoral_bot.backup import create_backup, prune
from pastoral_bot.config import BotSettings

logger = logging.getLogger("pastoral.backup_scheduler")


async def run() -> None:
    settings = BotSettings()
    directory = settings.state_dir / "backups"
    last_prune_hour = None
    while True:
        now = datetime.now(timezone.utc)
        hour = now.strftime("%Y%m%d%H")
        if hour != last_prune_hour:
            try:
                prune(directory, settings.backup_retention_days, now=now)
                last_prune_hour = hour
            except Exception as exc:
                logger.error("prune_failed kind=%s", type(exc).__name__)
        # Existing encrypted files also record completed work across restarts.
        today = now.strftime("%Y%m%d")
        if now.hour >= 3 and not any(directory.glob(f"pastoral-{today}T*.dump.enc")):
            try:
                await create_backup(settings)
                logger.info("backup_completed day=%s", today)
            except Exception as exc:
                logger.error("backup_failed kind=%s", type(exc).__name__)
        await asyncio.sleep(60)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
