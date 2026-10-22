"""Finish the Zoho Inventory connection once the Self Client exists (docs/ZOHO.md).

  python -m planner.integrations.zoho_setup

Reads ZOHO_DC, ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET and ZOHO_REFRESH_TOKEN from .env, refreshes an
access token, finds the Zoho Inventory organisation and writes ZOHO_ORGANIZATION_ID to .env, then
lists vendors and items and writes out/zoho/supplier_master_draft.csv: every seed SKU matched to the
Zoho item with the same SKU. Read-only against Zoho (GET calls only). Never prints secrets.
"""

import csv
import sys

import httpx

from planner.config import ROOT

ENV = ROOT / ".env"


def read_env():
    out = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.removeprefix("export ").partition("=")
        if sep and not key.lstrip().startswith("#"):
            out[key.strip()] = value.strip()
    return out


def set_env(key, value):
    lines = [line for line in ENV.read_text(encoding="utf-8").splitlines() if not line.startswith(f"{key}=")]
    ENV.write_text("\n".join([*lines, f"{key}={value}"]) + "\n", encoding="utf-8")


def access_token(env):
    r = httpx.post(
        f"https://accounts.zoho.{env.get('ZOHO_DC', 'in')}/oauth/v2/token",
        timeout=30,
        data={
            "grant_type": "refresh_token",
            "refresh_token": env["ZOHO_REFRESH_TOKEN"],
            "client_id": env["ZOHO_CLIENT_ID"],
            "client_secret": env["ZOHO_CLIENT_SECRET"],
        },
    )
    body = r.json()
    if "access_token" not in body:
        raise SystemExit(f"token refresh failed: HTTP {r.status_code} {body.get('error', '')}")
    return body["access_token"]


def get(env, token, path, **params):
    r = httpx.get(
        f"https://www.zohoapis.{env.get('ZOHO_DC', 'in')}/inventory/v1{path}",
        params=params,
        timeout=30,
        headers={"Authorization": f"Zoho-oauthtoken {token}"},
    )
    body = r.json()
    if body.get("code", 0) != 0:
        raise SystemExit(f"Zoho {path}: code {body.get('code')} {body.get('message')}")
    return body


def main():
    env = read_env()
    missing = [k for k in ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN") if not env.get(k)]
    if missing:
        print(f"missing in .env: {missing} (see docs/ZOHO.md)", file=sys.stderr)
        return 1
    token = access_token(env)
    print("token refresh: ok")
    orgs = get(env, token, "/organizations").get("organizations", [])
    if not orgs:
        print(
            "No Zoho Inventory organisation on this account yet. Create one at "
            f"https://inventory.zoho.{env.get('ZOHO_DC', 'in')}/ and run this again."
        )
        return 2
    org = next((o for o in orgs if o.get("is_default_org")), orgs[0])
    set_env("ZOHO_ORGANIZATION_ID", org["organization_id"])
    print(f"organisation: {org['name']} ({org.get('currency_code')}) -> ZOHO_ORGANIZATION_ID saved to .env")

    oid = org["organization_id"]
    vendors = [
        c
        for c in get(env, token, "/contacts", organization_id=oid, per_page=200).get("contacts", [])
        if c.get("contact_type") == "vendor"
    ]
    items = get(env, token, "/items", organization_id=oid, per_page=200).get("items", [])
    by_sku = {i.get("sku"): i for i in items if i.get("sku")}
    print(f"vendors: {len(vendors)}  items: {len(items)}  items with a SKU: {len(by_sku)}")
    for v in vendors[:20]:
        print(f"  vendor {v['contact_id']}  {v.get('contact_name')}")

    with open(ROOT / "data" / "seed" / "sku_master.csv", newline="", encoding="utf-8-sig") as f:
        skus = list(csv.DictReader(f))
    out = ROOT / "out" / "zoho"
    out.mkdir(parents=True, exist_ok=True)
    cols = [
        "SKU_ID",
        "Supplier_ID",
        "Supplier_Name",
        "Lead_Time_Weeks",
        "Zoho_Vendor_ID",
        "Zoho_Item_ID",
        "Unit_Cost_INR",
    ]
    with open(out / "supplier_master_draft.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for s in skus:
            item = by_sku.get(s["SKU_ID"], {})
            w.writerow(
                {
                    "SKU_ID": s["SKU_ID"],
                    "Supplier_ID": "",
                    "Supplier_Name": "",
                    "Lead_Time_Weeks": "",
                    "Zoho_Vendor_ID": "",
                    "Zoho_Item_ID": item.get("item_id", ""),
                    "Unit_Cost_INR": item.get("purchase_rate", ""),
                }
            )
    matched = sum(1 for s in skus if s["SKU_ID"] in by_sku)
    print(f"{matched}/{len(skus)} SKUs matched to Zoho items -> {out / 'supplier_master_draft.csv'}")
    print(
        "Fill in the vendor columns, send it as supplier_master.csv with the SAP export, then turn on "
        "ZOHO_API_ENABLED and the zoho_api_enabled switch (docs/ZOHO.md checklist)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
