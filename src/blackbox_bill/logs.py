"""Readers for local Claude Code and Codex session logs.

Everything is local, no network. Unparseable files are skipped with a
warning recorded on the result — a corrupt log must never hide the bill.
"""

import glob
import json
import os
from collections import Counter
from datetime import datetime


def _parse_ts(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def _empty_session(tool, session_id):
    return {
        "tool": tool,
        "session_id": session_id,
        "first_ts": None,
        "last_ts": None,
        "model": "",
        "models": Counter(),
        "input_tokens": 0,
        "output_tokens": 0,
        "requests": 0,
    }


def _extract_usage(obj):
    """Pull (model, usage-dict) from a loosely-shaped message object."""
    model = ""
    usage = {}
    if isinstance(obj, dict):
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
        model = str(obj.get("model") or msg.get("model") or "")
        u = obj.get("usage") or msg.get("usage") or {}
        if isinstance(u, dict):
            usage = u
    return model, usage


def _fold(s, model, usage, ts):
    if model:
        s["models"][model] += 1
    if isinstance(usage, dict):
        s["input_tokens"] += usage.get("input_tokens", 0) or 0
        s["output_tokens"] += usage.get("output_tokens", 0) or 0
    s["requests"] += 1
    if ts:
        if s["first_ts"] is None or ts < s["first_ts"]:
            s["first_ts"] = ts
        if s["last_ts"] is None or ts > s["last_ts"]:
            s["last_ts"] = ts


def _finalize(s):
    if s["models"]:
        s["model"] = s["models"].most_common(1)[0][0]
    return s


def read_claude_logs(projects_dir=None):
    """Read ~/.claude/projects/**/*.jsonl into per-session summaries.

    Returns (sessions, warnings). Each session dict has tool='claude-code',
    session_id, first_ts, last_ts, model (most common), input_tokens,
    output_tokens, requests.
    """
    base = projects_dir or os.path.expanduser("~/.claude/projects")
    sessions, warnings = [], []
    if not os.path.isdir(base):
        return [], ["claude-code log dir not found: %s" % base]
    files = glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)
    if not files:
        warnings.append("no .jsonl transcripts under %s" % base)
    for path in sorted(files):
        session_id = os.path.splitext(os.path.basename(path))[0]
        s = _empty_session("claude-code", session_id)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    model, usage = _extract_usage(obj)
                    _fold(s, model, usage, _parse_ts(obj.get("timestamp")))
        except OSError as exc:
            warnings.append("cannot read %s: %s" % (path, exc))
            continue
        if s["requests"]:
            sessions.append(_finalize(s))
    return sessions, warnings


def read_codex_logs(sessions_dir=None):
    """Read ~/.codex/sessions/ into per-session summaries.

    Codex session files are JSON/JSONL with varying shapes; we extract
    whatever model/usage/timestamp fields exist and skip the rest.
    Returns (sessions, warnings).
    """
    base = sessions_dir or os.path.expanduser("~/.codex/sessions")
    sessions, warnings = [], []
    if not os.path.isdir(base):
        return [], ["codex log dir not found: %s" % base]
    files = glob.glob(os.path.join(base, "**", "*"), recursive=True)
    files = [f for f in files if os.path.isfile(f)
             and os.path.splitext(f)[1] in (".json", ".jsonl", "")]
    if not files:
        warnings.append("no session files under %s" % base)
    for path in sorted(files):
        session_id = os.path.relpath(path, base).replace(os.sep, "/")
        s = _empty_session("codex", session_id)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            warnings.append("cannot read %s: %s" % (path, exc))
            continue
        objs = []
        if path.endswith(".jsonl") or not path.endswith(".json"):
            for line in text.splitlines():
                line = line.strip()
                if line:
                    try:
                        objs.append(json.loads(line))
                    except ValueError:
                        continue
        else:
            try:
                objs.append(json.loads(text))
            except ValueError:
                warnings.append("skipping unparseable codex file %s" % path)
                continue
        for obj in objs:
            if isinstance(obj, dict):
                model, usage = _extract_usage(obj)
                _fold(s, model, usage,
                      _parse_ts(obj.get("timestamp") or obj.get("time")
                                or obj.get("created_at")))
        if s["requests"]:
            sessions.append(_finalize(s))
    return sessions, warnings


def read_all_logs(claude_dir=None, codex_dir=None):
    sessions, warnings = [], []
    cs, w = read_claude_logs(claude_dir)
    sessions.extend(cs)
    warnings.extend(w)
    xs, w = read_codex_logs(codex_dir)
    sessions.extend(xs)
    warnings.extend(w)
    return sessions, warnings
