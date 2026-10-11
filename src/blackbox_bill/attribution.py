"""Cost attribution: per-project and subagent breakdowns from local logs.

A billing CSV tells you *which pool* the money left from. These reports
answer the questions users actually ask in forums:

- "which project burned my quota?" (per-project attribution over
  ``~/.claude/projects/*/*.jsonl``)
- "how much of my spend was subagents?" (subagent share + Top-N ranking)

Token counts come straight from the transcripts. Dollar figures are
*estimates*: they re-price observed tokens with a rate card. The bundled
card is illustrative; pass ``--rates-file`` with your own numbers.
"""

import json

from .tokenizer import dual_line, fmt_tokens

# Illustrative rates, USD per 1M tokens. NOT real vendor pricing — the tool
# has no network and vendor pricing changes. See --rates-file.
ILLUSTRATIVE_RATES = {
    "label": "illustrative built-in rates (NOT real pricing — use --rates-file)",
    "default": {"input": 3.0, "output": 15.0},
    "patterns": [
        (["opus"], {"input": 15.0, "output": 75.0}),
        (["sonnet"], {"input": 3.0, "output": 15.0}),
        (["haiku"], {"input": 1.0, "output": 5.0}),
        (["gpt-5", "gpt-4"], {"input": 5.0, "output": 20.0}),
        (["gemini"], {"input": 2.0, "output": 12.0}),
        (["grok"], {"input": 3.0, "output": 15.0}),
    ],
}


def load_rate_card(path=None):
    """Load a rate card: {'label', 'default': {input, output}, 'patterns': [...]}.

    JSON format for --rates-file::

        {
          "label": "my actual rates",
          "default": {"input": 3.0, "output": 15.0},
          "patterns": [
            {"match": ["opus"], "input": 15.0, "output": 75.0}
          ]
        }

    Rates are USD per 1M tokens. Without a file, the illustrative
    built-in card is returned (clearly labeled as such).
    """
    if path is None:
        return ILLUSTRATIVE_RATES
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    card = {
        "label": data.get("label", path),
        "default": {
            "input": float(data.get("default", {}).get("input", 3.0)),
            "output": float(data.get("default", {}).get("output", 15.0)),
        },
        "patterns": [],
    }
    for p in data.get("patterns", []) or []:
        match = p.get("match") or []
        if isinstance(match, str):
            match = [match]
        card["patterns"].append((
            [str(m).lower() for m in match],
            {"input": float(p.get("input", 3.0)),
             "output": float(p.get("output", 15.0))},
        ))
    return card


def rate_for(model, card=None):
    """Pick (input_per_1M, output_per_1M) for a model name."""
    card = card or ILLUSTRATIVE_RATES
    name = (model or "").lower()
    for needles, rate in card.get("patterns", []):
        if any(n in name for n in needles):
            return rate["input"], rate["output"]
    d = card.get("default", {"input": 3.0, "output": 15.0})
    return d["input"], d["output"]


def estimate_cost(input_tokens, output_tokens, model, card=None):
    """Estimated USD cost of token usage under a rate card."""
    rin, rout = rate_for(model, card)
    return input_tokens / 1e6 * rin + output_tokens / 1e6 * rout


def _in_window(session, since, until):
    ts = session.get("first_ts") or session.get("last_ts")
    if ts is None:
        return True  # undated sessions are kept, same as undated bill rows
    d = ts.date()
    if since and d < since:
        return False
    if until and d > until:
        return False
    return True


def _session_cost(session, card):
    model = session.get("model") or ""
    return estimate_cost(session.get("input_tokens", 0),
                         session.get("output_tokens", 0), model, card)


def by_project(sessions, since=None, until=None, card=None):
    """Aggregate sessions by project directory.

    Returns a list of project dicts sorted by estimated cost desc:
    {project, tokens, input_tokens, output_tokens, est_cost, requests,
     sessions: [...], subagent_tokens, subagent_runs}
    """
    card = card or ILLUSTRATIVE_RATES
    projects = {}
    for s in sessions:
        if not _in_window(s, since, until):
            continue
        key = s.get("project") or "(unknown project)"
        p = projects.setdefault(key, {
            "project": key,
            "tokens": 0, "input_tokens": 0, "output_tokens": 0,
            "est_cost": 0.0, "requests": 0,
            "sessions": [], "subagent_tokens": 0, "subagent_runs": 0,
        })
        tin = s.get("input_tokens", 0)
        tout = s.get("output_tokens", 0)
        stin = s.get("subagent_input_tokens", 0)
        stout = s.get("subagent_output_tokens", 0)
        p["tokens"] += tin + tout
        p["input_tokens"] += tin
        p["output_tokens"] += tout
        p["est_cost"] += _session_cost(s, card)
        p["requests"] += s.get("requests", 0)
        p["subagent_tokens"] += stin + stout
        p["subagent_runs"] += s.get("subagent_runs", 0)
        p["sessions"].append({
            "session_id": s.get("session_id", ""),
            "model": s.get("model", ""),
            "tokens": tin + tout,
            "est_cost": _session_cost(s, card),
            "requests": s.get("requests", 0),
            "subagent_tokens": stin + stout,
            "subagent_runs": s.get("subagent_runs", 0),
        })
    result = sorted(projects.values(), key=lambda p: -p["est_cost"])
    for p in result:
        p["sessions"].sort(key=lambda x: -x["est_cost"])
    return result


def by_agent(sessions, since=None, until=None, card=None, top=10):
    """Subagent attribution.

    Returns {total_tokens, subagent_tokens, share, runs, agents: [...]}
    where agents is a Top-N list of
    {agent_type, tokens, share_of_subagent, runs, sessions} sorted by
    tokens desc. A session's sidechain tokens are split across the agent
    types it invoked, proportional to invocation counts.
    """
    card = card or ILLUSTRATIVE_RATES
    total_tokens = 0
    subagent_tokens = 0
    runs = 0
    agents = {}
    for s in sessions:
        if not _in_window(s, since, until):
            continue
        tin = s.get("input_tokens", 0)
        tout = s.get("output_tokens", 0)
        st = s.get("subagent_input_tokens", 0) + s.get("subagent_output_tokens", 0)
        total_tokens += tin + tout
        subagent_tokens += st
        types = s.get("subagent_types") or {}
        n_runs = sum(types.values())
        runs += n_runs
        if st and n_runs:
            for agent_type, count in types.items():
                a = agents.setdefault(agent_type, {
                    "agent_type": agent_type, "tokens": 0, "runs": 0,
                    "sessions": set()})
                a["tokens"] += st * count / n_runs
                a["runs"] += count
                a["sessions"].add(s.get("session_id", ""))
    agent_list = []
    for a in agents.values():
        agent_list.append({
            "agent_type": a["agent_type"],
            "tokens": a["tokens"],
            "share_of_subagent": a["tokens"] / subagent_tokens if subagent_tokens else 0.0,
            "runs": a["runs"],
            "sessions": len(a["sessions"]),
        })
    agent_list.sort(key=lambda a: -a["tokens"])
    return {
        "total_tokens": total_tokens,
        "subagent_tokens": subagent_tokens,
        "share": subagent_tokens / total_tokens if total_tokens else 0.0,
        "runs": runs,
        "agents": agent_list[:top],
    }


def project_report_text(projects, tokenizer, tok_factor, card=None):
    card = card or ILLUSTRATIVE_RATES
    L = []
    L.append("## Per-project attribution (local Claude Code logs)")
    L.append("Rate card: %s" % card.get("label", "built-in illustrative"))
    L.append("Tokenizer: %s figures shown both ways; cost estimated from "
             "measured tokens." % tokenizer)
    L.append("")
    if not projects:
        L.append("No sessions found. Check --claude-dir.")
        return "\n".join(L)
    total_tokens = sum(p["tokens"] for p in projects)
    total_cost = sum(p["est_cost"] for p in projects)
    L.append("All projects: %s tokens, est. $%.2f"
             % (dual_line(total_tokens, tokenizer, tok_factor), total_cost))
    L.append("")
    for p in projects:
        share = p["est_cost"] / total_cost if total_cost else 0.0
        sub_share = (p["subagent_tokens"] / p["tokens"]
                     if p["tokens"] else 0.0)
        L.append("%s  —  %s tokens  est. $%.2f (%.1f%% of total)"
                 % (p["project"], dual_line(p["tokens"], tokenizer, tok_factor),
                    p["est_cost"], share * 100))
        L.append("    %d session(s), %d requests, subagent %.1f%% of tokens "
                 "(%d runs)"
                 % (len(p["sessions"]), p["requests"], sub_share * 100,
                    p["subagent_runs"]))
        for s in p["sessions"][:8]:
            L.append("      %-28.28s %-22.22s %9s tok  est. $%7.2f  %4d reqs"
                     % (s["session_id"][:28], s["model"][:22],
                        fmt_tokens(s["tokens"]), s["est_cost"],
                        s["requests"]))
        if len(p["sessions"]) > 8:
            L.append("      ... and %d more session(s)"
                     % (len(p["sessions"]) - 8))
        L.append("")
    return "\n".join(L)


def agent_report_text(result, tokenizer, tok_factor, card=None):
    card = card or ILLUSTRATIVE_RATES
    L = []
    L.append("## Subagent attribution (local Claude Code logs)")
    L.append("Rate card: %s" % card.get("label", "built-in illustrative"))
    L.append("Subagent tokens are measured on sidechain transcript messages; "
             "agent-type split is proportional to Task invocations.")
    L.append("")
    L.append("Subagent share of all tokens: %s tokens = %.1f%% (%d runs)"
             % (dual_line(result["subagent_tokens"], tokenizer, tok_factor),
                result["share"] * 100, result["runs"]))
    L.append("Total measured tokens: %s"
             % dual_line(result["total_tokens"], tokenizer, tok_factor))
    L.append("")
    if not result["agents"]:
        L.append("No subagent activity detected. (Subagents appear as "
                 "sidechain transcript messages with Task tool calls.)")
        return "\n".join(L)
    L.append("Top subagent types:")
    for i, a in enumerate(result["agents"], 1):
        L.append("  %2d. %-24.24s %9s tok  %5.1f%% of subagent  %4d runs  %3d sessions"
                 % (i, a["agent_type"], fmt_tokens(a["tokens"]),
                    a["share_of_subagent"] * 100, a["runs"], a["sessions"]))
    return "\n".join(L)
