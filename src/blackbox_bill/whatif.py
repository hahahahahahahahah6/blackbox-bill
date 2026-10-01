"""Plan-switch what-if simulator.

Re-prices the observed usage (tokens per bill row) under a different
plan's rate card and prints the delta vs the actual billed cost.

The bundled rate cards are ILLUSTRATIVE placeholders, not Cursor's
real prices — real plan pricing changes and this tool has no network.
Pass --rates-file with your own JSON to re-price with real numbers:

    {
      "label": "My actual Pro plan",
      "monthly_included_usd": 20.0,
      "rates": {
        "cursor-models": {"input": 3.0, "output": 15.0},
        "other-models":  {"input": 5.0, "output": 20.0},
        "api-key":       {"input": 3.0, "output": 15.0},
        "unknown":       {"input": 3.0, "output": 15.0}
      }
    }

Rates are USD per 1M tokens. ``monthly_included_usd`` is subtracted from
the re-priced total (floored at 0) to model an included allowance.
"""

import json

from .cursor_csv import normalize_pool

# Illustrative rate cards. NOT real Cursor pricing. See module docstring.
PLANS = {
    "cursor:pro": {
        "label": "Cursor Pro (illustrative rates — NOT real pricing)",
        "monthly_included_usd": 20.0,
        "rates": {
            "cursor-models": {"input": 3.0, "output": 15.0},
            "other-models": {"input": 5.0, "output": 20.0},
            "api-key": {"input": 3.0, "output": 15.0},
            "unknown": {"input": 3.0, "output": 15.0},
        },
    },
    "cursor:ultra": {
        "label": "Cursor Ultra (illustrative rates — NOT real pricing)",
        "monthly_included_usd": 200.0,
        "rates": {
            "cursor-models": {"input": 2.0, "output": 10.0},
            "other-models": {"input": 4.0, "output": 16.0},
            "api-key": {"input": 3.0, "output": 15.0},
            "unknown": {"input": 3.0, "output": 15.0},
        },
    },
    "api-only": {
        "label": "Pure API-key usage (illustrative rates)",
        "monthly_included_usd": 0.0,
        "rates": {
            "cursor-models": {"input": 3.0, "output": 15.0},
            "other-models": {"input": 3.0, "output": 15.0},
            "api-key": {"input": 3.0, "output": 15.0},
            "unknown": {"input": 3.0, "output": 15.0},
        },
    },
}


def load_plan(name, rates_file=None):
    """Resolve a plan by name, optionally overridden by a JSON rates file."""
    if rates_file:
        with open(rates_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("label", rates_file), {
            "label": data.get("label", rates_file),
            "monthly_included_usd": float(data.get("monthly_included_usd", 0.0)),
            "rates": data["rates"],
        }
    if name not in PLANS:
        raise KeyError("unknown plan %r (available: %s)"
                       % (name, ", ".join(sorted(PLANS))))
    plan = PLANS[name]
    return plan["label"], plan


def reprice(rows, plan_name, rates_file=None):
    """Re-price bill rows under a plan. Returns a result dict."""
    label, plan = load_plan(plan_name, rates_file)
    rates = plan["rates"]
    included = plan.get("monthly_included_usd", 0.0)

    per_pool = {}
    actual_total = 0.0
    has_actual = False
    for r in rows:
        pool = normalize_pool(r.get("pool"))
        rate = rates.get(pool, rates.get("unknown", {"input": 0, "output": 0}))
        tin = r.get("input_tokens") or 0
        tout = r.get("output_tokens") or 0
        est = tin / 1e6 * rate.get("input", 0) + tout / 1e6 * rate.get("output", 0)
        bucket = per_pool.setdefault(pool, {"estimated": 0.0, "actual": 0.0,
                                            "rows": 0, "requests": 0})
        bucket["estimated"] += est
        bucket["rows"] += 1
        bucket["requests"] += r.get("requests") or 0
        if r.get("cost") is not None:
            bucket["actual"] += r["cost"]
            actual_total += r["cost"]
            has_actual = True

    gross = sum(b["estimated"] for b in per_pool.values())
    net = max(0.0, gross - included)
    return {
        "plan": plan_name,
        "label": label,
        "per_pool": per_pool,
        "gross_estimated": gross,
        "included": included,
        "net_estimated": net,
        "actual_total": actual_total if has_actual else None,
        "delta": (net - actual_total) if has_actual else None,
    }


def whatif_text(result):
    lines = ["What-if: %s" % result["label"],
             "  (illustrative rates — pass --rates-file for real numbers)"]
    for pool in sorted(result["per_pool"]):
        b = result["per_pool"][pool]
        lines.append("  %-14s %6d rows %6d reqs  re-priced %8.2f"
                     % (pool, b["rows"], b["requests"], b["estimated"]))
    lines.append("  re-priced gross: $%.2f" % result["gross_estimated"])
    lines.append("  included allowance: $%.2f" % result["included"])
    lines.append("  re-priced net:   $%.2f" % result["net_estimated"])
    if result["actual_total"] is not None:
        lines.append("  actual billed:   $%.2f" % result["actual_total"])
        d = result["delta"]
        verb = "more" if d > 0 else "less"
        sign = "+" if d > 0 else "-"
        lines.append("  delta: %s$%.2f %s than the actual bill"
                     % (sign, abs(d), verb))
    else:
        lines.append("  (no cost column in CSV — delta vs actual bill "
                     "unavailable)")
    return "\n".join(lines)
