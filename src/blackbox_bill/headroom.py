"""Dispatch pre-flight headroom dashboard.

v0.2 explained bills after the fact ("why the Other Models pool drained
first"). v0.3 answers the question one Offrun Show HN user put bluntly:
"surface per-account rate limit headroom BEFORE I dispatch, not after
I'm stuck mid task."

The idea: quota limits are not in the billing export (no vendor
publishes remaining quota in a CSV), so you declare them once in a
small JSON file and blackbox-bill subtracts your observed usage from
them — per account, per canonical pool — before you dispatch the next
task. An optional dispatch simulator then re-prices a planned task
under each account's plan rate card (reusing the v0.2 what-if rate
cards) and tells you which account can absorb it.

Limits file format (JSON):

    {
      "accounts": [
        {
          "name": "cursor-pro",
          "plan": "cursor:pro",
          "limits": {
            "cursor-models": 500.0,
            "other-models": 125.0,
            "api-key": null
          }
        },
        {
          "name": "shared-api-key",
          "plan": "api-only",
          "limits": {"api-key": 200.0},
          "match": {"column": "account", "value": "shared-api-key"}
        }
      ]
    }

Limits are USD caps per canonical pool (see cursor_csv.normalize_pool);
``null`` means unlimited. ``plan`` names a rate card from whatif.PLANS
(or any name when used with --rates-file in dispatch mode). ``match``
optionally partitions bill rows across accounts by a CSV column value;
exactly one account may omit it and becomes the default for unclaimed
rows. All arithmetic is local and deterministic — this never phones
home; limits are your own numbers.
"""

import json

from .cursor_csv import normalize_pool
from .whatif import load_plan

# Remaining quota below this fraction of the limit counts as LOW.
LOW_RATIO = 0.25
# Bar width in the dashboard rendering.
_BAR_WIDTH = 20

_STATUS_RANK = {"UNLIMITED": 0, "OK": 1, "LOW": 2, "EXHAUSTED": 3}


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def load_limits(path):
    """Parse a limits JSON file. Returns the accounts list.

    Raises ValueError with a human-readable message on any structural
    problem (bad JSON, missing accounts, duplicate names, bad limits).
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ValueError("limits file not found: %s" % path)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("cannot read limits file %s: %s" % (path, exc))

    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise ValueError("limits file must be a JSON object with an "
                         "'accounts' list")
    accounts = data["accounts"]
    if not accounts:
        raise ValueError("limits file declares no accounts")
    seen = set()
    for i, acct in enumerate(accounts):
        where = "accounts[%d]" % i
        if not isinstance(acct, dict):
            raise ValueError("%s must be an object" % where)
        name = acct.get("name")
        if not name or not isinstance(name, str):
            raise ValueError("%s needs a string 'name'" % where)
        if name in seen:
            raise ValueError("duplicate account name %r" % name)
        seen.add(name)
        limits = acct.get("limits")
        if not isinstance(limits, dict) or not limits:
            raise ValueError("account %r needs a non-empty 'limits' object "
                             "(pool -> USD cap, null for unlimited)" % name)
        for pool, cap in limits.items():
            canon = normalize_pool(pool)
            if cap is not None and not _is_number(cap):
                raise ValueError("account %r: limit for pool %r must be a "
                                 "number or null" % (name, pool))
            if _is_number(cap) and cap < 0:
                raise ValueError("account %r: limit for pool %r cannot be "
                                 "negative" % (name, pool))
            if canon != pool:
                limits[canon] = limits.pop(pool)
        match = acct.get("match")
        if match is not None:
            if (not isinstance(match, dict)
                    or not isinstance(match.get("column"), str)
                    or "value" not in match):
                raise ValueError("account %r: 'match' must be "
                                 "{'column': ..., 'value': ...}" % name)
        plan = acct.get("plan")
        if plan is not None and not isinstance(plan, str):
            raise ValueError("account %r: 'plan' must be a string" % name)
    return accounts


def partition_rows(rows, accounts):
    """Split bill rows across accounts.

    Returns (rows_by_account, unclaimed_rows). A row belongs to the
    account whose ``match`` spec it satisfies; an account without a
    ``match`` spec is the default for unclaimed rows. Raises ValueError
    if the specs overlap or cannot cover the rows unambiguously.
    """
    with_match = [a for a in accounts if a.get("match")]
    without = [a for a in accounts if not a.get("match")]
    if len(without) > 1:
        raise ValueError("%d accounts lack a 'match' spec — only one may "
                         "be the default; give the others a match column "
                         "or merge them" % len(without))
    default = without[0] if without else None

    def claims(acct, row):
        spec = acct.get("match")
        if spec is None:
            return False
        col, val = spec["column"], spec["value"]
        # 'account' is not a logical billing field, so match on the raw
        # CSV cell (falling back to the parsed row for logical columns).
        cell = row.get("raw", {}).get(col, row.get(col))
        return cell is not None and str(cell).strip() == str(val).strip()

    rows_by_account = {a["name"]: [] for a in accounts}
    unclaimed = []
    for row in rows:
        hit = [a for a in with_match if claims(a, row)]
        if len(hit) > 1:
            raise ValueError("row %r matches more than one account "
                             "(%s) — tighten your 'match' specs"
                             % (row, ", ".join(a["name"] for a in hit)))
        if hit:
            rows_by_account[hit[0]["name"]].append(row)
        elif default is not None:
            rows_by_account[default["name"]].append(row)
        else:
            unclaimed.append(row)
    return rows_by_account, unclaimed


def _status(remaining, limit):
    if limit is None:
        return "UNLIMITED"
    if remaining <= 0:
        return "EXHAUSTED"
    if limit > 0 and remaining / limit < LOW_RATIO:
        return "LOW"
    return "OK"


def compute_headroom(rows_by_account, accounts):
    """Per-account, per-pool headroom from consumed spend vs declared limits.

    Returns a result dict with per-account pool entries:
    consumed, limit, remaining (None when unlimited), pct remaining and
    status (OK / LOW / EXHAUSTED / UNLIMITED).
    """
    warnings = []
    out_accounts = []
    for acct in accounts:
        rows = rows_by_account.get(acct["name"], [])
        by_pool = {}
        for r in rows:
            by_pool.setdefault(normalize_pool(r.get("pool")), 0.0)
            by_pool[normalize_pool(r.get("pool"))] += r.get("cost") or 0.0
        pools = {}
        for pool, limit in acct["limits"].items():
            consumed = by_pool.get(pool, 0.0)
            remaining = None if limit is None else limit - consumed
            pct = (100.0 * remaining / limit
                   if limit not in (None, 0) else None)
            pools[pool] = {
                "consumed": consumed,
                "limit": limit,
                "remaining": remaining,
                "pct_remaining": pct,
                "status": _status(remaining, limit),
            }
        unseen = sorted(set(by_pool) - set(pools))
        if unseen:
            warnings.append("account %r: usage in pool(s) %s has no declared "
                            "limit — not shown" % (acct["name"],
                                                   ", ".join(unseen)))
        out_accounts.append({
            "name": acct["name"],
            "plan": acct.get("plan"),
            "pools": pools,
            "rows": len(rows),
            "total_consumed": sum(by_pool.values()),
        })
    return {"accounts": out_accounts, "warnings": warnings}


def _bar(pct):
    if pct is None:
        return "[" + "?" * _BAR_WIDTH + "]"
    filled = max(0, min(_BAR_WIDTH, int(round(_BAR_WIDTH * pct / 100.0))))
    return "[" + "#" * filled + "-" * (_BAR_WIDTH - filled) + "]"


def _cap(x):
    return "unlimited" if x is None else "$%.2f" % x


def headroom_text(result, had_usage):
    """Render the dashboard. ``had_usage``: was a --csv supplied at all."""
    L = ["## Dispatch pre-flight headroom"]
    if not had_usage:
        L.append("(no --csv given: showing declared limits only; "
                 "re-run with a billing export to subtract usage)")
        L.append("")
    for acct in result["accounts"]:
        plan_note = "plan %s" % acct["plan"] if acct["plan"] else "no plan set"
        L.append("account '%s' (%s, %d bill row(s))"
                 % (acct["name"], plan_note, acct["rows"]))
        L.append("  %-14s %10s %10s %10s   %s  %s"
                 % ("pool", "consumed", "limit", "remaining",
                    "remaining " + " " * 12, "status"))
        for pool in sorted(acct["pools"]):
            h = acct["pools"][pool]
            pct = h["pct_remaining"]
            pct_s = ("%.1f%%" % pct) if pct is not None else "n/a"
            L.append("  %-14s %10s %10s %10s   %s %6s  %s"
                     % (pool, _cap(h["consumed"]), _cap(h["limit"]),
                        _cap(h["remaining"]), _bar(pct), pct_s, h["status"]))
        L.append("")
    if result["warnings"]:
        for w in result["warnings"]:
            L.append("warning: " + w)
        L.append("")
    return "\n".join(L)


def _estimate_cost(plan_label_plan, task_in, task_out, pool):
    label, plan = plan_label_plan
    rate = plan["rates"].get(pool, plan["rates"].get("unknown"))
    if rate is None:
        return None, label
    cost = (task_in / 1e6) * rate.get("input", 0) + \
           (task_out / 1e6) * rate.get("output", 0)
    return cost, label


def simulate_dispatch(headroom_result, accounts, task_in, task_out,
                      pool="other-models", rates_file=None):
    """Price a planned task on every account and check it against headroom.

    ``task_in``/``task_out`` are token estimates for the planned dispatch;
    ``pool`` is the canonical pool the task would land in. Each account is
    priced under its declared plan's rate card (the same cards the v0.2
    what-if simulator uses). Returns estimates sorted best-first plus a
    recommendation: the first account that can absorb the task.
    """
    pool = normalize_pool(pool)
    estimates = []
    for acct, hres in zip(accounts, headroom_result["accounts"]):
        entry = {"account": acct["name"], "plan": acct.get("plan"),
                 "cost": None, "label": None,
                 "fits": None, "projected_remaining": None,
                 "projected_status": None, "note": None}
        h = hres["pools"].get(pool)
        if h is None:
            entry["note"] = "no declared limit for pool %r" % pool
        elif not acct.get("plan"):
            entry["note"] = "no plan set — cannot price the task"
        else:
            try:
                cost, label = _estimate_cost(
                    load_plan(acct["plan"], rates_file),
                    task_in, task_out, pool)
            except (KeyError, OSError, ValueError) as exc:
                entry["note"] = "plan error: %s" % exc
                cost, label = None, None
            entry["cost"] = cost
            entry["label"] = label
            if cost is not None:
                rem = h["remaining"]
                if rem is None:
                    entry["fits"] = True
                    entry["note"] = "unlimited quota"
                else:
                    entry["fits"] = rem >= cost
                    entry["projected_remaining"] = rem - cost
                    entry["projected_status"] = _status(
                        entry["projected_remaining"], h["limit"])
        estimates.append(entry)

    def sort_key(e):
        # Fits first, then highest projected remaining; unpriceable last.
        if e["fits"] is True:
            pr = e["projected_remaining"]
            return (0, -(pr if pr is not None else float("inf")))
        if e["fits"] is False:
            return (1, 0)
        return (2, 0)

    estimates.sort(key=sort_key)
    recommendation = next((e["account"] for e in estimates
                           if e["fits"] is True), None)
    return {
        "task_in": task_in,
        "task_out": task_out,
        "pool": pool,
        "estimates": estimates,
        "recommendation": recommendation,
    }


def dispatch_text(sim):
    """Render the dispatch simulation."""
    L = ["## Dispatch simulation"]
    L.append("Planned task: %s input + %s output tokens -> pool '%s'"
             % (_fmt_tokens(sim["task_in"]), _fmt_tokens(sim["task_out"]),
                sim["pool"]))
    L.append("")
    for e in sim["estimates"]:
        name = "  '%s'" % e["account"]
        if e["cost"] is None:
            L.append("%-22s cannot price: %s" % (name, e["note"]))
            continue
        if e["fits"] is None:
            L.append("%-22s est %s — %s" % (name, _cap(e["cost"]), e["note"]))
        elif e["fits"]:
            proj = ("unlimited" if e["projected_remaining"] is None
                    else _cap(e["projected_remaining"]))
            L.append("%-22s est %s  fits, projected remaining %s (%s)"
                     % (name, _cap(e["cost"]), proj,
                        e["projected_status"] or "UNLIMITED"))
        else:
            L.append("%-22s est %s  DOES NOT FIT (remaining %s)"
                     % (name, _cap(e["cost"]), _cap(e["projected_remaining"])))
    L.append("")
    if sim["recommendation"]:
        L.append("RECOMMEND: dispatch on '%s' — it has headroom for this "
                 "task." % sim["recommendation"])
    else:
        L.append("WARNING: no account has headroom for this task under the "
                 "declared limits. Top up, switch plan, or shrink the task "
                 "before you dispatch — not after you're stuck mid task.")
    return "\n".join(L)


def _fmt_tokens(n):
    if n is None:
        return "?"
    n = int(n)
    if n >= 1_000_000:
        return "%.3gM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.3gK" % (n / 1_000)
    return str(n)
