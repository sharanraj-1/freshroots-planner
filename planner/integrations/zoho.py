"""Zoho Books or Zoho Inventory API: create DRAFT purchase orders, nothing else. Sources: docs/ZOHO.md.

ZOHO_PRODUCT picks the product: "books" (https://www.zoho.com/books/api/v3/) or "inventory"
(https://www.zoho.com/inventory/api/v1/). Both use the same /purchaseorders and /items resources;
they differ in the base path and in the line-item price field.

Two gates: the ZOHO_API_ENABLED deployment flag and the zoho_api_enabled kill switch.
The client allows only POST /purchaseorders and GET reads; every other call raises before any
request is made, so issuing, approving, emailing, editing or deleting a PO is impossible from here.
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime

import httpx
from sqlalchemy import select

from planner import models as m
from planner.control import switches, tripwires
from planner.control.alerts import raise_alert

log = logging.getLogger("planner.zoho")
# product -> (API base path, line-item price field)
PRODUCTS = {"inventory": ("inventory/v1", "purchase_rate"), "books": ("books/v3", "rate")}
RETRIES = 3  # after the first attempt
BACKOFF_S = (1, 2, 4)
PO_NOTES = "Draft from the Freshroots planner. Review and release in Zoho; the planner never issues POs."


class ZohoError(Exception):
    pass


class ForbiddenCall(ZohoError):
    """A call outside the draft-only allowlist."""


def api_enabled():
    return os.environ.get("ZOHO_API_ENABLED", "false").lower() == "true"


def _allowed(method, path):
    if method == "POST":
        return path == "/purchaseorders"
    if method == "GET":
        return path == "/items" or path.startswith("/purchaseorders")
    return False


class ZohoClient:
    def __init__(self, transport=None, env=None, clock=time.monotonic):
        env = env or os.environ
        dc = env.get("ZOHO_DC", "in")
        self.product = env.get("ZOHO_PRODUCT", "inventory").lower()
        if self.product not in PRODUCTS:
            raise ZohoError(f"ZOHO_PRODUCT must be one of {list(PRODUCTS)}")
        base, self.price_field = PRODUCTS[self.product]
        self.api_base = f"https://www.zohoapis.{dc}/{base}"
        self.token_url = f"https://accounts.zoho.{dc}/oauth/v2/token"
        self.org = env.get("ZOHO_ORGANIZATION_ID", "")
        self.creds = {
            k: env.get(f"ZOHO_{k.upper()}", "") for k in ("client_id", "client_secret", "refresh_token")
        }
        self.http = httpx.Client(timeout=30, transport=transport)
        self.clock = clock
        self._token, self._expires = None, 0.0

    def _access_token(self, force=False):
        if self._token and not force and self.clock() < self._expires:
            return self._token
        if not all(self.creds.values()) or not self.org:
            raise ZohoError("Zoho credentials or ZOHO_ORGANIZATION_ID are not set")
        r = self.http.post(self.token_url, data={"grant_type": "refresh_token", **self.creds})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "access_token" not in body:
            raise ZohoError(
                f"Zoho token refresh failed: HTTP {r.status_code} {body.get('error', '')}".strip()
            )
        self._token = body["access_token"]
        self._expires = self.clock() + float(body.get("expires_in", 3600)) - 60
        return self._token

    def request(self, method, path, params=None, json=None):
        if not _allowed(method, path):
            raise ForbiddenCall(f"{method} {path} is not allowed: the planner only creates draft POs")
        params = {"organization_id": self.org, **(params or {})}
        for force in (False, True):  # one token refresh on 401
            headers = {"Authorization": f"Zoho-oauthtoken {self._access_token(force)}"}
            r = self.http.request(method, self.api_base + path, params=params, json=json, headers=headers)
            if r.status_code != 401:
                break
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or body.get("code", 0) != 0:
            raise ZohoError(
                f"Zoho HTTP {r.status_code} code {body.get('code')}: {body.get('message', r.text[:200])}"
            )
        return body

    def item_id_for_sku(self, sku):
        # Inventory filters by exact SKU; Books has no SKU filter, so search and match exactly.
        params = {"sku": sku} if self.product == "inventory" else {"search_text": sku}
        items = [
            i for i in self.request("GET", "/items", params=params).get("items") or [] if i.get("sku") == sku
        ]
        if not items:
            raise ZohoError(f"no Zoho item with SKU {sku}")
        return str(items[0]["item_id"])

    def create_draft_po(self, vendor_id, line_items, reference_number, notes):
        body = {
            "vendor_id": vendor_id,
            "line_items": line_items,
            "reference_number": reference_number,
            "notes": notes,
            "date": datetime.now().date().isoformat(),
        }
        po = self.request("POST", "/purchaseorders", json=body).get("purchaseorder") or {}
        if not po.get("purchaseorder_id"):
            raise ZohoError("Zoho returned no purchaseorder_id")
        return po


def _with_retries(session, cfg, fn, sleep):
    last = None
    for attempt in range(RETRIES + 1):
        try:
            result = fn()
            session.add(m.IntegrationCall(system="zoho", ok=True, called_at=datetime.now()))
            session.commit()
            return result
        except ForbiddenCall:
            raise
        except (ZohoError, httpx.HTTPError) as e:
            last = e
            session.add(
                m.IntegrationCall(system="zoho", ok=False, called_at=datetime.now(), detail=str(e)[:500])
            )
            session.commit()
            if tripwires.check_zoho(session, cfg):
                break
            if attempt < RETRIES:
                sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
    raise ZohoError(str(last))


def push_run(session, run, cfg, client=None, sleep=time.sleep):
    """Send a run's approved lines to Zoho as draft POs, one per vendor.

    Returns {"created": [...], "csv_only": [...], "failed": [...]}.
    """
    if not api_enabled():
        raise ZohoError("ZOHO_API_ENABLED is not true: use the CSV export")
    if not switches.is_enabled(session, switches.ZOHO_API) or not switches.is_enabled(
        session, switches.PLANNER
    ):
        raise ZohoError("the Zoho API switch is off: use the CSV export")
    if run.status != "completed":
        raise ZohoError(f"run {run.id} is {run.status}")
    suppliers = {s.sku_id: s for s in session.scalars(select(m.Supplier))}
    result = {"created": [], "csv_only": [], "failed": []}
    groups = defaultdict(list)
    for rec in run.recommendations:
        d = rec.decisions[-1] if rec.decisions else None
        if not d or d.action not in ("approve", "edit") or d.qty <= 0 or d.zoho_po_id:
            continue
        s = suppliers.get(rec.sku_id)
        if not s or not s.zoho_vendor_id:
            d.zoho_status, d.zoho_error = "csv_only", "no Zoho vendor for this SKU"
            result["csv_only"].append(rec.sku_id)
            continue
        groups[s.zoho_vendor_id].append((rec, d, s))
    session.commit()

    client = client or ZohoClient()
    ref = f"FR-{run.run_date}-run{run.id}"
    for vendor, lines in groups.items():
        if not switches.is_enabled(session, switches.ZOHO_API):  # a tripwire fired during this push
            for rec, d, _ in lines:
                d.zoho_status, d.zoho_error = "csv_only", "Zoho API switched off during the push"
                result["csv_only"].append(rec.sku_id)
            session.commit()
            continue
        try:
            items = []
            for rec, d, s in lines:
                item_id = s.zoho_item_id or _with_retries(
                    session, cfg, lambda r=rec: client.item_id_for_sku(r.sku_id), sleep
                )
                line = {"item_id": item_id, "quantity": d.qty}
                if s.unit_cost_inr is not None:
                    line[client.price_field] = s.unit_cost_inr
                items.append(line)
            po = _with_retries(
                session, cfg, lambda v=vendor, i=items: client.create_draft_po(v, i, ref, PO_NOTES), sleep
            )
        except ZohoError as e:
            for rec, d, _ in lines:
                d.zoho_status, d.zoho_error = "failed", str(e)[:500]
                result["failed"].append(rec.sku_id)
            session.commit()
            continue
        status = str(po.get("status", "")).lower()
        for rec, d, _ in lines:
            d.zoho_po_id, d.zoho_status, d.zoho_error = str(po["purchaseorder_id"]), "draft_created", None
            result["created"].append({"sku_id": rec.sku_id, "zoho_po_id": d.zoho_po_id})
        session.commit()
        if status != "draft":
            raise_alert(
                session,
                cfg,
                "zoho",
                f"Zoho PO {po['purchaseorder_id']} came back with status '{status}', not draft. "
                "Check it in Zoho now; the planner never asks for anything but a draft.",
                severity="critical",
                run_id=run.id,
            )
    log.info("zoho push for run %s: %s", run.id, {k: len(v) for k, v in result.items()})
    return result
