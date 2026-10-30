#!/usr/bin/env python3
"""
Platform-operator commands. These are deliberately NOT part of the HTTP API: creating a school, and deciding which
email domains belong to it, is the trust anchor of the whole system. A school admin who could add "gmail.com" would
let anyone in. Check that a school really owns a domain before registering it.

    python tools/manage.py create-school --name "Universitas X" --domain x.ac.id --admin-email rector@x.ac.id --admin-name "A. Rector"
    python tools/manage.py add-domain --school x.ac.id --domain student.x.ac.id --students-only
    python tools/manage.py add-domain --school x.ac.id --domain staff.x.ac.id --staff-only
    python tools/manage.py resend-admin-invite --admin-email rector@x.ac.id

create-school registers the school and emails its single admin a one-time code to set their password. With
EMAIL_BACKEND=console the code is printed in this terminal instead of being emailed.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import auth  # noqa: E402
from app.accounts import AccountError, Accounts  # noqa: E402
from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.email_sender import build_mailer  # noqa: E402


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-school")
    c.add_argument("--name", required=True)
    c.add_argument("--domain", required=True, help="the school's primary email domain, e.g. umy.ac.id")
    c.add_argument("--admin-email", required=True)
    c.add_argument("--admin-name", required=True)
    d = sub.add_parser("add-domain")
    d.add_argument("--school", required=True, help="the school's primary domain")
    d.add_argument("--domain", required=True)
    g = d.add_mutually_exclusive_group()
    g.add_argument("--staff-only", action="store_true")
    g.add_argument("--students-only", action="store_true")
    r = sub.add_parser("resend-admin-invite")
    r.add_argument("--admin-email", required=True)
    args = p.parse_args()

    cfg = load_config()
    if not (cfg.database_url and cfg.jwt_secret):
        print("DATABASE_URL and JWT_SECRET must be set in .env")
        return 1
    db = await Database.connect(cfg.database_url)
    try:
        await db.migrate(ROOT / "migrations")
        acc = Accounts(db, cfg.jwt_secret, build_mailer(cfg), code_ttl_s=cfg.email_code_ttl_s, refresh_ttl_s=cfg.refresh_token_ttl_s)
        try:
            if args.cmd == "create-school":
                inst, admin = await acc.create_institution(args.name, args.domain.strip().lower(), args.admin_email, args.admin_name)
                print(f"School created ({inst}). Its admin {args.admin_email} has been sent a code to set a password.")
            elif args.cmd == "add-domain":
                inst = await db.pool.fetchval("SELECT id FROM institutions WHERE lower(domain) = lower($1)", args.school)
                if inst is None:
                    print(f"No school has the primary domain {args.school}")
                    return 1
                await acc.add_domain(inst, args.domain, allow_staff=not args.students_only, allow_students=not args.staff_only)
                print(f"{args.domain.lower()} now belongs to {args.school} "
                      f"({'staff only' if args.staff_only else 'students only' if args.students_only else 'staff and students'}).")
            elif args.cmd == "resend-admin-invite":
                await acc.resend_admin_invite(auth.normalize_email(args.admin_email))
                print("Invite sent.")
        except (AccountError, ValueError) as e:
            print("Error:", e)
            return 1
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
