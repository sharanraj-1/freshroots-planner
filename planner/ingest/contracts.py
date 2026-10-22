"""Schema contracts for the SAP flat-file exports.

Field aliases are the exact CSV column names. A file whose header does not match is rejected.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Row(BaseModel):
    model_config = ConfigDict(populate_by_name=True, str_strip_whitespace=True, frozen=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v


class SkuRow(_Row):
    sku_id: str = Field(alias="SKU_ID")
    sku_name: str = Field(alias="SKU_Name")
    category: str = Field(alias="Category")
    perishability: str = Field(alias="Perishability")
    standard_reorder_qty: int = Field(alias="Standard_Reorder_Qty", ge=0)
    unit_of_measure: str = Field(alias="Unit_of_Measure")
    distributor_id: str = Field(alias="Distributor_ID")


class DistributorRow(_Row):
    distributor_id: str = Field(alias="Distributor_ID")
    distributor_name: str = Field(alias="Distributor_Name")
    region: str = Field(alias="Region")
    status: str = Field(alias="Status")


class CalendarRow(_Row):
    year: int = Field(alias="Year")
    month: int = Field(alias="Month", ge=1, le=12)
    year_month: str = Field(alias="Year_Month", pattern=r"^\d{4}-\d{2}$")
    is_festival: bool = Field(alias="Is_Festival_Month")


class SalesRow(_Row):
    sku_id: str = Field(alias="SKU_ID")
    week: str = Field(alias="Week", pattern=r"^\d{4}-W\d{2}$")
    units_sold: int = Field(alias="Units_Sold")  # negatives are reported by validation, not here

    @property
    def week_start(self):
        return iso_week_monday(self.week)


class InventoryRow(_Row):
    sku_id: str = Field(alias="SKU_ID")
    location: str = Field(alias="Location")
    units_on_hand: int = Field(alias="Units_On_Hand", ge=0)
    as_of_date: date = Field(alias="As_Of_Date")


class OpenPoRow(_Row):
    po_number: str = Field(alias="PO_Number")
    sku_id: str = Field(alias="SKU_ID")
    units_open: int = Field(alias="Units_Open", ge=0)
    status: Literal["Open", "In_Transit"] = Field(alias="Status")
    expected_date: date | None = Field(None, alias="Expected_Date")


class SupplierRow(_Row):
    sku_id: str = Field(alias="SKU_ID")
    supplier_id: str = Field(alias="Supplier_ID")
    supplier_name: str = Field(alias="Supplier_Name")
    lead_time_weeks: float | None = Field(None, alias="Lead_Time_Weeks", ge=0)
    zoho_vendor_id: str | None = Field(None, alias="Zoho_Vendor_ID")
    zoho_item_id: str | None = Field(None, alias="Zoho_Item_ID")
    unit_cost_inr: float | None = Field(None, alias="Unit_Cost_INR", ge=0)


# file name -> (row contract, required)
FILES = {
    "sku_master.csv": (SkuRow, True),
    "distributor_master.csv": (DistributorRow, True),
    "calendar.csv": (CalendarRow, True),
    "weekly_sales.csv": (SalesRow, True),
    "current_inventory.csv": (InventoryRow, True),
    "open_po.csv": (OpenPoRow, False),
    "supplier_master.csv": (SupplierRow, False),
}


def columns(model):
    return [f.alias for f in model.model_fields.values()]


def iso_week_monday(week):  # "2026-W42" -> Monday of that ISO week
    return datetime.strptime(week + "-1", "%G-W%V-%u").date()


@dataclass
class Dataset:
    """Everything one planning run reads. Optional files are None when SAP did not send them."""

    skus: list[SkuRow]
    distributors: list[DistributorRow]
    calendar: list[CalendarRow]
    sales: list[SalesRow]
    inventory: list[InventoryRow]
    open_pos: list[OpenPoRow] | None = None
    suppliers: list[SupplierRow] | None = None

    def sku_map(self):
        return {s.sku_id: s for s in self.skus}

    def distributor_map(self):
        return {d.distributor_id: d for d in self.distributors}

    def festival_months(self):
        return {c.year_month: c.is_festival for c in self.calendar}

    def series(self):
        """sku -> [(week_start, week, units)] in date order."""
        out = defaultdict(list)
        for r in self.sales:
            out[r.sku_id].append((r.week_start, r.week, r.units_sold))
        return {sku: sorted(rows) for sku, rows in out.items()}

    def latest_inventory(self):
        """sku -> (units on hand summed over locations, as_of_date) for each SKU's latest snapshot date."""
        latest = {}
        for r in self.inventory:
            latest[r.sku_id] = max(latest.get(r.sku_id, r.as_of_date), r.as_of_date)
        stock = defaultdict(int)
        for r in self.inventory:
            if r.as_of_date == latest[r.sku_id]:
                stock[r.sku_id] += r.units_on_hand
        return {sku: (stock[sku], d) for sku, d in latest.items()}

    def open_units(self):
        """sku -> open + in-transit units, or None when there is no open-PO data."""
        if self.open_pos is None:
            return None
        out = defaultdict(int)
        for p in self.open_pos:
            out[p.sku_id] += p.units_open
        return dict(out)
