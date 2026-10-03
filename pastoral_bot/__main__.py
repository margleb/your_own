"""Run with python -m pastoral_bot; all configuration is explicitly isolated."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from contextlib import suppress
from pathlib import Path

import aiohttp

from infrastructure.telegram.client import TelegramClient
from pastoral_bot.app import BotApp
from pastoral_bot.budget import Budget
from pastoral_bot.config import BotSettings
from pastoral_bot.knowledge import Knowledge
from pastoral_bot.llm import PastoralLLM
from pastoral_bot.storage import Store
from pastoral_bot.turn import PastoralTurn


async def execute(args) -> None:
    settings = BotSettings()
    if args.command == "doctor":
        print(json.dumps({"telegram_configured": bool(settings.telegram_token.get_secret_value()),
                          "llm_configured": bool(settings.openrouter_api_key.get_secret_value()),
                          "database_configured": bool(settings.database_url),
                          "backup_key_configured": bool(settings.backup_key.get_secret_value()),
                          "model": settings.model, "daily_answers": settings.daily_answer_limit,
                          "daily_budget_usd": str(settings.daily_budget_usd)}, ensure_ascii=False))
        if not settings.database_url:
            return
    if not settings.database_url:
        raise ValueError("Set PASTORAL_DATABASE_URL")
    settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal = settings.state_dir / "deletions.jsonl"
    modes = settings.state_dir / "modes.jsonl"
    if args.command == "restore" and (not journal.is_file() or not modes.is_file()):
        raise ValueError("Restore requires the original deletion and mode journals")
    # Metadata only; independent of database backups. Never truncate it.
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    fd = os.open(modes, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.close(fd)
    store = Store(settings.database_url, deletion_journal=journal, mode_journal=modes)
    budget_store = None
    ownership = None
    try:
        if args.command in ("run", "restore"):
            from sqlalchemy import text
            ownership = await store.engine.connect()
            acquired = await ownership.scalar(text("SELECT pg_try_advisory_lock(7261032026)"))
            if not acquired:
                raise RuntimeError("pastoral_service_already_running")
        await store.initialize()
        await store.replay_deletions()
        await store.replay_modes()
        knowledge = Knowledge(store, settings)
        await knowledge.initialize()
        if args.command == "doctor":
            print(json.dumps({"database_ready": True, "approved_documents": await knowledge.approved_count()}, ensure_ascii=False))
            return
        if args.command == "init-db":
            print("pastoral_database_ready")
            return
        if args.command == "metrics":
            print(json.dumps(await store.metrics(), ensure_ascii=False, default=str))
            return
        if args.command in ("backup", "prune-backups", "restore"):
            from pastoral_bot.backup import create_backup, prune, restore_backup
            if args.command == "backup":
                print(await create_backup(settings))
            elif args.command == "prune-backups":
                prune(settings.state_dir / "backups", settings.backup_retention_days)
                print("backup_retention_applied")
            else:
                if not args.confirm_restore:
                    raise ValueError("Stop the bot and pass --confirm-restore to replace its isolated database")
                await restore_backup(settings, Path(args.path), store)
                print("restored_with_deletions_reapplied")
            return
        settings.require_runtime()
        if await knowledge.approved_count() != 4:
            raise RuntimeError("Import and review all four knowledge sources before running the public bot")
        if settings.budget_database_url and settings.budget_database_url != settings.database_url:
            budget_store = Store(settings.budget_database_url)
            await budget_store.initialize()
        budget = Budget(budget_store or store, settings)
        await budget.recover()
        async with aiohttp.ClientSession() as session:
            client = TelegramClient(settings.telegram_token.get_secret_value(), session=session, private=True)
            identity = await client.get_me()
            if not identity.get("is_bot"):
                raise RuntimeError("invalid_bot_token")
            turn = PastoralTurn(settings, store, knowledge, PastoralLLM(settings), budget)
            app = BotApp(settings, store, turn, client)
            loop = asyncio.get_running_loop()
            stop = asyncio.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            poll = asyncio.create_task(app.poll())
            stopping = asyncio.create_task(stop.wait())
            logging.getLogger("pastoral.runtime").info("bot_started model=%s", settings.model)
            try:
                done, _ = await asyncio.wait([poll, stopping], return_when=asyncio.FIRST_COMPLETED)
                if poll in done:
                    await poll
            finally:
                poll.cancel()
                stopping.cancel()
                await app.close()
                await asyncio.gather(poll, stopping, return_exceptions=True)
    finally:
        if ownership is not None:
            with suppress(Exception):
                from sqlalchemy import text
                await ownership.execute(text("SELECT pg_advisory_unlock(7261032026)"))
            await ownership.close()
        await store.close()
        if budget_store is not None:
            await budget_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent Orthodox Telegram helper")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "doctor", "init-db", "metrics", "backup", "prune-backups", "restore"])
    parser.add_argument("path", nargs="?")
    parser.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args()
    if args.command == "restore" and not args.path:
        parser.error("restore requires an encrypted backup path")
    # No exception text or traceback: SQL/provider errors may contain content.
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("pastoral.runtime").setLevel(logging.INFO)
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logging.getLogger("pastoral.runtime").error("startup_failed kind=%s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
