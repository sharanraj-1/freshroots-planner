"""Grounding check: every number in an AI brief must be in its input or derivable from it.

Derivable means one step of arithmetic (sum, difference, product, ratio, or a ratio as a
percentage or percentage change) from:
  - the payload's own figures (stock, averages, cover, quantities, flags) and the rule settings,
  - a figure the brief has already grounded (so shown working, step by step, passes),
  - the weekly sales: any single week, the sum or mean of consecutive weeks, and a week compared
    with the 12-week, 4-week or festival-adjusted average.
A figure matches when it equals a candidate at the precision it is written (227 for 226.7,
1.21 for 1.214); percentages may also be within 1% of the candidate.

The suggested quantity range must also be shown: low and high each appear in the input or in the
brief's own calculation text.

Limits, measured on the seed review cases (docs/AI_ASSISTANT.md): an invented figure between 100
and 2,000 passes by coincidence 13-20% of the time, above 2,000 about 1%; figures under 100 and
percentages are rarely caught, because almost every small number is one step from some input.
"""

import re
from bisect import bisect_left, insort
from itertools import combinations

NUMBER = re.compile(r"(?<![\w.])-?\d+(?:,\d+)*(?:\.\d+)?%?")
INJECTION = re.compile(
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|rules)"
    r"|disregard (?:the|all|your)|system prompt|you are now|new instructions|reply with|respond with"
    r"|<\s*/?\s*(?:system|instructions?)\s*>|\bassistant\s*:|\bdeveloper\s*:",
    re.IGNORECASE,
)
AVERAGES = ("avg_12w", "avg_4w", "adjusted_weekly")


def numbers_in(text):
    """[(token, value)] for every number written in the text; '1,820' is 1820, '25%' is 25."""
    return [(t, float(t.rstrip("%").replace(",", ""))) for t in NUMBER.findall(text)]


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def _values(obj):
    """Every number in a JSON value, as (token, value), in document order."""
    for v in _walk(obj):
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int | float):
            yield str(v), float(v)
        elif isinstance(v, str):
            yield from numbers_in(v)


def _ops(a, b):
    out = {a + b, abs(a - b), a * b}
    for x, y in ((a, b), (b, a)):
        if y:
            r = x / y
            out.update((r, r * 100, abs(r - 1) * 100))
    return out


class Candidates:
    def __init__(self, payload, constants=()):
        weekly = payload.get("weekly_sales_16w") or {}
        series = [float(v) for v in weekly.values() if isinstance(v, int | float)]
        self.base = sorted(
            {v for _, v in _values({k: v for k, v in payload.items() if k != "weekly_sales_16w"})}
            | {float(c) for c in constants}
            | {v for _, v in _values(list(weekly))}
        )
        values = set(self.base) | set(series) | {b * 100 for b in self.base} | set(range(len(series) + 1))
        for a, b in combinations(self.base, 2):
            values |= _ops(a, b)
        for i in range(len(series)):
            total = 0.0
            for j in range(i, len(series)):
                total += series[j]
                values.update((total, total / (j - i + 1)))
        for avg in (payload.get(k) for k in AVERAGES):
            if isinstance(avg, int | float) and avg:
                for w in series:
                    values.update((abs(w - avg), w / avg * 100, abs(w / avg - 1) * 100, w / avg))
        self.values = sorted(values)

    def add(self, v):
        """A grounded brief figure becomes an operand for later figures."""
        new = set(_ops(v, 0.0)) | {v}
        for b in self.base:
            new |= _ops(v, b)
        for x in new:
            insort(self.values, x)
        self.base.append(v)

    def match(self, token, v):
        decimals = len(token.rstrip("%").split(".")[1]) if "." in token else 0
        tol = 0.5 * 10**-decimals + 1e-9
        if token.endswith("%"):
            tol = max(tol, abs(v) * 0.01)
        targets = (v, v / 100) if token.endswith("%") else (v,)
        for t in targets:
            i = bisect_left(self.values, t - tol)
            if i < len(self.values) and self.values[i] <= t + tol:
                return True
        return False


def check(brief, payload, constants=()):
    """Returns {"passed", "checked", "ungrounded": [tokens], "qty_range_shown": bool}."""
    cands = Candidates(payload, constants)
    checked, ungrounded = 0, []
    for token, v in _values(brief):
        checked += 1
        if cands.match(token, v):
            cands.add(v)
        else:
            ungrounded.append(token)
    qty_shown = True
    q = brief.get("suggested_qty_range") if isinstance(brief, dict) else None
    if isinstance(q, dict):
        shown = {v for _, v in numbers_in(q.get("calculation", ""))} | {v for _, v in _values(payload)}
        qty_shown = all(any(abs(q[k] - s) <= 0.5 for s in shown) for k in ("low", "high"))
    return {
        "passed": not ungrounded and qty_shown,
        "checked": checked,
        "ungrounded": ungrounded,
        "qty_range_shown": qty_shown,
    }


def find_injection(payload):
    """Instruction-like text inside the data. Returns the offending snippets."""
    return [m.group(0) for v in _walk(payload) if isinstance(v, str) for m in INJECTION.finditer(v)]
