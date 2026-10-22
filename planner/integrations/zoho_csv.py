"""Zoho Inventory purchase-order import file. Every line is a Draft; nothing here releases a PO."""

import csv

HEADER = ["Item SKU", "Item Name", "Quantity", "Vendor", "Status", "Reason", "Needs Review"]


def write_draft_po_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            if r["decision"] == "REORDER":
                w.writerow(
                    [
                        r["sku_id"],
                        r["sku_name"],
                        r["recommended_qty"],
                        "TO BE ASSIGNED",
                        "Draft",
                        r["rule_trace"][-1],
                        "Yes" if r["review_flags"] else "No",
                    ]
                )
