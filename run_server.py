#!/usr/bin/env python3
"""
Entry point for the lecture-interpreter backend.

    python run_server.py

With DATABASE_URL and JWT_SECRET in .env it runs in multi-tenant mode: the WebSocket server (default port 8765)
and the HTTP API for accounts, rooms and the admin dashboard (default port 8000, docs at /docs), sharing one
database pool, and it applies pending migrations on startup (AUTO_MIGRATE=false to skip). Without DATABASE_URL
it runs in single-session mode: WebSocket only, no accounts, rooms or metering.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import uvicorn

from app.accounts import Accounts
from app.api import create_app
from app.config import load_config
from app.db import Database
from app.email_sender import build_mailer
from app.server import Server

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class _ApiServer(uvicorn.Server):
    """uvicorn without its own signal handling: Ctrl+C is handled once, by asyncio, for both servers."""

    def install_signal_handlers(self) -> None:  # older uvicorn
        pass

    @contextlib.contextmanager
    def capture_signals(self):  # newer uvicorn
        yield


async def amain() -> None:
    log = logging.getLogger("run_server")
    cfg = load_config()
    db, api = None, None
    if cfg.database_url:
        if not cfg.jwt_secret:
            raise SystemExit("JWT_SECRET must be set in .env when DATABASE_URL is set")
        if len(cfg.jwt_secret) < 32:
            log.warning("JWT_SECRET is shorter than 32 characters; use a long random value")
        db = await Database.connect(cfg.database_url)
        if cfg.auto_migrate:
            applied = await db.migrate(MIGRATIONS)
            log.info("Migrations applied: %s", ", ".join(applied) if applied else "none pending")
        accounts = Accounts(db, cfg.jwt_secret, build_mailer(cfg), code_ttl_s=cfg.email_code_ttl_s,
                            refresh_ttl_s=cfg.refresh_token_ttl_s)
        api = _ApiServer(uvicorn.Config(create_app(cfg, db, accounts), host=cfg.api_host, port=cfg.api_port,
                                        log_level="info", lifespan="off"))
    else:
        log.warning("DATABASE_URL not set: single-session mode, no accounts, rooms, metering or HTTP API")
    tasks = [asyncio.create_task(Server(cfg, db=db).serve_forever())]
    if api is not None:
        tasks.append(asyncio.create_task(api.serve()))
    try:
        await asyncio.gather(*tasks)
    finally:
        if api is not None:
            api.should_exit = True
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
