"""Transactional email for address verification (D18).

There is no mail dependency in this project and no reason to add one for a
single message. The sender therefore has two implementations behind one
interface:

* **SMTP** when `SMTP_HOST` is set, using the standard library. This is what a
  real deployment uses.
* **Outbox** otherwise: the message is written to a table the admin console
  reads, so a developer or an operator can complete a verification by hand and
  no signup silently loses its mail.

The fallback is deliberate. D18 gates the free tier behind verification, so a
deployment with no working mail path must still be able to onboard people
rather than lock every new account out of the product.
"""

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

logger = logging.getLogger("esg.email")

DEFAULT_FROM = "ESG Assistant <no-reply@localhost>"
# Verification links expire in 48 hours and the token itself carries that
# expiry, so the window only has to be explained to the reader.
RESEND_COOLDOWN_SECONDS = 60


def _now():
    return datetime.now(timezone.utc).isoformat()


def smtp_configured():
    return bool(os.environ.get("SMTP_HOST"))


def verification_subject(base_url):
    return "Confirm your email address"


def verification_body(base_url, token):
    link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    return (
        "Confirm your email address\n\n"
        "Open this link to activate your ESG Assistant account:\n\n"
        f"{link}\n\n"
        "The link works once and expires in 48 hours. If you did not create "
        "this account you can ignore this message.\n"
    )


def verification_html(base_url, token):
    link = f"{base_url.rstrip('/')}/verify-email?token={token}"
    return (
        "<p>Confirm your email address</p>"
        f'<p><a href="{link}">Activate your ESG Assistant account</a></p>'
        "<p>The link works once and expires in 48 hours. If you did not "
        "create this account you can ignore this message.</p>"
    )


class OutboxSender:
    """Records messages in the admin store instead of transmitting them."""

    def __init__(self, admin_store):
        self.admin_store = admin_store

    def name(self):
        return "outbox"

    def send(self, to_address, subject, text_body, html_body=None):
        if self.admin_store is None:
            # No store to record into and no relay to send through. The token
            # is still returned to the caller, which surfaces the link, so the
            # account is not lost -- but this must be visible, not silent.
            logger.warning(
                "no SMTP relay and no admin store: verification mail for %s "
                "was not delivered", to_address,
            )
            return False
        self.admin_store.record_email(
            to_address, subject, text_body, html_body=html_body
        )
        logger.info("verification mail recorded in outbox for %s", to_address)
        return True


class SmtpSender:
    """Transmits through the configured relay."""

    def __init__(self, host=None, port=None, user=None, password=None,
                 sender=None, use_tls=True):
        self.host = host or os.environ.get("SMTP_HOST")
        self.port = int(port or os.environ.get("SMTP_PORT", 587))
        self.user = user if user is not None else os.environ.get("SMTP_USER")
        self.password = (
            password if password is not None else os.environ.get("SMTP_PASSWORD")
        )
        self.sender = sender or os.environ.get("SMTP_FROM") or DEFAULT_FROM
        self.use_tls = use_tls

    def name(self):
        return "smtp"

    def send(self, to_address, subject, text_body, html_body=None):
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to_address
        message["Subject"] = subject
        message.set_content(text_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=20) as server:
            if self.use_tls:
                server.starttls()
            if self.user and self.password:
                server.login(self.user, self.password)
            server.send_message(message)
        logger.info("verification mail sent to %s", to_address)


def build_sender(admin_store=None, force_outbox=None):
    """Pick a sender: SMTP when a host is configured, else the outbox.

    `force_outbox` lets tests and operators bypass SMTP without unsetting the
    environment. A configured host that fails at send time is not silently
    swapped for the outbox at call sites: the caller decides, so a failed send
    is visible rather than looking delivered.
    """
    if force_outbox is True or not smtp_configured():
        return OutboxSender(admin_store)
    return SmtpSender()


def send_verification_email(sender, to_address, base_url, token):
    """Returns True when the message went somewhere recoverable."""
    return sender.send(
        to_address,
        verification_subject(base_url),
        verification_body(base_url, token),
        html_body=verification_html(base_url, token),
    )