"""Lenient parser for Cursor billing export CSVs.

Real exports vary in columns, order, casing, and even whether the cost
or pool column exists. We fuzzy-match headers against known aliases,
tolerate ragged rows, and fail soft: missing columns become None plus a
warning, never a crash.
"""

import csv
from datetime import datetime

# Logical field -> list of accepted header spellings (lowercased, stripped).
COLUMN_ALIASES = {
    "ts": [
        "date", "datetime", "timestamp", "time", "created", "created_at",
        "request_time", "requesttime", "day", "usage_date", "started",
        "started_at",
    ],
    "model": [
        "model", "model_name", "modelname", "requested_model",
        "billed_model", "served_model",
    ],
    "input_tokens": [
        "input_tokens", "prompt_tokens", "tokens_in", "input", "prompt",
        "prompttokens",
    ],
    "output_tokens": [
        "output_tokens", "completion_tokens", "tokens_out", "output",
        "completion", "completiontokens",
    ],
    "cost": [
        "cost", "cost_usd", "amount", "price", "spend_usd", "usd", "charge",
        "total_cost",
    ],
    "pool": [
        "pool", "quota_pool", "bucket", "quota_bucket", "plan_pool",
        "usage_pool", "pool_name",
    ],
    "requests": [
        "requests", "request_count", "calls", "count", "num_requests",
        "numrequests",
    ],
}

_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d",
)


def _norm_header(name):
    """Lowercase, strip, and drop separators so 'Model Name' == 'model_name'."""
    return "".join(ch for ch in name.strip().lower() if ch.isalnum())


def map_columns(headers):
    """Map raw header names to logical fields. Returns (mapping, unmatched)."""
    # Normalize the aliases the same way headers are normalized, so
    # "input_tokens" (header) matches "input_tokens" (alias) even after
    # separators are stripped.
    alias_to_field = {}
    for logical, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            alias_to_field.setdefault(_norm_header(a), logical)
    mapping = {}
    unmatched = []
    seen = set()
    for h in headers:
        key = _norm_header(h)
        field = alias_to_field.get(key)
        if field is not None and field not in seen:
            seen.add(field)
            mapping[h] = field
        else:
            unmatched.append(h)
    return mapping, unmatched


def parse_float(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value):
    f = parse_float(value)
    return int(f) if f is not None else None


def parse_timestamp(value):
    """Best-effort timestamp parse. Returns naive datetime or None.

    Times are interpreted as the machine's local timezone; the bill and
    the local session logs must be read on the same machine for the join
    to make sense (see README limitations).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Epoch seconds (Cursor-style exports sometimes use these).
    if text.isdigit() and len(text) >= 10:
        try:
            return datetime.fromtimestamp(int(text[:10]))
        except (OverflowError, OSError, ValueError):
            pass
    # ISO with timezone offset -> drop tz, keep local wall time.
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_csv(path):
    """Parse a Cursor billing export.

    Returns (rows, warnings). Each row is a dict with keys:
    ts, model, pool, input_tokens, output_tokens, cost, requests, raw.
    Missing columns yield None values and a warning, never an exception.
    """
    warnings = []
    rows = []
    try:
        fh = open(path, "r", newline="", encoding="utf-8-sig")
    except OSError as exc:
        return [], ["cannot open CSV %s: %s" % (path, exc)]
    with fh:
        reader = csv.reader(fh)
        headers = None
        for raw_row in reader:
            if not any(cell.strip() for cell in raw_row):
                continue  # blank line
            if headers is None:
                headers = raw_row
                continue
            mapping, _ = map_columns(headers)
            rec = {"ts": None, "model": "", "pool": "", "input_tokens": None,
                   "output_tokens": None, "cost": None, "requests": None,
                   "raw": dict(zip(headers, raw_row))}
            for idx, header in enumerate(headers):
                if idx >= len(raw_row):
                    continue  # ragged short row
                field = mapping.get(header)
                if not field:
                    continue
                value = raw_row[idx]
                if field == "ts":
                    rec["ts"] = parse_timestamp(value)
                elif field == "model":
                    rec["model"] = str(value).strip()
                elif field == "pool":
                    rec["pool"] = str(value).strip()
                elif field == "requests":
                    rec["requests"] = parse_int(value)
                elif field == "cost":
                    rec["cost"] = parse_float(value)
                elif field == "input_tokens":
                    rec["input_tokens"] = parse_float(value)
                elif field == "output_tokens":
                    rec["output_tokens"] = parse_float(value)
            rows.append(rec)

    if headers is None:
        return [], ["CSV %s is empty" % path]
    mapping, unmatched = map_columns(headers)
    present = set(mapping.values())
    for logical in ("ts", "model", "input_tokens", "output_tokens", "cost",
                    "pool", "requests"):
        if logical not in present:
            warnings.append(
                "column missing from CSV: %s (looked for: %s)"
                % (logical, ", ".join(COLUMN_ALIASES[logical][:4])))
    if unmatched:
        warnings.append("ignored %d unrecognized column(s): %s"
                        % (len(unmatched), ", ".join(unmatched[:6])))
    return rows, warnings


def normalize_pool(pool_name):
    """Map a free-form pool label to one of the three canonical pools."""
    name = (pool_name or "").strip().lower()
    if not name:
        return "unknown"
    if "other" in name:
        return "other-models"
    if "api" in name and "key" in name:
        return "api-key"
    if "cursor" in name or "first" in name or "pro" in name or "ultra" in name:
        return "cursor-models"
    return "unknown"


def normalize_model(model_name):
    """Loose model normalization for display and fuzzy matching."""
    return "".join(ch for ch in (model_name or "").lower() if ch.isalnum())


def models_match(a, b):
    """True if two model labels plausibly refer to the same model."""
    na, nb = normalize_model(a), normalize_model(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na in nb or nb in na
