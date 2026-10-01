"""Rule-based routing-anomaly detection and plain-English explanations.

Every explanation cites the underlying bill rows it was computed from
("3 sessions, 41 requests, $12.40") so the user can verify it. Rules are
heuristics inferred from observed patterns — they are NOT official
Cursor/OpenAI/Anthropic routing documentation.
"""

import re
from collections import defaultdict
from datetime import date, datetime

from .cursor_csv import normalize_model, normalize_pool
from .join import summarize_join

# Minimum evidence thresholds so a single stray row cannot fire a rule.
MIN_REQUESTS = 3
DRAIN_RATIO = 2.0          # other-models spend must be >= 2x cursor-models
ORPHAN_MIN_USD = 0.50       # unattended-burn fires above this
MIDDAY_FIRST_DAY_SHARE = 0.25
MIDDAY_SECOND_DAY_SHARE = 0.60


def _money(x):
    return "$%.2f" % x


def _row_stats(rows):
    cost = sum(r.get("cost") or 0 for r in rows)
    req = sum(r.get("requests") or 0 for r in rows)
    return cost, req


class Explanation:
    def __init__(self, rule_id, title, body, cost=0.0, requests=0,
                 rows=0, detail=""):
        self.rule_id = rule_id
        self.title = title
        self.body = body
        self.cost = cost
        self.requests = requests
        self.rows = rows
        self.detail = detail

    def citation(self):
        return "%d row(s), %d request(s), %s" % (
            self.rows, self.requests, _money(self.cost))

    def to_text(self):
        out = "[%s] %s\n%s\nEvidence: %s." % (
            self.rule_id, self.title, self.body, self.citation())
        if self.detail:
            out += "\n" + self.detail
        return out


def _top_models(rows, limit=3):
    by_model = defaultdict(list)
    for r in rows:
        by_model[r.get("model") or "(unknown model)"].append(r)
    ranked = sorted(by_model.items(),
                    key=lambda kv: sum(x.get("cost") or 0 for x in kv[1]),
                    reverse=True)
    return [(m, *_row_stats(rs), len(rs)) for m, rs in ranked[:limit]]


def rule_pool_drain(rows):
    """'Why your Other Models pool drained first'.

    Fires when other-models pool spend is >= DRAIN_RATIO x the
    first-party pool spend with enough requests to matter — the exact
    shape of the "Cursor Models 16% while Other Models 100%" complaint.
    """
    other = [r for r in rows if normalize_pool(r.get("pool")) == "other-models"]
    first = [r for r in rows if normalize_pool(r.get("pool")) == "cursor-models"]
    if not other or not first:
        return None
    o_cost, o_req = _row_stats(other)
    f_cost, _ = _row_stats(first)
    if o_req < MIN_REQUESTS or f_cost <= 0:
        return None
    if o_cost < DRAIN_RATIO * f_cost:
        return None
    pct = 100.0 * o_cost / (o_cost + f_cost) if o_cost + f_cost else 0
    top = _top_models(other)
    detail = "Largest contributors inside the Other Models pool:\n" + "\n".join(
        "  - %s: %d requests, %s" % (m, req, _money(c))
        for m, c, req, _ in top)
    return Explanation(
        "pool-drain",
        "Why your 'Other Models' pool drained first",
        "Auto-model routing sent most of your billable work to non-Cursor "
        "models. Of your first-party vs Other Models spend, %.0f%% landed "
        "in the Other Models pool (%s there vs %s in Cursor Models). That "
        "is why the Other Models pool hit its limit while the Cursor Models "
        "pool looked barely touched." % (pct, _money(o_cost), _money(f_cost)),
        cost=o_cost, requests=o_req, rows=len(other), detail=detail)


def rule_pool_switch(rows):
    """'The <model> spend landed in pool Y'.

    Fires when one billed model shows up in more than one pool — the
    "Grok Bot billed to Other Models two months in a row" shape.
    """
    pools_by_model = defaultdict(set)
    rows_by_model = defaultdict(list)
    for r in rows:
        m = r.get("model") or "(unknown model)"
        pools_by_model[m].add(normalize_pool(r.get("pool")))
        rows_by_model[m].append(r)
    # Ignore the 'unknown' pool: a missing pool column is not a switch.
    candidates = {m: p for m, p in pools_by_model.items()
                  if len([x for x in p if x != "unknown"]) >= 2}
    if not candidates:
        return None
    # Report the model with the most spend behind the switch.
    model = max(candidates, key=lambda m: _row_stats(rows_by_model[m])[0])
    sub = rows_by_model[model]
    cost, req = _row_stats(sub)
    if req < MIN_REQUESTS:
        return None
    pools = sorted(p for p in candidates[model] if p != "unknown")
    per_pool = defaultdict(list)
    for r in sub:
        per_pool[normalize_pool(r.get("pool"))].append(r)
    detail_lines = []
    for p, rs in sorted(per_pool.items()):
        pc, prq = _row_stats(rs)
        detail_lines.append("  - pool '%s': %d requests, %s" % (p, prq, _money(pc)))
    detail = "\n".join(detail_lines)
    return Explanation(
        "pool-switch",
        "The %s spend landed in unexpected pool(s)" % model,
        "Billed rows for '%s' were attributed to %d different pools: %s. "
        "This is the 'support told me my full pool is from the Grok Bot' "
        "pattern — the same model name gets bucketed into different quota "
        "pools, and the bill does not say why." % (
            model, len(pools), ", ".join("'%s'" % p for p in pools)),
        cost=cost, requests=req, rows=len(sub), detail=detail)


def model_family(name):
    """Coarse model family: strip versions so 'fable-5.1' and 'fable-5.5'
    share a family but 'gemini-4' and 'claude-opus-5.5' do not."""
    base = re.sub(r"[\d.\-]+", "", (name or "").lower())
    return "".join(ch for ch in base if ch.isalpha())


def rule_reroute(matched):
    """'Fable 5.1 requests were routed to 5.5 — here are the N sessions'.

    Fires when the join shows local activity in the billed time window
    but the billed model is a different variant of the SAME model family
    the session used. Cross-family mismatches (Gemini billed, Claude
    used) are not reroutes — those rows are either orphans or pool
    misattribution, handled by other rules.
    """
    cases = []
    for row, sessions, model_agrees in matched:
        if model_agrees:
            continue
        billed = row.get("model") or ""
        for s in sessions:
            used = s.get("model") or ""
            if (billed and used
                    and normalize_model(billed) != normalize_model(used)
                    and model_family(billed) == model_family(used)
                    and model_family(billed)):
                cases.append((row, s, billed, used))
    if not cases:
        return None
    by_pair = defaultdict(list)
    for row, s, billed, used in cases:
        by_pair[(billed, used)].append((row, s))
    # Report the most expensive pair.
    pair = max(by_pair, key=lambda p: sum(r.get("cost") or 0
                                          for r, _ in by_pair[p]))
    billed, used = pair
    rows = [r for r, _ in by_pair[pair]]
    sess_ids = sorted({s["session_id"] for _, s in by_pair[pair]})
    cost, req = _row_stats(rows)
    detail = "Affected local sessions (%d):\n%s" % (
        len(sess_ids), "\n".join("  - %s" % sid for sid in sess_ids[:10]))
    if len(sess_ids) > 10:
        detail += "\n  ... and %d more" % (len(sess_ids) - 10)
    return Explanation(
        "model-reroute",
        "'%s' requests were routed to '%s'" % (used, billed),
        "You asked for '%s' but the bill shows '%s' for the same time "
        "window. This is the silent model-swap shape (like Fable 5.1 being "
        "served by 5.5): the name in your IDE and the name on the invoice "
        "do not match." % (used, billed),
        cost=cost, requests=req, rows=len(rows), detail=detail)


def rule_unattended(orphans):
    """'$X spent while no interactive session was open'.

    Fires when bill rows have no matching local session activity at all —
    the '$40 while I slept' case (background agents, headless runs, or a
    key leaking to another provider).
    """
    cost, req = _row_stats(orphans)
    if cost < ORPHAN_MIN_USD:
        return None
    top = _top_models(orphans)
    detail = "Orphaned spend by model:\n" + "\n".join(
        "  - %s: %d requests, %s" % (m, rq, _money(c))
        for m, c, rq, _ in top)
    return Explanation(
        "unattended-burn",
        "%s spent with no matching local session" % _money(cost),
        "This spend has no corresponding activity in your local Claude "
        "Code / Codex logs around the billed time. Either a headless or "
        "background agent ran while you were away, or a credential leaked "
        "and something else is spending on your account. Check background "
        "agents first, then rotate keys.",
        cost=cost, requests=req, rows=len(orphans), detail=detail)


def rule_midday_switch(rows):
    """'Sudden pool switch on <date>'.

    Fires when a day with low Other-Models share is followed by a day
    where the Other-Models share jumps — the mid-cycle surprise.
    """
    dated = [r for r in rows if r.get("ts") is not None]
    if not dated:
        return None
    by_day = defaultdict(list)
    for r in dated:
        ts = r["ts"]
        by_day[date(ts.year, ts.month, ts.day)].append(r)

    def other_share(day_rows):
        total = sum(x.get("cost") or 0 for x in day_rows)
        if total <= 0:
            return None
        other = sum(x.get("cost") or 0 for x in day_rows
                    if normalize_pool(x.get("pool")) == "other-models")
        return other / total

    days = sorted(by_day)
    for prev, cur in zip(days, days[1:]):
        s_prev, s_cur = other_share(by_day[prev]), other_share(by_day[cur])
        if s_prev is None or s_cur is None:
            continue
        if (s_prev <= MIDDAY_FIRST_DAY_SHARE
                and s_cur >= MIDDAY_SECOND_DAY_SHARE):
            sub = by_day[cur]
            cost, req = _row_stats([r for r in sub
                                    if normalize_pool(r.get("pool"))
                                    == "other-models"])
            return Explanation(
                "midday-pool-switch",
                "Sudden pool switch on %s" % cur.isoformat(),
                "On %s only %.0f%% of spend went to the Other Models pool; "
                "on %s it jumped to %.0f%%. Same usage pattern, different "
                "bucket — this is the mid-cycle 'why is Other Models at "
                "100%% already?' surprise." % (
                    prev.isoformat(), 100 * s_prev,
                    cur.isoformat(), 100 * s_cur),
                cost=cost, requests=req, rows=len(sub))
    return None


def run_all_rules(rows, matched, orphans):
    """Run every anomaly rule. Returns list of Explanation (may be empty)."""
    out = []
    for fn in (rule_pool_drain, rule_pool_switch,
               lambda r: rule_reroute(matched),
               lambda r: rule_unattended(orphans),
               rule_midday_switch):
        try:
            exp = fn(rows)
        except Exception:
            continue  # one broken rule must not kill the report
        if exp is not None:
            out.append(exp)
    return out


def pool_summary(rows):
    """Aggregate cost/requests by canonical pool, then by model."""
    by_pool = defaultdict(list)
    for r in rows:
        by_pool[normalize_pool(r.get("pool"))].append(r)
    summary = {}
    for pool, rs in by_pool.items():
        cost, req = _row_stats(rs)
        models = [(m, c, rq, n) for m, c, rq, n in _top_models(rs, limit=50)]
        summary[pool] = {"cost": cost, "requests": req, "rows": len(rs),
                         "models": models}
    return summary


def daily_summary(rows):
    by_day = defaultdict(list)
    for r in rows:
        ts = r.get("ts")
        key = date(ts.year, ts.month, ts.day).isoformat() if ts else "(no date)"
        by_day[key].append(r)
    out = {}
    for day, rs in sorted(by_day.items()):
        cost, req = _row_stats(rs)
        by_pool = defaultdict(float)
        for r in rs:
            by_pool[normalize_pool(r.get("pool"))] += r.get("cost") or 0
        out[day] = {"cost": cost, "requests": req,
                    "by_pool": dict(by_pool)}
    return out


def join_summary_text(matched, orphans, no_timestamp):
    s = summarize_join(matched, orphans)
    lines = [
        "Local-log join: %d bill row(s) matched a local session, "
        "%d row(s) had no local activity, %d row(s) had no timestamp "
        "and could not be joined." % (
            s["matched_rows"], s["orphan_rows"], len(no_timestamp)),
        "Matched spend: %s across %d requests. "
        "Unmatched spend: %s across %d requests." % (
            _money(s["matched_cost"]), s["matched_requests"],
            _money(s["orphan_cost"]), s["orphan_requests"]),
    ]
    return "\n".join(lines)
