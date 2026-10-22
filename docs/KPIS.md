# KPIs

SQL for the planner database. The queries marked `-- portable` run on both Postgres and SQLite,
and `tests/test_handover.py` runs them against the seed data. The week bucketing in the last two
queries is written for Postgres, with the SQLite form in a comment.

"Current decision" means the latest `planner_decision` row for a recommendation; earlier rows
are history.

## 1. Cycle time: run finished → last planner decision
```sql
-- portable
SELECT r.id AS run_id, r.run_date, r.finished_at,
       MAX(d.decided_at) AS last_decision,
       COUNT(DISTINCT d.recommendation_id) AS decided,
       COUNT(DISTINCT rec.id) AS recommendations
FROM planning_run r
JOIN recommendation rec ON rec.run_id = r.id
LEFT JOIN planner_decision d ON d.recommendation_id = rec.id
WHERE r.status = 'completed'
GROUP BY r.id, r.run_date, r.finished_at
ORDER BY r.run_date;
```
Hours to decide (Postgres): `EXTRACT(EPOCH FROM MAX(d.decided_at) - r.finished_at) / 3600`.
SQLite: `(julianday(MAX(d.decided_at)) - julianday(r.finished_at)) * 24`.

## 2. Recommendations accepted unedited
```sql
-- portable
SELECT r.id AS run_id, r.run_date,
       COUNT(*) AS decided,
       SUM(CASE WHEN d.action = 'approve' THEN 1 ELSE 0 END) AS approved_unedited,
       ROUND(100.0 * SUM(CASE WHEN d.action = 'approve' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_unedited
FROM planning_run r
JOIN recommendation rec ON rec.run_id = r.id
JOIN planner_decision d ON d.recommendation_id = rec.id
WHERE d.id = (SELECT MAX(d2.id) FROM planner_decision d2 WHERE d2.recommendation_id = d.recommendation_id)
GROUP BY r.id, r.run_date
ORDER BY r.run_date;
```

## 3. Exception and review rate per run
```sql
-- portable
SELECT r.id AS run_id, r.run_date, COUNT(*) AS skus,
       SUM(CASE WHEN rec.decision = 'EXCEPTION' THEN 1 ELSE 0 END) AS exceptions,
       SUM(CASE WHEN rec.needs_review THEN 1 ELSE 0 END) AS need_review,
       ROUND(100.0 * SUM(CASE WHEN rec.needs_review THEN 1 ELSE 0 END) / COUNT(*), 1) AS pct_review
FROM planning_run r
JOIN recommendation rec ON rec.run_id = r.id
WHERE r.status IN ('completed', 'held')
GROUP BY r.id, r.run_date
ORDER BY r.run_date;
```

## 4. Stockout incidents per SKU per week
A stockout is an inventory snapshot with zero units on hand.
```sql
SELECT sku_id, date_trunc('week', as_of_date)::date AS week, COUNT(*) AS stockout_days
-- SQLite: date(as_of_date, 'weekday 0', '-6 days') AS week
FROM inventory_snapshot
WHERE units_on_hand = 0
GROUP BY sku_id, week
ORDER BY week, sku_id;
```

## 5. Overstock incidents per SKU per week
An overstock is a run where the SKU's cover was above its window (rule decision PAUSE, or an
EXCEPTION for rising demand while overstocked).
```sql
SELECT rec.sku_id, date_trunc('week', r.run_date)::date AS week, COUNT(*) AS overstock_runs
-- SQLite: date(r.run_date, 'weekday 0', '-6 days') AS week
FROM recommendation rec
JOIN planning_run r ON r.id = rec.run_id
WHERE r.status = 'completed' AND rec.decision IN ('PAUSE', 'EXCEPTION')
GROUP BY rec.sku_id, week
ORDER BY week, rec.sku_id;
```

## 6. Shadow mode
While the planner runs alongside the manual process, compare each run with Raghav's own orders:
```bash
python -m planner.jobs.shadow_report --run-id 7 --actual raghav_orders_2026-10-19.csv
```
This writes `out/shadow/run7.md` and `.csv` with order / no-order agreement, units on each side,
and the SKUs where they disagree.
