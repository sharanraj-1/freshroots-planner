# SAP export contracts

The ingest job reads one folder (`SAP_INBOUND_DIR`) of UTF-8 CSV files. Column names must match
exactly (order does not matter). A folder is loaded all-or-nothing: one error rejects every file,
and the `ingest_report` table records each file's row count, SHA-256 checksum, errors and warnings.

| File | Required | Columns |
|---|---|---|
| `sku_master.csv` | yes | SKU_ID, SKU_Name, Category, Perishability, Standard_Reorder_Qty, Unit_of_Measure, Distributor_ID |
| `distributor_master.csv` | yes | Distributor_ID, Distributor_Name, Region, Status |
| `calendar.csv` | yes | Year, Month, Year_Month (YYYY-MM), Is_Festival_Month (TRUE/FALSE) |
| `weekly_sales.csv` | yes | SKU_ID, Week (ISO, e.g. 2026-W42), Units_Sold |
| `current_inventory.csv` | yes | SKU_ID, Location, Units_On_Hand, As_Of_Date (YYYY-MM-DD) |
| `open_po.csv` | no | PO_Number, SKU_ID, Units_Open, Status (Open/In_Transit), Expected_Date |
| `supplier_master.csv` | no | SKU_ID, Supplier_ID, Supplier_Name, Lead_Time_Weeks, Zoho_Vendor_ID, Zoho_Item_ID, Unit_Cost_INR |

The two optional files are not in the seed data. Their columns are a proposal for the SAP team to confirm.
`open_po.csv` and `supplier_master.csv` are current-state snapshots, replaced on each load.
Sales and inventory are upserted by (SKU, week) and (SKU, location, date).

## Errors (the load and the run stop)
- a required file is missing, a header does not match, or a value has the wrong type
- negative sales or negative stock; duplicate sales, inventory or SKU rows
- a sales, inventory or open-PO row for a SKU that is not in `sku_master`
- a SKU with no inventory or no sales history; an unknown perishability class
- inventory older than `data_freshness.max_inventory_age_days`, or dated after the run date
- latest sales week older than `data_freshness.max_sales_age_days`

## Warnings (logged with the run)
- missing weeks inside a SKU's history, or a SKU with no rows for the latest week
- distributor not in the master; distributor region outside `home_regions`
- no open-PO data (cover uses on-hand stock only); no supplier data, or SKUs with no Zoho vendor
