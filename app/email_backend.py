"""Email backends (PO4): a tiny abstraction over SMTP-or-console delivery.

MISSION §5 says "SMTP via a configurable provider (start with a fake/console
backend in dev, real SMTP in prod)". This module exposes:

- ``EmailMessage``: a dataclass capturing what we want to send (sender,
  recipient, subject, html_body, list of attachments).
- ``EmailBackend``: a Protocol with one method, ``send(message)``. Anything
  implementing it can be used by the route.
- ``ConsoleEmailBackend``: appends every sent message to a process-level
  outbox so dev / tests can assert against deliveries without wiring up a
  fake SMTP server. Also writes a short summary to stdout for dev visibility.
- ``SmtpEmailBackend``: stdlib ``smtplib`` over ``email.message.EmailMessage``,
  with optional STARTTLS + login.
- ``get_email_backend(settings)``: factory returning the configured backend.

The factory is called per-request from the route — backends carry no shared
mutable state besides the console outbox (which lives at module level so it
survives across requests in dev / tests).
"""

from __future__ import annotations

import smtplib
import sys
from dataclasses import dataclass, field
from email.message import EmailMessage as StdlibEmailMessage
from typing import Protocol

from app.config import Settings


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content_type: str  # e.g. "application/pdf"
    content: bytes


@dataclass(frozen=True)
class EmailMessage:
    """A view of an outbound email — backend-agnostic.

    ``html_body`` is the only body shape v1 uses (the PO send route renders a
    short HTML message and attaches the PDF). A future plain-text variant can
    add a ``text_body`` field without changing callers.
    """

    sender: str
    recipient: str
    subject: str
    html_body: str
    attachments: list[EmailAttachment] = field(default_factory=list)


class EmailBackend(Protocol):
    def send(self, message: EmailMessage) -> None:
        """Deliver ``message`` or raise on failure."""


# ---------------------------------------------------------------------------
# Console backend — used in dev + tests
# ---------------------------------------------------------------------------
#
# A module-level list keeps every message a console backend ever sent during
# this process. Tests read it via ``console_outbox()`` and clear it via
# ``clear_console_outbox()``. The list never grows unboundedly in prod (prod
# uses ``SmtpEmailBackend``), and dev workflows aren't long-running enough for
# the leak to matter.

_CONSOLE_OUTBOX: list[EmailMessage] = []


def console_outbox() -> list[EmailMessage]:
    """Return the live outbox list. Mutating the returned list is fine."""
    return _CONSOLE_OUTBOX


def clear_console_outbox() -> None:
    """Drop every recorded message. Used by test fixtures."""
    _CONSOLE_OUTBOX.clear()


class ConsoleEmailBackend:
    """Records every message in-memory + prints a one-line summary to stdout.

    No real network call. The outbox lets tests assert "the right message was
    sent" without standing up a fake SMTP server.
    """

    def send(self, message: EmailMessage) -> None:
        _CONSOLE_OUTBOX.append(message)
        # Single-line dev summary. Avoid dumping the html body (likely long).
        attachment_summary = ", ".join(
            f"{a.filename} ({len(a.content)} bytes)" for a in message.attachments
        )
        print(
            f"[email:console] to={message.recipient} "
            f"subject={message.subject!r} "
            f"attachments=[{attachment_summary}]",
            file=sys.stdout,
        )


# ---------------------------------------------------------------------------
# SMTP backend — used in prod
# ---------------------------------------------------------------------------


class SmtpEmailBackend:
    """Deliver via stdlib ``smtplib`` (STARTTLS + auth optional).

    Required ``settings``: ``smtp_host``, ``smtp_port``, ``smtp_from``.
    Optional: ``smtp_user`` / ``smtp_password`` (login when both set);
    ``smtp_use_tls`` (STARTTLS when true).
    """

    def __init__(self, settings: Settings) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._user = settings.smtp_user
        self._password = settings.smtp_password
        self._use_tls = settings.smtp_use_tls

    def send(self, message: EmailMessage) -> None:
        msg = StdlibEmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = message.sender
        msg["To"] = message.recipient
        msg.set_content("This message contains an HTML body.")
        msg.add_alternative(message.html_body, subtype="html")
        for attachment in message.attachments:
            maintype, _, subtype = attachment.content_type.partition("/")
            msg.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        with smtplib.SMTP(self._host, self._port) as smtp:
            if self._use_tls:
                smtp.starttls()
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_email_backend(settings: Settings) -> EmailBackend:
    """Return the backend named by ``settings.email_backend``.

    ``console`` (default) → in-memory + stdout. ``smtp`` → real SMTP.
    Anything else raises — fail loud rather than silently mis-deliver.
    """
    name = (settings.email_backend or "console").strip().lower()
    if name == "console":
        return ConsoleEmailBackend()
    if name == "smtp":
        return SmtpEmailBackend(settings)
    raise RuntimeError(f"unknown email backend {name!r}; expected 'console' or 'smtp'")
