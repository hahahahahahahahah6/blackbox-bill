"""Smoke tests for blackbox-bill. Run: python3 tests/test_blackbox_bill.py"""

import json
import os
import subprocess
import sys
import tempfile

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
ENV_BASE = dict(os.environ, PYTHONPATH=SRC + os.pathsep +
                os.environ.get("PYTHONPATH", ""))

PASS = []


def check(name, cond, detail=""):
    PASS.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name
          + (" — " + detail if detail and not cond else ""))


def run_cli(args, cwd="/tmp", env_extra=None):
    env = dict(ENV_BASE)
    env.update(env_extra or {})
    p = subprocess.run(
        [sys.executable, "-m", "blackbox_bill"] + args,
        capture_output=True, text=True, cwd=cwd, env=env, timeout=30)
    return p.returncode, p.stdout, p.stderr


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# --- fixtures -------------------------------------------------------------

CSV_CLEAN = """date,model,pool,input_tokens,output_tokens,cost_usd,requests
2026-09-28 10:00:00,claude-opus-5.5,Cursor Models,100000,20000,3.50,10
2026-09-28 11:00:00,claude-opus-5.5,Cursor Models,100000,20000,3.50,10
2026-09-29 10:00:00,claude-opus-5.5,Cursor Models,50000,10000,1.75,5
"""

CSV_RAGGED = (
    "\ufeff"  # BOM
    "Model Name,Timestamp,Quota Bucket,Prompt Tokens,Completion Tokens,Amount,Calls,Extra Junk\n"
    "grok-4,2026-09-28 10:00:00,Other Models,50000,10000,1.20,4,zzz\n"
    "\n"  # blank line
    "grok-4,2026-09-28 11:00:00,Other Models,50000,10000,1.20\n"  # short row
    "claude-opus-5.5,2026-09-28 12:00:00,Cursor Models,100000,20000,3.50,10,zzz,trailing1,trailing2\n"  # long row
)

CSV_NO_COST = """date,model,pool,input_tokens,output_tokens
2026-09-28 10:00:00,gpt-5,Other Models,100000,20000
"""

CSV_DRAIN = """date,model,pool,cost,requests
2026-09-28 10:00:00,grok-4,Other Models,8.00,20
2026-09-28 11:00:00,grok-4,Other Models,8.00,20
2026-09-28 12:00:00,gemini-4,Other Models,8.00,20
2026-09-28 13:00:00,claude-opus-5.5,Cursor Models,2.00,5
2026-09-28 14:00:00,claude-opus-5.5,Cursor Models,1.00,3
"""

CSV_POOL_SWITCH = """date,model,pool,cost,requests
2026-09-28 10:00:00,grok-4,Other Models,4.00,10
2026-09-28 11:00:00,grok-4,Other Models,4.00,10
2026-09-28 12:00:00,grok-4,API Key,4.00,10
"""

CSV_UNATTENDED = """date,model,pool,cost,requests
2026-09-26 03:12:00,gemini-4,API Key,12.00,30
2026-09-28 10:00:00,claude-opus-5.5,Cursor Models,3.50,10
"""

CSV_MIDDAY = """date,model,pool,cost,requests
2026-09-28 10:00:00,claude-opus-5.5,Cursor Models,9.00,20
2026-09-28 11:00:00,grok-4,Other Models,1.00,3
2026-09-29 10:00:00,claude-opus-5.5,Cursor Models,1.00,3
2026-09-29 11:00:00,grok-4,Other Models,9.00,25
"""

CSV_HEADROOM = """date,model,pool,cost,requests
2026-10-01 10:00:00,grok-4,Other Models,100.00,50
2026-10-02 10:00:00,grok-4,Other Models,20.00,10
2026-10-01 11:00:00,claude-opus-5.5,Cursor Models,30.00,15
"""

CSV_MULTI_ACCT = """date,model,pool,account,cost,requests
2026-10-01 10:00:00,grok-4,Other Models,acct-a,60.00,20
2026-10-01 11:00:00,grok-4,Other Models,acct-b,10.00,5
"""

LIMITS_ONE = {
    "accounts": [
        {"name": "solo", "plan": "cursor:pro",
         "limits": {"cursor-models": 500.0, "other-models": 125.0,
                    "api-key": None}},
    ]
}

LIMITS_MULTI = {
    "accounts": [
        {"name": "acct-a", "plan": "cursor:pro",
         "limits": {"other-models": 50.0},
         "match": {"column": "account", "value": "acct-a"}},
        {"name": "acct-b", "plan": "cursor:pro",
         "limits": {"other-models": 50.0},
         "match": {"column": "account", "value": "acct-b"}},
    ]
}


def make_claude_log(projects_dir, session_id, model, ts_iso,
                    in_tok=1000, out_tok=500):
    proj = os.path.join(projects_dir, "projA")
    os.makedirs(proj, exist_ok=True)
    line = json.dumps({
        "timestamp": ts_iso,
        "message": {"model": model,
                    "usage": {"input_tokens": in_tok,
                              "output_tokens": out_tok}},
    })
    with open(os.path.join(proj, session_id + ".jsonl"), "w") as fh:
        fh.write(line + "\n")


def main():
    sys.path.insert(0, SRC)
    from blackbox_bill import (cursor_csv, explain, headroom, join as joinmod,
                               logs, whatif)

    # --- CSV parsing ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "ragged.csv")
        write(p, CSV_RAGGED)
        rows, warnings = cursor_csv.parse_csv(p)
        check("ragged CSV parses 3 rows", len(rows) == 3, "rows=%d" % len(rows))
        check("fuzzy headers mapped (Model Name->model, Amount->cost)",
              rows[0]["model"] == "grok-4" and rows[0]["cost"] == 1.20
              and rows[0]["pool"] == "Other Models"
              and rows[0]["input_tokens"] == 50000,
              repr({k: rows[0][k] for k in ("model", "cost", "pool")}))
        check("short row missing fields -> None, not crash",
              rows[1]["cost"] == 1.20 and rows[1]["requests"] is None,
              repr(rows[1]))
        check("long row extra cells ignored",
              rows[2]["model"] == "claude-opus-5.5" and rows[2]["cost"] == 3.50)
        check("BOM + blank line tolerated, unrecognized col warned",
              any("Extra Junk" in w or "unrecognized" in w for w in warnings),
              "; ".join(warnings))

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "nocost.csv")
        write(p, CSV_NO_COST)
        rows, warnings = cursor_csv.parse_csv(p)
        check("missing cost column -> fail soft (None + warning)",
              rows[0]["cost"] is None
              and any("cost" in w and "missing" in w for w in warnings),
              "; ".join(warnings))
        check("missing pool/quota variants still mapped",
              rows[0]["pool"] == "Other Models", rows[0]["pool"])

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "empty.csv")
        write(p, "")
        rows, warnings = cursor_csv.parse_csv(p)
        check("empty CSV -> warning, no crash",
              rows == [] and any("empty" in w for w in warnings))
        rows, warnings = cursor_csv.parse_csv(os.path.join(tmp, "nope.csv"))
        check("missing file -> warning, no crash",
              rows == [] and any("cannot open" in w for w in warnings))

    # --- pool normalization math ---
    check("normalize_pool mappings",
          cursor_csv.normalize_pool("Other Models") == "other-models"
          and cursor_csv.normalize_pool("Cursor Models") == "cursor-models"
          and cursor_csv.normalize_pool("API Key") == "api-key"
          and cursor_csv.normalize_pool("") == "unknown"
          and cursor_csv.normalize_pool("Ultra Pool") == "cursor-models")

    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drain.csv")
        write(p, CSV_DRAIN)
        rows, _ = cursor_csv.parse_csv(p)
        summary = explain.pool_summary(rows)
        check("pool summary math",
              abs(summary["other-models"]["cost"] - 24.0) < 1e-9
              and abs(summary["cursor-models"]["cost"] - 3.0) < 1e-9
              and summary["other-models"]["requests"] == 60,
              repr({k: v["cost"] for k, v in summary.items()}))
        daily = explain.daily_summary(rows)
        check("daily summary math",
              abs(daily["2026-09-28"]["cost"] - 27.0) < 1e-9
              and daily["2026-09-28"]["requests"] == 68,
              repr(daily))

    # --- rule: pool-drain fires / does not fire ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drain.csv")
        write(p, CSV_DRAIN)
        rows, _ = cursor_csv.parse_csv(p)
        e = explain.rule_pool_drain(rows)
        check("pool-drain fires on Other-heavy bill",
              e is not None and "Other Models" in e.title
              and "24.00" in e.citation(),
              e.citation() if e else "no explanation")
        p2 = os.path.join(tmp, "clean.csv")
        write(p2, CSV_CLEAN)
        rows2, _ = cursor_csv.parse_csv(p2)
        check("pool-drain silent on clean bill",
              explain.rule_pool_drain(rows2) is None)

    # --- rule: pool-switch fires / does not fire ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "switch.csv")
        write(p, CSV_POOL_SWITCH)
        rows, _ = cursor_csv.parse_csv(p)
        e = explain.rule_pool_switch(rows)
        check("pool-switch fires when one model spans pools",
              e is not None and "grok-4" in e.title,
              e.title if e else "no explanation")
        p2 = os.path.join(tmp, "clean.csv")
        write(p2, CSV_CLEAN)
        rows2, _ = cursor_csv.parse_csv(p2)
        check("pool-switch silent on clean bill",
              explain.rule_pool_switch(rows2) is None)

    # --- rule: unattended-burn fires / does not fire ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "unatt.csv")
        write(p, CSV_UNATTENDED)
        rows, _ = cursor_csv.parse_csv(p)
        sessions, _ = logs.read_all_logs(
            os.path.join(tmp, "no-claude"), os.path.join(tmp, "no-codex"))
        matched, orphans, _ = joinmod.join_rows(rows, sessions)
        e = explain.rule_unattended(orphans)
        # both rows are orphans (no local sessions): 12.00 + 3.50 = 15.50
        check("unattended-burn fires with no local sessions",
              e is not None and "$15.50" in e.title,
              e.title if e else "no explanation")
        # tiny orphan spend stays silent
        e2 = explain.rule_unattended(
            [{"cost": 0.10, "requests": 1, "model": "x", "pool": ""}])
        check("unattended-burn silent under threshold", e2 is None)

    # --- rule: midday-switch fires / does not fire ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "mid.csv")
        write(p, CSV_MIDDAY)
        rows, _ = cursor_csv.parse_csv(p)
        e = explain.rule_midday_switch(rows)
        check("midday-switch fires on pool share jump",
              e is not None and "2026-09-29" in e.title,
              e.title if e else "no explanation")
        p2 = os.path.join(tmp, "clean.csv")
        write(p2, CSV_CLEAN)
        rows2, _ = cursor_csv.parse_csv(p2)
        check("midday-switch silent on clean bill",
              explain.rule_midday_switch(rows2) is None)

    # --- rule: reroute fires / does not fire ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "reroute.csv")
        write(p, "date,model,pool,cost,requests\n"
                 "2026-09-28 10:30:00,fable-5.5,Cursor Models,4.00,12\n")
        rows, _ = cursor_csv.parse_csv(p)
        cdir = os.path.join(tmp, "claude")
        make_claude_log(cdir, "sess1", "fable-5.1",
                        "2026-09-28T10:00:00")
        sessions, _ = logs.read_claude_logs(cdir)
        matched, orphans, _ = joinmod.join_rows(rows, sessions)
        check("join matches on time window, flags model disagreement",
              len(matched) == 1 and len(orphans) == 0
              and matched[0][2] is False,
              "matched=%d orphans=%d agrees=%s"
              % (len(matched), len(orphans),
                 matched[0][2] if matched else None))
        e = explain.rule_reroute(matched)
        check("reroute fires on billed!=used model",
              e is not None and "fable-5.1" in e.title
              and "fable-5.5" in e.title and "sess1" in e.detail,
              e.title if e else "no explanation")
        # clean: billed model == used model
        p2 = os.path.join(tmp, "reroute2.csv")
        write(p2, "date,model,pool,cost,requests\n"
                  "2026-09-28 10:30:00,fable-5.5,Cursor Models,4.00,12\n")
        rows2, _ = cursor_csv.parse_csv(p2)
        make_claude_log(cdir, "sess2", "fable-5.5", "2026-09-28T10:00:00")
        sessions2, _ = logs.read_claude_logs(cdir)
        matched2, _, _ = joinmod.join_rows(rows2, sessions2)
        check("reroute silent when billed==used",
              explain.rule_reroute(matched2) is None)
        # cross-family mismatch is NOT a reroute (gemini billed, claude used)
        p3 = os.path.join(tmp, "reroute3.csv")
        write(p3, "date,model,pool,cost,requests\n"
                  "2026-09-28 10:30:00,gemini-4,API Key,4.00,12\n")
        rows3, _ = cursor_csv.parse_csv(p3)
        cdir3 = os.path.join(tmp, "claude3")
        make_claude_log(cdir3, "s3", "claude-opus-5.5", "2026-09-28T10:00:00")
        sessions3, _ = logs.read_claude_logs(cdir3)
        matched3, _, _ = joinmod.join_rows(rows3, sessions3)
        check("reroute silent on cross-family mismatch",
              len(matched3) == 1 and explain.rule_reroute(matched3) is None)
        check("model_family groups versions, splits providers",
              explain.model_family("fable-5.1") == explain.model_family("fable-5.5")
              and explain.model_family("gemini-4") != explain.model_family("claude-opus-5.5"))

    # --- join: orphan outside window, no-timestamp rows ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "j.csv")
        write(p, "date,model,pool,cost,requests\n"
                 "2026-09-20 10:00:00,claude-opus-5.5,Cursor Models,4.00,12\n"
                 ",claude-opus-5.5,Cursor Models,1.00,2\n")
        rows, _ = cursor_csv.parse_csv(p)
        cdir = os.path.join(tmp, "claude")
        make_claude_log(cdir, "s1", "claude-opus-5.5", "2026-09-28T10:00:00")
        sessions, _ = logs.read_claude_logs(cdir)
        matched, orphans, no_ts = joinmod.join_rows(rows, sessions,
                                                    window_hours=12)
        check("row 8 days away is orphan, dateless row unjoinable",
              len(matched) == 0 and len(orphans) == 1 and len(no_ts) == 1,
              "m=%d o=%d nts=%d" % (len(matched), len(orphans), len(no_ts)))

    # --- logs: corrupt jsonl fails soft ---
    with tempfile.TemporaryDirectory() as tmp:
        cdir = os.path.join(tmp, "claude")
        proj = os.path.join(cdir, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, "bad.jsonl"), "w") as fh:
            fh.write("not json at all\n{\"timestamp\": \"2026-09-28T10:00:00\", "
                     "\"message\": {\"model\": \"m\", \"usage\": "
                     "{\"input_tokens\": 5, \"output_tokens\": 6}}}\n")
        sessions, warnings = logs.read_claude_logs(cdir)
        check("corrupt jsonl line skipped, good line kept",
              len(sessions) == 1 and sessions[0]["input_tokens"] == 5
              and sessions[0]["output_tokens"] == 6)
        s, w = logs.read_claude_logs(os.path.join(tmp, "missing"))
        check("missing claude dir -> warning, no crash",
              s == [] and any("not found" in x for x in w))
        s, w = logs.read_codex_logs(os.path.join(tmp, "missing2"))
        check("missing codex dir -> warning, no crash",
              s == [] and any("not found" in x for x in w))

    # --- what-if delta math ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "w.csv")
        write(p, "date,model,pool,input_tokens,output_tokens,cost\n"
                 "2026-09-28 10:00:00,grok-4,Other Models,1000000,1000000,25.00\n")
        rows, _ = cursor_csv.parse_csv(p)
        res = whatif.reprice(rows, "cursor:pro")
        # 1M in * 5.0 + 1M out * 20.0 = 25.00 gross, 20 included -> 5.00 net
        check("what-if reprice math",
              abs(res["gross_estimated"] - 25.0) < 1e-9
              and abs(res["net_estimated"] - 5.0) < 1e-9
              and abs(res["actual_total"] - 25.0) < 1e-9
              and abs(res["delta"] - (-20.0)) < 1e-9,
              repr({k: res[k] for k in ("gross_estimated", "net_estimated",
                                        "delta")}))
        txt = whatif.whatif_text(res)
        check("what-if text prints delta", "-$20.00" in txt and "less" in txt,
              txt.splitlines()[-3:])
        try:
            whatif.reprice(rows, "cursor:nonexistent")
            check("unknown plan raises", False)
        except KeyError:
            check("unknown plan raises", True)
        # custom rates file
        rf = os.path.join(tmp, "rates.json")
        write(rf, json.dumps({
            "label": "custom", "monthly_included_usd": 0,
            "rates": {"other-models": {"input": 1.0, "output": 2.0},
                      "unknown": {"input": 1.0, "output": 2.0}}}))
        res2 = whatif.reprice(rows, "cursor:pro", rates_file=rf)
        check("custom rates file honored",
              abs(res2["gross_estimated"] - 3.0) < 1e-9,  # 1*1 + 1*2
              "gross=%.2f" % res2["gross_estimated"])
        # no cost column -> delta None
        p2 = os.path.join(tmp, "wn.csv")
        write(p2, CSV_NO_COST)
        rows2, _ = cursor_csv.parse_csv(p2)
        res3 = whatif.reprice(rows2, "cursor:pro")
        check("no cost column -> delta unavailable, not crash",
              res3["delta"] is None and res3["net_estimated"] >= 0)

    # --- CLI end to end ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drain.csv")
        write(p, CSV_DRAIN)
        md = os.path.join(tmp, "report.md")
        code, out, err = run_cli(["--csv", p, "--no-logs", "--markdown", md,
                                  "--whatif", "cursor:pro"], cwd=tmp)
        check("CLI end-to-end exit 0 on drain fixture", code == 0,
              "rc=%d err=%s" % (code, err[:200]))
        check("CLI prints pool summary + explanation",
              "other-models" in out and "drained first" in out,
              out[:200])
        check("CLI prints what-if section", "What-if" in out and "delta" in out)
        check("markdown report written",
              os.path.exists(md) and "drained first" in open(md).read())
        code, out, err = run_cli(["--csv", os.path.join(tmp, "nope.csv")],
                                 cwd=tmp)
        check("CLI exit 2 on missing CSV", code == 2, "rc=%d" % code)
        code, out, err = run_cli(["--csv", p, "--no-logs",
                                  "--whatif", "nope:plan"], cwd=tmp)
        check("CLI exit 2 on unknown plan", code == 2, "rc=%d" % code)

    # --- CLI with local logs joined ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "unatt.csv")
        write(p, CSV_UNATTENDED)
        cdir = os.path.join(tmp, "claude")
        make_claude_log(cdir, "day", "claude-opus-5.5", "2026-09-28T09:30:00")
        code, out, err = run_cli(["--csv", p, "--claude-dir", cdir,
                                  "--codex-dir", os.path.join(tmp, "nocodex")],
                                 cwd=tmp)
        check("CLI joins local logs, flags orphan spend",
              code == 0 and "no matching local session" in out
              and "Local-log join" in out,
              "rc=%d" % code)

    # --- v0.3 headroom dashboard ---
    with tempfile.TemporaryDirectory() as tmp:
        lp = os.path.join(tmp, "limits.json")
        write(lp, json.dumps(LIMITS_ONE))
        accounts = headroom.load_limits(lp)
        check("limits file loads one account", len(accounts) == 1
              and accounts[0]["name"] == "solo")
        check("non-canonical pool labels normalized to canonical",
              "cursor-models" in accounts[0]["limits"]
              and "other-models" in accounts[0]["limits"])

        # malformed inputs rejected, not crash
        bad = os.path.join(tmp, "bad.json")
        write(bad, "{not json")
        try:
            headroom.load_limits(bad)
            check("malformed limits JSON raises ValueError", False)
        except ValueError:
            check("malformed limits JSON raises ValueError", True)
        write(bad, json.dumps({"accounts": []}))
        try:
            headroom.load_limits(bad)
            check("empty accounts list raises ValueError", False)
        except ValueError:
            check("empty accounts list raises ValueError", True)
        try:
            headroom.load_limits(os.path.join(tmp, "missing.json"))
            check("missing limits file raises ValueError", False)
        except ValueError:
            check("missing limits file raises ValueError", True)

        # headroom without a CSV: limits only, no usage
        rows_by_acct, unclaimed = headroom.partition_rows([], accounts)
        check("no rows -> no unclaimed", unclaimed == []
              and rows_by_acct == {"solo": []})
        res = headroom.compute_headroom(rows_by_acct, accounts)
        solo = res["accounts"][0]["pools"]
        check("no usage -> OK, full headroom",
              solo["other-models"]["status"] == "OK"
              and solo["other-models"]["remaining"] == 125.0)
        check("null limit -> UNLIMITED",
              solo["api-key"]["status"] == "UNLIMITED"
              and solo["api-key"]["remaining"] is None)
        txt = headroom.headroom_text(res, had_usage=False)
        check("limits-only dashboard says no CSV was given",
              "no --csv given" in txt and "unlimited" in txt)

        # with usage: LOW at <25% remaining, EXHAUSTED over the cap
        p = os.path.join(tmp, "usage.csv")
        write(p, CSV_HEADROOM)
        rows, _ = cursor_csv.parse_csv(p)
        rows_by_acct, unclaimed = headroom.partition_rows(rows, accounts)
        res = headroom.compute_headroom(rows_by_acct, accounts)
        om = res["accounts"][0]["pools"]["other-models"]
        check("120/125 consumed -> LOW, remaining 5.00",
              om["status"] == "LOW" and abs(om["remaining"] - 5.0) < 1e-9
              and om["consumed"] == 120.0,
              repr(om))
        check("cursor-models 30/500 -> OK",
              res["accounts"][0]["pools"]["cursor-models"]["status"] == "OK")

        # over-limit -> EXHAUSTED
        lp2 = os.path.join(tmp, "limits2.json")
        write(lp2, json.dumps({"accounts": [
            {"name": "tight", "plan": "cursor:pro",
             "limits": {"other-models": 100.0}}]}))
        accounts2 = headroom.load_limits(lp2)
        rows_by_acct2, _ = headroom.partition_rows(rows, accounts2)
        om2 = headroom.compute_headroom(rows_by_acct2, accounts2
                                        )["accounts"][0]["pools"]["other-models"]
        check("120/100 consumed -> EXHAUSTED, negative remaining",
              om2["status"] == "EXHAUSTED" and om2["remaining"] < 0)

        # multi-account partition by column
        lpm = os.path.join(tmp, "limits-multi.json")
        write(lpm, json.dumps(LIMITS_MULTI))
        maccounts = headroom.load_limits(lpm)
        pm = os.path.join(tmp, "multi.csv")
        write(pm, CSV_MULTI_ACCT)
        mrows, _ = cursor_csv.parse_csv(pm)
        mby, unclaimed = headroom.partition_rows(mrows, maccounts)
        check("rows partitioned by account column, none unclaimed",
              len(mby["acct-a"]) == 1 and len(mby["acct-b"]) == 1
              and not unclaimed)
        mres = headroom.compute_headroom(mby, maccounts)
        ma = {a["name"]: a for a in mres["accounts"]}
        check("acct-a 60/50 -> EXHAUSTED",
              ma["acct-a"]["pools"]["other-models"]["status"] == "EXHAUSTED")
        check("acct-b 10/50 -> OK",
              ma["acct-b"]["pools"]["other-models"]["status"] == "OK")

        # overlapping match specs rejected
        lpo = os.path.join(tmp, "limits-overlap.json")
        write(lpo, json.dumps({"accounts": [
            {"name": "x", "limits": {"other-models": 10.0},
             "match": {"column": "account", "value": "acct-a"}},
            {"name": "y", "limits": {"other-models": 10.0},
             "match": {"column": "account", "value": "acct-a"}},
        ]}))
        oaccounts = headroom.load_limits(lpo)
        try:
            headroom.partition_rows(mrows, oaccounts)
            check("overlapping match specs raise ValueError", False)
        except ValueError:
            check("overlapping match specs raise ValueError", True)

        # dispatch simulator: which account can absorb the task?
        # cursor:pro other-models = $5/M in + $20/M out:
        # 1M in + 250K out = $10. acct-a remaining -10 -> no; acct-b 40 -> yes.
        sim = headroom.simulate_dispatch(
            mres, maccounts, 1_000_000, 250_000, pool="other-models")
        by_name = {e["account"]: e for e in sim["estimates"]}
        check("task priced at $10 under cursor:pro other-models",
              by_name["acct-b"]["cost"] is not None
              and abs(by_name["acct-b"]["cost"] - 10.0) < 1e-9,
              repr(by_name["acct-b"]["cost"]))
        check("acct-b fits the task", by_name["acct-b"]["fits"] is True)
        check("acct-a does not fit the task", by_name["acct-a"]["fits"] is False)
        check("recommendation is acct-b",
              sim["recommendation"] == "acct-b")
        dtxt = headroom.dispatch_text(sim)
        check("dispatch text names the recommendation",
              "RECOMMEND" in dtxt and "acct-b" in dtxt)

        # impossible task -> no recommendation, honest warning
        sim2 = headroom.simulate_dispatch(
            mres, maccounts, 100_000_000, 100_000_000, pool="other-models")
        check("no account fits -> recommendation None + WARNING",
              sim2["recommendation"] is None
              and "WARNING" in headroom.dispatch_text(sim2))

    # --- headroom CLI end to end ---
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "usage.csv")
        write(p, CSV_HEADROOM)
        lp = os.path.join(tmp, "limits.json")
        write(lp, json.dumps(LIMITS_ONE))
        code, out, err = run_cli(
            ["headroom", "--csv", p, "--limits-file", lp,
             "--task-in", "1000000", "--task-out", "250000"], cwd=tmp)
        check("headroom CLI exit 0 with csv + limits + task",
              code == 0, "rc=%d err=%s" % (code, err[:200]))
        check("headroom CLI prints pre-flight dashboard",
              "pre-flight" in out and "solo" in out and "LOW" in out,
              out[:200])
        check("headroom CLI prints dispatch recommendation",
              "RECOMMEND" in out or "Dispatch simulation" in out)
        md = os.path.join(tmp, "headroom.md")
        code, out, err = run_cli(
            ["headroom", "--limits-file", lp, "--markdown", md], cwd=tmp)
        md_ok = (code == 0 and os.path.exists(md)
                 and "pre-flight" in open(md).read().lower())
        check("headroom CLI works limits-only and writes markdown",
              md_ok, "rc=%d" % code)
        code, out, err = run_cli(["headroom", "--csv", p], cwd=tmp)
        check("headroom CLI exit 2 without --limits-file", code == 2,
              "rc=%d" % code)
        bad = os.path.join(tmp, "bad.json")
        write(bad, "nope")
        code, out, err = run_cli(
            ["headroom", "--limits-file", bad], cwd=tmp)
        check("headroom CLI exit 2 on malformed limits file", code == 2,
              "rc=%d err=%s" % (code, err[:120]))

    print("\n%d/%d passed" % (sum(PASS), len(PASS)))
    return 0 if all(PASS) else 1


if __name__ == "__main__":
    sys.exit(main())
