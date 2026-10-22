"""Review queue: the SKUs a planner must look at, as payloads for the exception assistant."""

QUEUE_FIELDS = (
    "sku_id",
    "sku_name",
    "perishability",
    "decision",
    "recommended_qty",
    "exception_reasons",
    "review_flags",
    "stock_on_hand",
    "standard_qty",
    "avg_12w",
    "avg_4w",
    "adjusted_weekly",
    "weeks_cover",
    "min_cover",
    "max_cover",
    "post_order_cover",
    "qty_to_min_cover",
    "festival",
)


def needs_review(row):
    return bool(row["review_flags"] or row["exception_reasons"])


def review_payload(row):
    """What the exception assistant sees for one SKU: the review_queue.json fields."""
    return {k: row.get(k) for k in QUEUE_FIELDS} | {
        "weekly_sales_16w": dict(zip(row.get("weeks") or [], row.get("units") or [], strict=True))
    }


def build_review_queue(rows):
    return [review_payload(r) for r in rows if needs_review(r)]
