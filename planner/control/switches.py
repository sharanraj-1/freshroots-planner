"""Kill switches. Every job and endpoint asks is_enabled() first.

Fail closed: a missing switch row, or any error reading one, counts as OFF.
Only people in config `switch_admins` can flip a switch, always with a reason, and every flip is
audited. Tripwires may turn switches off but never on.
"""

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from planner import models as m

log = logging.getLogger("planner.control")

PLANNER = "planner_enabled"
SCHEDULED_RUNS = "scheduled_runs_enabled"
AI_ASSISTANT = "ai_assistant_enabled"
ZOHO_API = "zoho_api_enabled"
PRE_PO_CHECK = "pre_po_check_enabled"
SWITCHES = (PLANNER, SCHEDULED_RUNS, AI_ASSISTANT, ZOHO_API, PRE_PO_CHECK)
# Zoho starts off: it is turned on deliberately once the Zoho org is configured.
DEFAULTS = {PLANNER: True, SCHEDULED_RUNS: True, AI_ASSISTANT: True, ZOHO_API: False, PRE_PO_CHECK: True}


class SwitchError(Exception):
    pass


class NotAdmin(SwitchError):
    pass


def is_enabled(session, name):
    try:
        row = session.get(m.ControlSwitch, name)
        return bool(row and row.enabled)
    except SQLAlchemyError:
        log.exception("could not read switch %s; treating it as OFF", name)
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        return False


def read_all(session):
    """name -> row (or None when missing). Raises if the table cannot be read."""
    rows = {r.name: r for r in session.scalars(select(m.ControlSwitch))}
    return {name: rows.get(name) for name in SWITCHES}


def flip(session, name, enabled, reason, user, cfg, source="cli"):
    if name not in SWITCHES:
        raise SwitchError(f"unknown switch '{name}'; one of {list(SWITCHES)} or 'all'")
    if not (reason or "").strip():
        raise SwitchError("a reason is required to flip a switch")
    if source == "tripwire":
        if enabled:
            raise SwitchError("tripwires can only turn switches off; a person turns them back on")
    elif user not in cfg["switch_admins"]:
        raise NotAdmin(f"'{user}' is not in switch_admins")
    now = datetime.now()
    row = session.get(m.ControlSwitch, name)
    old = row.enabled if row else None
    if row is None:
        row = m.ControlSwitch(name=name, enabled=enabled, updated_at=now, updated_by=user, reason=reason)
        session.add(row)
    row.enabled, row.updated_at, row.updated_by, row.reason = enabled, now, user, reason
    session.add(
        m.SwitchAudit(
            name=name,
            old_value=old,
            new_value=enabled,
            changed_by=user,
            changed_at=now,
            reason=reason,
            source=source,
        )
    )
    session.commit()
    log.warning("switch %s %s -> %s by %s (%s): %s", name, old, enabled, user, source, reason)
    return row


def off_all(session, reason, user, cfg, source="cli"):
    return [flip(session, name, False, reason, user, cfg, source) for name in SWITCHES]


def install_defaults(session):
    """Create any missing switch at its default. Never changes an existing switch."""
    now = datetime.now()
    for name, enabled in DEFAULTS.items():
        if session.get(m.ControlSwitch, name) is None:
            session.add(
                m.ControlSwitch(
                    name=name, enabled=enabled, updated_at=now, updated_by="install", reason="initial default"
                )
            )
            session.add(
                m.SwitchAudit(
                    name=name,
                    old_value=None,
                    new_value=enabled,
                    changed_by="install",
                    changed_at=now,
                    reason="initial default",
                    source="install",
                )
            )
    session.commit()
