#!/usr/bin/env python3
"""
Entry point for the lecture-interpreter backend.

    python run_server.py

With DATABASE_URL (and JWT_SECRET) in .env it runs in multi-tenant mode and
applies pending migrations on startup (AUTO_MIGRATE=false to skip). Without
DATABASE_URL it runs in single-session mode, as before.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.config import load_config
from app.db import Database
from app.server import Server

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


async def amain() -> None:
    log = logging.getLogger("run_server")
    cfg = load_config()
    db = None
    if cfg.database_url:
        db = await Database.connect(cfg.database_url)
        if cfg.auto_migrate:
            applied = await db.migrate(MIGRATIONS)
            log.info("Migrations applied: %s", ", ".join(applied) if applied else "none pending")
    else:
        log.warning("DATABASE_URL not set: single-session mode, no accounts, rooms or metering")
    try:
        await Server(cfg, db=db).serve_forever()
    finally:
        if db is not None:
            await db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logging.getLogger("run_server").info("Shutting down.")


if __name__ == "__main__":
    main()