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


def build_review_queue(rows):
    return [
        {k: r.get(k) for k in QUEUE_FIELDS}
        | {"weekly_sales_16w": dict(zip(r["weeks"], r["units"], strict=True))}
        for r in rows
        if needs_review(r)
    ]
