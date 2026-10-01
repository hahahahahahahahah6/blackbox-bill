"""Deterministic join of bill rows with local session activity.

A bill row is 'matched' when at least one local session's activity window
overlaps the row timestamp (within ``window_hours``) AND the model labels
fuzzily agree. Everything else about a row is ignored for matching — the
join is intentionally dumb so its results are auditable.
"""

from datetime import timedelta

from .cursor_csv import models_match


def join_rows(bill_rows, sessions, window_hours=12):
    """Returns (matched, orphans, no_timestamp).

    matched: list of (row, [session, ...], model_agrees). A row is matched
    when at least one local session's activity window overlaps the row
    timestamp (within ``window_hours``); ``model_agrees`` records whether
    any overlapping session's model fuzzily agrees with the billed model.
    orphans: rows with a timestamp but no overlapping local activity.
    no_timestamp: rows without a timestamp (cannot be joined at all).
    """
    matched, orphans, no_timestamp = [], [], []
    window = timedelta(hours=window_hours)
    for row in bill_rows:
        ts = row.get("ts")
        if ts is None:
            no_timestamp.append(row)
            continue
        hits = []
        for s in sessions:
            if s["first_ts"] is None or s["last_ts"] is None:
                continue
            if s["first_ts"] - window <= ts <= s["last_ts"] + window:
                hits.append(s)
        if hits:
            agrees = any(models_match(row.get("model", ""), s.get("model", ""))
                         for s in hits)
            matched.append((row, hits, agrees))
        else:
            orphans.append(row)
    return matched, orphans, no_timestamp


def summarize_join(matched, orphans):
    """Aggregate matched / orphan cost and request counts for reporting."""
    def agg(rows):
        cost = sum(r.get("cost") or 0 for r in rows)
        req = sum(r.get("requests") or 0 for r in rows)
        return cost, req

    m_rows = [r for r, _, _ in matched]
    o_rows = list(orphans)
    m_cost, m_req = agg(m_rows)
    o_cost, o_req = agg(o_rows)
    return {
        "matched_cost": m_cost, "matched_requests": m_req,
        "orphan_cost": o_cost, "orphan_requests": o_req,
        "matched_rows": len(m_rows), "orphan_rows": len(o_rows),
    }
