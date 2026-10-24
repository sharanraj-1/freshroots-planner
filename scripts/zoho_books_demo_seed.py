"""One-off: put demo vendors and the 15 seed SKUs into a Zoho Books organisation (case project).

  python scripts/zoho_books_demo_seed.py <one-time code with ZohoBooks.contacts.CREATE/READ and
                                          ZohoBooks.settings.CREATE/READ>

Exchanges the code, creates what is missing (vendors by name, items by SKU), writes
data/zoho_demo/supplier_master.csv for the planner, then revokes the token it was given, so the
create rights do not outlive the setup. Reads ZOHO_* from .env. Never prints secrets.
All names and costs are demo values for the case project.
"""

import csv
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENV = dict(
    line.split("=", 1)
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines()
    if "=" in line and not line.startswith("#")
)
DC = ENV.get("ZOHO_DC", "in")
ORG = ENV["ZOHO_ORGANIZATION_ID"]
API = f"https://www.zohoapis.{DC}/books/v3"

VENDORS = {
    "konkan": ("Konkan Agro Supplies (Demo)", "KONKAN-DEMO", 2),
    "deccan": ("Deccan Staples Co (Demo)", "DECCAN-DEMO", 3),
    "sahyadri": ("Sahyadri Fresh Foods (Demo)", "SAHYADRI-DEMO", 1),
}
# SKU -> (vendor key, demo purchase cost in INR)
DEMO = {
    "SKU001": ("konkan", 18),
    "SKU002": ("deccan", 32),
    "SKU003": ("deccan", 85),
    "SKU004": ("sahyadri", 120),
    "SKU005": ("deccan", 140),
    "SKU006": ("deccan", 210),
    "SKU007": ("konkan", 30),
    "SKU008": ("deccan", 22),
    "SKU009": ("deccan", 160),
    "SKU010": ("konkan", 190),
    "SKU011": ("sahyadri", 45),
    "SKU012": ("deccan", 110),
    "SKU013": ("deccan", 150),
    "SKU014": ("deccan", 95),
    "SKU015": ("konkan", 230),
}


def main(code):
    r = httpx.post(
        f"https://accounts.zoho.{DC}/oauth/v2/token",
        timeout=30,
        data={
            "grant_type": "authorization_code",
            "client_id": ENV["ZOHO_CLIENT_ID"],
            "client_secret": ENV["ZOHO_CLIENT_SECRET"],
            "code": code,
        },
    )
    tok = r.json()
    if "access_token" not in tok:
        raise SystemExit(f"code exchange failed: {r.status_code} {tok.get('error')}")
    h = {"Authorization": f"Zoho-oauthtoken {tok['access_token']}"}
    q = {"organization_id": ORG}
    try:
        orgs = httpx.get(f"{API}/organizations", headers=h, timeout=30).json().get("organizations", [])
        org = next((o for o in orgs if str(o["organization_id"]) == ORG), None)
        print(
            "organisation:", org["name"] if org else f"NOT FOUND among {[o['organization_id'] for o in orgs]}"
        )
        if not org:
            return 1

        existing = (
            httpx.get(
                f"{API}/contacts",
                headers=h,
                params={**q, "contact_type": "vendor", "per_page": 200},
                timeout=30,
            )
            .json()
            .get("contacts", [])
        )
        by_name = {c["contact_name"]: c["contact_id"] for c in existing}
        vendor_ids = {}
        for key, (name, _, _) in VENDORS.items():
            if name in by_name:
                vendor_ids[key] = by_name[name]
                print("vendor exists:", name)
                continue
            body = httpx.post(
                f"{API}/contacts",
                headers=h,
                params=q,
                timeout=30,
                json={
                    "contact_name": name,
                    "company_name": name,
                    "contact_type": "vendor",
                    "notes": "Demo vendor for the Freshroots planner case project",
                },
            ).json()
            if body.get("code") != 0:
                raise SystemExit(f"vendor {name}: {body.get('code')} {body.get('message')}")
            vendor_ids[key] = body["contact"]["contact_id"]
            print("vendor created:", name)

        items = (
            httpx.get(f"{API}/items", headers=h, params={**q, "per_page": 200}, timeout=30)
            .json()
            .get("items", [])
        )
        item_by_sku = {i.get("sku"): i["item_id"] for i in items if i.get("sku")}
        with open(ROOT / "data" / "seed" / "sku_master.csv", newline="", encoding="utf-8-sig") as f:
            skus = list(csv.DictReader(f))
        for s in skus:
            if s["SKU_ID"] in item_by_sku:
                print("item exists:", s["SKU_ID"])
                continue
            cost = DEMO[s["SKU_ID"]][1]
            body = httpx.post(
                f"{API}/items",
                headers=h,
                params=q,
                timeout=30,
                json={
                    "name": s["SKU_Name"],
                    "sku": s["SKU_ID"],
                    "rate": round(cost * 1.35, 2),
                    "purchase_rate": cost,
                    "item_type": "sales_and_purchases",
                    "product_type": "goods",
                    "unit": "pcs",
                    "description": f"Demo item ({s['Category']}) for the Freshroots planner case project",
                },
            ).json()
            if body.get("code") != 0:
                raise SystemExit(f"item {s['SKU_ID']}: {body.get('code')} {body.get('message')}")
            item_by_sku[s["SKU_ID"]] = body["item"]["item_id"]
            print("item created:", s["SKU_ID"], s["SKU_Name"])

        out = ROOT / "data" / "zoho_demo"
        out.mkdir(exist_ok=True)
        with open(out / "supplier_master.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "SKU_ID",
                    "Supplier_ID",
                    "Supplier_Name",
                    "Lead_Time_Weeks",
                    "Zoho_Vendor_ID",
                    "Zoho_Item_ID",
                    "Unit_Cost_INR",
                ]
            )
            for s in skus:
                key, cost = DEMO[s["SKU_ID"]]
                name, sid, lead = VENDORS[key]
                w.writerow([s["SKU_ID"], sid, name, lead, vendor_ids[key], item_by_sku[s["SKU_ID"]], cost])
        print("wrote", out / "supplier_master.csv")
        return 0
    finally:
        # The create rights were for this setup only: revoke the token Zoho issued with them.
        for t in (tok.get("refresh_token"), tok.get("access_token")):
            if t:
                httpx.post(
                    f"https://accounts.zoho.{DC}/oauth/v2/token/revoke", params={"token": t}, timeout=30
                )
        print("setup token revoked")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
