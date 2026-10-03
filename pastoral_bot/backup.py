"""Encrypted PostgreSQL snapshots. The deletion journal stays outside snapshots."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import make_url


class BackupError(RuntimeError):
    pass


def cipher(settings) -> Fernet:
    try:
        return Fernet(settings.backup_key.get_secret_value().encode("ascii"))
    except (ValueError, UnicodeError):
        raise BackupError("Set a valid PASTORAL_BACKUP_KEY (Fernet key)") from None


def pg_options(settings) -> tuple[list[str], dict]:
    url = make_url(settings.database_url)
    if not url.database or url.database == "your_own":
        raise BackupError("An isolated pastoral database is required")
    options = ["--host", url.host or "localhost", "--port", str(url.port or 5432),
               "--username", url.username or "postgres", "--dbname", url.database]
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    return options, env


async def _command(args: list[str], env: dict, payload: bytes | None = None) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, env=env, stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await process.communicate(payload)
    except (OSError, ValueError):
        raise BackupError("postgres_tool_unavailable") from None
    if process.returncode:
        # stderr may include DSNs or SQL/content; never print it.
        raise BackupError("postgres_tool_failed")
    return output


def prune(directory: Path, days: int = 7, now: datetime | None = None) -> None:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    for path in directory.glob("pastoral-*.dump.enc"):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) <= cutoff:
            path.unlink()


async def create_backup(settings) -> Path:
    encryption = cipher(settings)
    options, env = pg_options(settings)
    directory = settings.state_dir / "backups"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    prune(directory, settings.backup_retention_days)
    # Dump only application-owned tables and their indexes/owned sequences.
    # A full database dump includes the administrator-owned vector extension;
    # restoring that with --clean as the restricted bot role cannot drop it.
    raw = await _command([
        "pg_dump", *options, "--format=custom", "--no-owner", "--no-acl",
        "--table=public.pastoral_*", "--strict-names",
    ], env)
    encrypted = encryption.encrypt(raw)
    del raw
    path = directory / datetime.now(timezone.utc).strftime("pastoral-%Y%m%dT%H%M%S%f.dump.enc")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    return path


async def restore_backup(settings, path: Path, store) -> None:
    # Refuse to restore if deletion metadata has been lost. Keeping the
    # journal is a required operational invariant, including disaster recovery.
    journal = store.deletion_journal
    if journal is None or not journal.is_file():
        raise BackupError("Deletion journal is required for restoration")
    if store.mode_journal is None or not store.mode_journal.is_file():
        raise BackupError("Mode journal is required for restoration")
    try:
        raw = cipher(settings).decrypt(path.read_bytes())
    except (InvalidToken, OSError):
        raise BackupError("invalid_encrypted_backup") from None
    options, env = pg_options(settings)
    # Provisioning must already have installed vector as the administrator.
    # Fail before destructive restoration if its required types are absent.
    await store.initialize()
    await _command(["pg_restore", *options, "--clean", "--if-exists", "--no-owner", "--no-acl", "--exit-on-error"], env, raw)
    del raw
    await store.initialize()
    await store.replay_deletions()
    await store.replay_modes()
