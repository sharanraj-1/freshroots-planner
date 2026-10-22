"""Alerts: stored in the alert table, logged, and emailed to config `alert_recipients` over SMTP.

SMTP settings come from the environment: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD,
SMTP_FROM, SMTP_STARTTLS (true). Email failure never stops the caller; it is recorded on the alert.
"""

import logging
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage

from sqlalchemy import select

from planner import models as m

log = logging.getLogger("planner.alerts")


class EmailNotConfigured(Exception):
    pass


def send_email(subject, body, recipients):
    host = os.environ.get("SMTP_HOST")
    if not host:
        raise EmailNotConfigured("SMTP_HOST is not set")
    if not recipients:
        raise EmailNotConfigured("config alert_recipients is empty")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "planner@localhost"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as smtp:
        if os.environ.get("SMTP_STARTTLS", "true").lower() == "true":
            smtp.starttls()
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(msg)


def raise_alert(session, cfg, kind, message, severity="warning", run_id=None, key=None):
    """Store, log and email one alert. With a key, an unresolved alert with the same key is not repeated."""
    if key:
        existing = session.scalar(
            select(m.Alert).where(m.Alert.key == key, m.Alert.resolved_at.is_(None)).limit(1)
        )
        if existing:
            return existing
    alert = m.Alert(
        kind=kind, severity=severity, message=message, run_id=run_id, key=key, created_at=datetime.now()
    )
    session.add(alert)
    log.log(logging.CRITICAL if severity == "critical" else logging.WARNING, "ALERT %s: %s", kind, message)
    try:
        send_email(f"[Freshroots planner] {severity.upper()}: {kind}", message, cfg["alert_recipients"])
        alert.emailed = True
    except Exception as e:  # noqa: BLE001 - an alert must never fail because email failed
        alert.email_error = f"{type(e).__name__}: {e}"
        log.warning("alert email not sent: %s", alert.email_error)
    session.commit()
    return alert
