# Open questions for Raghav

The system does not invent rules. Each question below has a current behaviour that stays until
it is answered and the answer is written into `config.json` or this file.

| # | Question | Current behaviour |
|---|---|---|
| 1 | Should the high-demand test compare against the festival-raised baseline? | `trend_test_basis: unadjusted`. The workbench shows the `adjusted` result next to it. |
| 2 | Should festival uplift follow the run month or the month the stock will sell in? | Run month (`festival_basis: run_month`). |
| 3 | When the standard qty leaves cover below minimum (Brown Rice, Peanut Butter), order standard or top up? | Standard qty, with a review flag showing the top-up qty. |
| 4 | Coconut Water is at 4.08 weeks in a festival month with demand up 10%: is PAUSE right? | PAUSE, flagged as within 10% of a threshold. |
| 5 | Do the 6-week and 3-week minimums already include supplier lead time? | Lead time is stored (`supplier_master.csv`) but not used. |
| 6 | Should a missing week of sales stop the run, or count as zero sales? | Warning only. The 12-week average then spans more than 12 calendar weeks. |
| 7 | Do open POs and in-transit stock both count towards cover? | Both count when `open_po.csv` is sent (`cover_includes_open_pos: true`). |
| 8 | Pre-PO check: what counts as "last 3 days of sales/orders" when SAP only sends weekly sales? | Needs a daily orders export (see Phase 6). |
