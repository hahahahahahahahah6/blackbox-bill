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
        "project": "",
        "first_ts": None,
        "last_ts": None,
        "model": "",
        "models": Counter(),
        "input_tokens": 0,
        "output_tokens": 0,
        "requests": 0,
        # Subagent attribution: tokens measured on sidechain transcript
        # messages; runs/type counted from Task tool_use blocks.
        "subagent_input_tokens": 0,
        "subagent_output_tokens": 0,
        "subagent_runs": 0,
        "subagent_types": Counter(),
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


def _fold(s, model, usage, ts, is_sidechain=False):
    if model:
        s["models"][model] += 1
    tin = tout = 0
    if isinstance(usage, dict):
        tin = usage.get("input_tokens", 0) or 0
        tout = usage.get("output_tokens", 0) or 0
    s["input_tokens"] += tin
    s["output_tokens"] += tout
    if is_sidechain:
        # Sidechain messages are subagent work: their usage is counted in
        # the session total AND attributed to subagents. (Subagent spawns
        # are real API calls; the parent's ledger must not lose them.)
        s["subagent_input_tokens"] += tin
        s["subagent_output_tokens"] += tout
    s["requests"] += 1
    if ts:
        if s["first_ts"] is None or ts < s["first_ts"]:
            s["first_ts"] = ts
        if s["last_ts"] is None or ts > s["last_ts"]:
            s["last_ts"] = ts


def _task_invocations(obj):
    """Yield subagent_type values from Task tool_use blocks in a message.

    Claude Code transcripts record subagent spawns as assistant messages
    with a ``Task`` tool_use whose input carries ``subagent_type``. The
    tokens for the run itself land on sidechain messages; this counts the
    spawns and their declared types for the Top-N agent ranking.
    """
    if not isinstance(obj, dict):
        return
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use" or block.get("name") != "Task":
            continue
        inp = block.get("input") or {}
        stype = inp.get("subagent_type") if isinstance(inp, dict) else None
        yield str(stype) if stype else "unknown"


def _is_sidechain(obj):
    """True when a transcript entry belongs to a subagent's sidechain."""
    if not isinstance(obj, dict):
        return False
    if obj.get("isSidechain"):
        return True
    msg = obj.get("message")
    return bool(isinstance(msg, dict) and msg.get("isSidechain"))


def _finalize(s):
    if s["models"]:
        s["model"] = s["models"].most_common(1)[0][0]
    return s


def read_claude_logs(projects_dir=None):
    """Read ~/.claude/projects/**/*.jsonl into per-session summaries.

    Returns (sessions, warnings). Each session dict has tool='claude-code',
    session_id, project (directory name under the projects dir), first_ts,
    last_ts, model (most common), input_tokens, output_tokens, requests,
    plus subagent attribution fields (see _empty_session).
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
        rel = os.path.relpath(path, base)
        s["project"] = rel.split(os.sep)[0] if rel else ""
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
                    sidechain = _is_sidechain(obj)
                    _fold(s, model, usage, _parse_ts(obj.get("timestamp")),
                          is_sidechain=sidechain)
                    for stype in _task_invocations(obj):
                        s["subagent_runs"] += 1
                        s["subagent_types"][stype] += 1
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
