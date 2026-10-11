"""CLI for blackbox-bill.

Exit codes: 0 ok, 2 usage error (missing/unreadable CSV, unknown plan).
Missing local log dirs are warnings, not errors — the bill alone is
still explainable.
"""

import argparse
import os
import sys
from datetime import datetime

from . import __version__
from .attribution import (agent_report_text, by_agent, by_project,
                          load_rate_card, project_report_text)
from .cursor_csv import parse_csv
from .explain import (daily_summary, join_summary_text, pool_summary,
                      run_all_rules)
from .join import join_rows
from .logs import read_all_logs, read_claude_logs
from .tokenizer import dual_line, resolve as resolve_tokenizer
from .whatif import PLANS, reprice, whatif_text


def _parse_date(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError("unparseable date %r (use YYYY-MM-DD)" % value)


def _add_tokenizer_args(p):
    p.add_argument("--tokenizer", default="legacy",
                   choices=["legacy", "claude-4.7"],
                   help="tokenizer the input token counts were measured with "
                        "(default legacy); reports show both figures")
    p.add_argument("--tokenizer-factor", type=float, default=None,
                   help="override the legacy->claude-4.7 conversion factor "
                        "(default 1.3, i.e. ~30%% more tokens)")


def build_parser():
    p = argparse.ArgumentParser(
        prog="blackbox-bill",
        description="Explain where your AI-coding-tool money went. "
                    "Deterministic, local-only, no API keys.")
    p.add_argument("--csv", default=None,
                   help="path to Cursor billing export CSV (bill mode)")
    p.add_argument("--claude-dir", default=None,
                   help="Claude Code projects dir (default ~/.claude/projects)")
    p.add_argument("--codex-dir", default=None,
                   help="Codex sessions dir (default ~/.codex/sessions)")
    p.add_argument("--no-logs", action="store_true",
                   help="skip local session logs entirely (bill-only mode)")
    p.add_argument("--since", default=None, help="only rows on/after YYYY-MM-DD")
    p.add_argument("--until", default=None, help="only rows on/before YYYY-MM-DD")
    p.add_argument("--window-hours", type=float, default=12.0,
                   help="join window for bill-row x session matching (default 12)")
    p.add_argument("--markdown", default=None,
                   help="also write a Markdown report to this path")
    p.add_argument("--whatif", default=None, metavar="PLAN",
                   help="re-price under a different plan: %s (or path via --rates-file)"
                   % ", ".join(sorted(PLANS)))
    p.add_argument("--rates-file", default=None,
                   help="JSON rate card overriding --whatif plan rates")
    _add_tokenizer_args(p)
    p.add_argument("--version", action="version", version="blackbox-bill " + __version__)

    sub = p.add_subparsers(dest="command", metavar="command",
                           help="report: attribution from local logs")
    r = sub.add_parser("report",
                       help="cost attribution from local Claude Code logs "
                            "(no bill CSV needed)")
    r.add_argument("--by-project", action="store_true",
                   help="aggregate tokens/est. cost per project, with per-session detail")
    r.add_argument("--by-agent", action="store_true",
                   help="subagent token share and Top-N subagent-type ranking")
    r.add_argument("--top", type=int, default=10,
                   help="how many subagent types to rank (default 10)")
    r.add_argument("--claude-dir", default=None,
                   help="Claude Code projects dir (default ~/.claude/projects)")
    r.add_argument("--since", default=None, help="only sessions on/after YYYY-MM-DD")
    r.add_argument("--until", default=None, help="only sessions on/before YYYY-MM-DD")
    r.add_argument("--markdown", default=None,
                   help="also write a Markdown report to this path")
    r.add_argument("--rates-file", default=None,
                   help="JSON rate card for cost estimates (illustrative built-in "
                        "otherwise)")
    _add_tokenizer_args(r)
    return p


def filter_dates(rows, since, until):
    out = []
    for r in rows:
        ts = r.get("ts")
        if ts is None:
            out.append(r)  # undated rows are kept but flagged
            continue
        d = ts.date()
        if since and d < since:
            continue
        if until and d > until:
            continue
        out.append(r)
    return out


def render(rows, warnings, sessions, log_warnings, matched, orphans,
           no_timestamp, explanations, whatif_result, tok=("legacy", "", 1.0)):
    L = []
    L.append("blackbox-bill v%s — where the money went" % __version__)
    L.append("=" * 60)

    for w in warnings + log_warnings:
        L.append("warning: " + w)
    if warnings or log_warnings:
        L.append("")

    total_cost = sum(r.get("cost") or 0 for r in rows)
    total_req = sum(r.get("requests") or 0 for r in rows)
    L.append("Bill rows: %d   requests: %d   billed total: $%.2f"
             % (len(rows), total_req, total_cost))
    if any(r.get("cost") is None for r in rows):
        L.append("(some rows have no cost column value — totals are partial)")
    L.append("")

    tok_name, tok_label, tok_factor = tok
    tin = sum(r.get("input_tokens") or 0 for r in rows)
    tout = sum(r.get("output_tokens") or 0 for r in rows)
    if tin or tout:
        L.append("## Tokenizer-aware token figures")
        L.append("Measured under: %s." % tok_label)
        L.append("Input tokens:  %s" % dual_line(tin, tok_name, tok_factor))
        L.append("Output tokens: %s" % dual_line(tout, tok_name, tok_factor))
        L.append("Total tokens:  %s" % dual_line(tin + tout, tok_name, tok_factor))
        L.append("(claude-4.7+ figures are approximations, not re-tokenized "
                 "counts)")
        L.append("")

    L.append("## Pool-normalized summary")
    summary = pool_summary(rows)
    for pool in sorted(summary):
        s = summary[pool]
        L.append("%-14s %6d rows %8d reqs  $%9.2f"
                 % (pool, s["rows"], s["requests"], s["cost"]))
        for m, c, rq, n in s["models"][:5]:
            L.append("    %-34.34s %6d reqs  $%8.2f" % (m, rq, c))
    L.append("")

    L.append("## Per-day")
    for day, d in daily_summary(rows).items():
        pools = ", ".join("%s $%.2f" % (p, c)
                          for p, c in sorted(d["by_pool"].items()))
        L.append("%s  %5d reqs  $%8.2f   (%s)" % (day, d["requests"], d["cost"], pools))
    L.append("")

    L.append("## Local session cross-check")
    L.append(join_summary_text(matched, orphans, no_timestamp))
    if sessions:
        L.append("Local sessions seen: %d (%s)" % (
            len(sessions),
            ", ".join(sorted({s["tool"] for s in sessions}))))
    L.append("")

    L.append("## Explanations")
    if explanations:
        for e in explanations:
            L.append("")
            L.append(e.to_text())
            L.append("")
    else:
        L.append("No anomalies detected. The bill matches local activity "
                 "and pool usage looks ordinary.")
        L.append("")

    if whatif_result is not None:
        L.append("## Plan-switch what-if")
        L.append(whatif_text(whatif_result))
        L.append("")

    L.append("---")
    L.append("Rules are heuristics inferred from observed billing patterns, "
             "not official vendor documentation. Times are local-tz "
             "best-effort; see README limitations.")
    return "\n".join(L)


def to_markdown(text):
    """Lightly convert the terminal report into Markdown."""
    out = ["# blackbox-bill report", ""]
    for line in text.splitlines():
        if line.startswith("## "):
            out.append("")
            out.append(line)
            out.append("")
        elif line.startswith("blackbox-bill v"):
            out.append("## " + line)
            out.append("")
        elif line.startswith("warning:"):
            out.append("> " + line)
        elif line.startswith("[") and "] " in line[:40]:
            out.append("### " + line)
            out.append("")
        else:
            out.append(line)
    return "\n".join(out)


def render_report(args, since, until):
    """Attribution report from local Claude Code logs (no bill CSV)."""
    try:
        tok_name, tok_label, tok_factor = resolve_tokenizer(
            args.tokenizer, args.tokenizer_factor)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2, None

    if not args.by_project and not args.by_agent:
        print("error: report needs --by-project and/or --by-agent",
              file=sys.stderr)
        return 2, None

    sessions, log_warnings = read_claude_logs(args.claude_dir)
    try:
        card = load_rate_card(args.rates_file)
    except (OSError, ValueError) as exc:
        print("error: --rates-file: %s" % exc, file=sys.stderr)
        return 2, None

    L = []
    L.append("blackbox-bill v%s — cost attribution" % __version__)
    L.append("=" * 60)
    L.append("Tokenizer basis: %s. All token figures are shown in both "
             "tokenizer equivalents." % tok_label)
    for w in log_warnings:
        L.append("warning: " + w)
    if log_warnings:
        L.append("")

    if args.by_project:
        projects = by_project(sessions, since, until, card)
        L.append(project_report_text(projects, tok_name, tok_factor, card))
        L.append("")
    if args.by_agent:
        result = by_agent(sessions, since, until, card, top=args.top)
        L.append(agent_report_text(result, tok_name, tok_factor, card))
        L.append("")

    L.append("---")
    L.append("Cost figures are estimates from observed tokens under the "
             "rate card above, not vendor bills. Token figures are measured "
             "counts; tokenizer equivalents are approximations (x%.2g)."
             % tok_factor)
    return 0, "\n".join(L)


def _write_markdown(path, text):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(text) + "\n")
        print("\nMarkdown report written to %s" % path)
        return 0
    except OSError as exc:
        print("error: cannot write %s: %s" % (path, exc), file=sys.stderr)
        return 2


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        since = _parse_date(args.since)
        until = _parse_date(args.until)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.command == "report":
        code, text = render_report(args, since, until)
        if code != 0 or text is None:
            return code
        print(text)
        if args.markdown:
            return _write_markdown(args.markdown, text)
        return 0

    # ---- bill mode (v1 behavior, tokenizer-aware totals added) ----
    if not args.csv:
        print("error: --csv is required (or use the 'report' subcommand)",
              file=sys.stderr)
        return 2
    try:
        tok_name, tok_label, tok_factor = resolve_tokenizer(
            args.tokenizer, args.tokenizer_factor)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    rows, warnings = parse_csv(args.csv)
    if not rows and any("cannot open" in w or "empty" in w for w in warnings):
        for w in warnings:
            print("error: " + w, file=sys.stderr)
        return 2
    rows = filter_dates(rows, since, until)

    sessions, log_warnings = ([], []) if args.no_logs else read_all_logs(
        args.claude_dir, args.codex_dir)

    matched, orphans, no_timestamp = join_rows(rows, sessions,
                                              window_hours=args.window_hours)
    explanations = run_all_rules(rows, matched, orphans)

    whatif_result = None
    if args.whatif:
        try:
            whatif_result = reprice(rows, args.whatif, args.rates_file)
        except (KeyError, OSError, ValueError) as exc:
            print("error: --whatif: %s" % exc, file=sys.stderr)
            return 2

    tok = (tok_name, tok_label, tok_factor)
    text = render(rows, warnings, sessions, log_warnings, matched, orphans,
                  no_timestamp, explanations, whatif_result, tok)
    print(text)

    if args.markdown:
        return _write_markdown(args.markdown, text)
    return 0
