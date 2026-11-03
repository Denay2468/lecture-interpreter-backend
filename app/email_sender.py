"""Sending the one-time codes. Console in development, SMTP in production.

The console backend prints the whole message, code included, to the server log. That is for development only:
it means nobody is receiving real email.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Protocol

logger = logging.getLogger("email")


class Mailer(Protocol):
    async def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleMailer:
    async def send(self, to: str, subject: str, body: str) -> None:
        logger.warning("EMAIL (console backend, development only; nothing is delivered)\n  To: %s\n  Subject: %s\n  %s",
                       to, subject, body.replace("\n", "\n  "))


class SmtpMailer:
    def __init__(self, host: str, port: int, user: str, password: str, sender: str) -> None:
        self._host, self._port, self._user, self._password, self._sender = host, port, user, password, sender

    async def send(self, to: str, subject: str, body: str) -> None:
        await asyncio.to_thread(self._send_blocking, to, subject, body)

    def _send_blocking(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self._sender, to, subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if self._port == 465:
            with smtplib.SMTP_SSL(self._host, self._port, timeout=15, context=ctx) as s:
                if self._user:
                    s.login(self._user, self._password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self._host, self._port, timeout=15) as s:
                s.starttls(context=ctx)
                if self._user:
                    s.login(self._user, self._password)
                s.send_message(msg)


def build_mailer(cfg) -> Mailer:
    if cfg.email_backend == "smtp":
        if not (cfg.smtp_host and cfg.smtp_from):
            raise RuntimeError("EMAIL_BACKEND=smtp needs SMTP_HOST and SMTP_FROM")
        return SmtpMailer(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user or "", cfg.smtp_password or "", cfg.smtp_from)
    logger.warning("EMAIL_BACKEND is 'console': verification codes are printed in this log, not emailed")
    return ConsoleMailer()


def activation_email(name: str, code: str, ttl_minutes: int) -> tuple[str, str]:
    return ("Your BLB verification code",
            f"Hello {name},\n\nYour Break Language Barrier code is {code}.\nEnter it to finish setting up your account. "
            f"It expires in {ttl_minutes} minutes.\n\nIf you didn't ask for this, ignore this email; nothing will happen.")


def reset_email(name: str, code: str, ttl_minutes: int) -> tuple[str, str]:
    return ("Reset your BLB password",
            f"Hello {name},\n\nYour password reset code is {code}. It expires in {ttl_minutes} minutes.\n\n"
            "If you didn't ask for this, ignore this email; your password has not changed.")


def existing_account_email(name: str) -> tuple[str, str]:
    return ("You already have a BLB account",
            f"Hello {name},\n\nSomeone (hopefully you) tried to register with this address, but it already has an account. "
            "Sign in instead, or use \"Forgot password\" if you can't remember your password.")
