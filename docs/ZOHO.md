# Zoho Inventory: draft purchase orders

The planner creates purchase orders in Zoho Inventory as **drafts only**. It never issues, sends,
approves, edits or deletes a PO. A person releases each PO inside Zoho.

Everything below comes from the official Zoho documentation, read on 2026-10-22. Items marked
**unconfirmed** must be checked in a test organisation before going live (see the checklist).

## Endpoints and fields used

| What | Value | Source |
|---|---|---|
| API base (India) | `https://www.zohoapis.in/inventory/v1` | https://www.zoho.com/inventory/api/v1/introduction/ |
| Organisation | `organization_id` query parameter on every request | same page, "organization-id" |
| Token endpoint (India) | `POST https://accounts.zoho.in/oauth/v2/token` | https://www.zoho.com/inventory/api/v1/oauth/ |
| Refresh grant | `grant_type=refresh_token`, `refresh_token`, `client_id`, `client_secret` | same |
| Auth header | `Authorization: Zoho-oauthtoken {access_token}` (header only, never a parameter) | same |
| Token life | access token 1 hour; refresh token does not expire (max 20 per user) | same |
| Create PO | `POST /purchaseorders?organization_id=...` JSON body | https://www.zoho.com/inventory/api/v1/purchaseorders/ |
| Required PO fields | `vendor_id`, `line_items` (and `purchaseorder_number`, see below) | same |
| Line item fields | `item_id`, `quantity`, `purchase_rate` (there is no plain `rate`) | same |
| Optional fields we send | `reference_number`, `notes`, `date` | same |
| Item lookup by SKU | `GET /items?organization_id=...&sku=<SKU>` (exact match) | https://www.zoho.com/inventory/api/v1/items/ |
| Errors | body `{"code": int, "message": str}`; 401 invalid token; 429 rate limit | https://www.zoho.com/inventory/api/v1/errors/ |
| Rate limits | 100 requests/minute per organisation; daily limit by plan (1,000 on Free) | introduction page |

### Scopes (Self Client, server-to-server)
`ZohoInventory.purchaseorders.CREATE,ZohoInventory.items.READ`, plus `ZohoInventory.purchaseorders.READ`
if you want to read drafts back. Do **not** grant `.UPDATE` or `.DELETE`: without them the
submit/approve/edit/delete endpoints are refused by Zoho itself.

Zoho documents "Mark as issued" (`POST /purchaseorders/{id}/status/issued`) under the same
`purchaseorders.CREATE` scope we need, so scopes alone cannot prevent issuing. The client in
`planner/integrations/zoho.py` therefore **allows only** `POST /purchaseorders`, `GET /items` and
`GET /purchaseorders...`. Any other method or path raises before a request is made.

Never called, by design (all on the purchase-orders page): `/status/issued`, `/status/cancelled`,
`/submit`, `/approve`, `/approve/final`, `/reject`, bulk submit/approve, `PUT`, `DELETE`.
No "email PO" endpoint is documented for Inventory. If one appears, the allowlist already blocks it.

### Unconfirmed points (check in the test org)
1. **PO number.** The docs list `purchaseorder_number` as required, yet also offer
   `ignore_auto_number_generation` (default false), which implies Zoho numbers POs by default.
   We send no number and rely on auto-numbering. If Zoho rejects that, set the number
   (`FR-<run date>-<vendor>`) and `ignore_auto_number_generation=true`.
2. **Draft status.** The docs do not say a created PO is a draft. Their sample response shows
   `"status_formatted": "Draft"` next to an inconsistent `status`. We read `status` from every
   create response, and anything other than draft raises a critical alert.
3. **Data centre.** The Self Client and refresh token must be created on accounts.zoho.in.

### Sandbox
Zoho Inventory has no general API sandbox. Use a separate organisation on a 14-day trial (India
support can extend it) with its own `organization_id`.

## Configuration
```
ZOHO_API_ENABLED=false        # deployment flag; the zoho_api_enabled kill switch must also be on
ZOHO_DC=in
ZOHO_ORGANIZATION_ID=
ZOHO_CLIENT_ID=
ZOHO_CLIENT_SECRET=
ZOHO_REFRESH_TOKEN=
```
Per SKU, `supplier_master.csv` supplies `Zoho_Vendor_ID` (required to send), `Zoho_Item_ID`
(otherwise looked up by SKU), and `Unit_Cost_INR` (sent as `purchase_rate` when present).

## Behaviour
- **Who sends.** A planner presses "Send drafts to Zoho" (`POST /api/runs/{id}/push-to-zoho`).
  Only approved or changed lines with qty > 0 that are not already in Zoho are sent.
- **Grouping.** One draft PO per vendor per run, with `reference_number` `FR-<run date>-run<id>`.
- **No vendor.** A line without a Zoho vendor is not sent (`csv_only`) and stays in the CSV export.
- **Failures.** Each PO is tried 4 times (first try plus 3 retries, 1s/2s/4s backoff). After that
  its lines are `failed` and stay in the CSV export. Three failed calls in a row turn
  `zoho_api_enabled` off (tripwire), and the rest of the run falls back to CSV.
- **Stored on each decision row.** The Zoho PO id. Lines already in Zoho leave the CSV export,
  and their decision can no longer be changed in the planner.

## Manual test checklist (test organisation)
1. Create a trial org on zoho.in. Add two vendors and three items whose SKUs match the seed
   SKUs. Note their ids.
2. Create a Self Client at https://api-console.zoho.in with the scopes above, generate a code,
   and exchange it for a refresh token. Fill in `.env`.
3. Put the vendor and item ids in `supplier_master.csv`, ingest, run, and approve three lines
   (two vendors, one line with no vendor).
4. `python -m planner.control on zoho_api_enabled --reason "sandbox test"`, set
   `ZOHO_API_ENABLED=true`, and press "Send drafts to Zoho".
5. Check in Zoho: two POs, both **Draft**, with the right items, quantities and reference
   number. Nothing issued, nothing emailed.
6. Check in the planner: decisions show the Zoho PO ids; the CSV export holds only the line
   with no vendor.
7. Break `ZOHO_CLIENT_SECRET` and send again. Expect 3 failures, `zoho_api_enabled` OFF, an
   alert, and the lines in the CSV export.
8. Confirm point 1 above (PO numbering). Record the result here.

## Status (2026-10-22)
- **Self Client created** in the India API console (api-console.zoho.in), scopes
  `ZohoInventory.purchaseorders.CREATE`, `purchaseorders.READ`, `items.READ`, `contacts.READ`,
  `settings.READ`. No UPDATE or DELETE scope.
- **Refresh token issued and stored** in the local `.env` (`ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`,
  `ZOHO_REFRESH_TOKEN`, `ZOHO_DC=in`). Token refresh tested: OK.
- **Blocked on:** the Zoho account has **no Zoho Inventory organisation** yet
  (`GET /organizations` returns none; inventory.zoho.in opens the organisation setup form).
- **Organisation creation refused by Zoho** (case project, demo details): `POST /api/v1/organizations`
  returned HTTP 400, code 6063 "Action cannot be completed. For assistance, please contact support",
  for two different organisation names. This is an account-level block that only Zoho support can lift.
- **Decision for the case project:** the Zoho API stays off (`ZOHO_API_ENABLED=false`). Approved lines go
  out through the draft-PO CSV export, which Zoho Inventory imports. The API integration remains
  implemented and covered by mocked tests (`tests/test_zoho.py`).
- **If an organisation becomes available:** create it at https://inventory.zoho.in, add vendors and items
  (item SKUs = planner SKU_IDs), then run `python -m planner.integrations.zoho_setup`. It saves
  `ZOHO_ORGANIZATION_ID` and drafts `out/zoho/supplier_master_draft.csv`. Then follow the
  checklist above from step 3. `ZOHO_API_ENABLED` stays `false` until then.
