"""v2 tests: tokenizer-aware figures, per-project and subagent attribution.

Run:  python3 -m unittest discover -s tests -t .
(requires src/ on sys.path; run from the repo root with PYTHONPATH=src)
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from blackbox_bill import tokenizer
from blackbox_bill.attribution import (agent_report_text, by_agent, by_project,
                                       estimate_cost, load_rate_card,
                                       project_report_text, rate_for)
from blackbox_bill import logs


def jline(**kw):
    return json.dumps(kw)


def write_jsonl(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def assistant_line(ts, model, in_tok, out_tok, sidechain=False,
                   subagent_type=None):
    content = [{"type": "text", "text": "ok"}]
    if subagent_type is not None:
        content.append({"type": "tool_use", "id": "t1", "name": "Task",
                        "input": {"subagent_type": subagent_type,
                                  "prompt": "do it"}})
    obj = {"timestamp": ts,
           "message": {"role": "assistant", "model": model,
                       "content": content,
                       "usage": {"input_tokens": in_tok,
                                 "output_tokens": out_tok}}}
    if sidechain:
        obj["isSidechain"] = True
    return jline(**obj)


def make_fixture_projects(base):
    """Two projects: alpha (1 session, 1 subagent run) and beta (clean)."""
    write_jsonl(os.path.join(base, "proj-alpha", "s1.jsonl"), [
        assistant_line("2026-10-01T10:00:01", "claude-sonnet-4-5",
                       100000, 5000, subagent_type="Explore"),
        assistant_line("2026-10-01T10:01:00", "claude-sonnet-4-5",
                       80000, 20000, sidechain=True),
        assistant_line("2026-10-01T10:02:00", "claude-sonnet-4-5",
                       50000, 10000),
    ])
    write_jsonl(os.path.join(base, "proj-beta", "s2.jsonl"), [
        assistant_line("2026-10-02T09:00:00", "claude-opus-4-6",
                       200000, 40000),
    ])
    return base


class TokenizerTest(unittest.TestCase):
    def test_resolve_legacy_defaults_to_1_3(self):
        name, label, f = tokenizer.resolve("legacy")
        self.assertEqual(name, "legacy")
        self.assertAlmostEqual(f, 1.3)

    def test_resolve_47_keeps_ratio(self):
        name, label, f = tokenizer.resolve("claude-4.7")
        self.assertAlmostEqual(f, 1.3)

    def test_factor_override(self):
        _, _, f = tokenizer.resolve("legacy", factor=1.5)
        self.assertAlmostEqual(f, 1.5)

    def test_unknown_tokenizer_raises(self):
        with self.assertRaises(ValueError):
            tokenizer.resolve("gpt-tokenizer")

    def test_bad_factor_raises(self):
        with self.assertRaises(ValueError):
            tokenizer.resolve("legacy", factor=0)

    def test_both_legacy_basis(self):
        legacy, c47, _, f = tokenizer.both(1000, "legacy")
        self.assertAlmostEqual(legacy, 1000)
        self.assertAlmostEqual(c47, 1300)

    def test_both_47_basis_converts_back(self):
        legacy, c47, _, f = tokenizer.both(1300, "claude-4.7")
        self.assertAlmostEqual(legacy, 1000)
        self.assertAlmostEqual(c47, 1300)

    def test_both_factor_override(self):
        legacy, c47, _, f = tokenizer.both(1000, "legacy", factor=2.0)
        self.assertAlmostEqual(c47, 2000)

    def test_dual_line_shows_both_figures(self):
        s = tokenizer.dual_line(1_000_000, "legacy")
        self.assertIn("1.00M (legacy)", s)
        self.assertIn("1.30M (claude-4.7, x1.3)", s)

    def test_fmt_tokens(self):
        self.assertEqual(tokenizer.fmt_tokens(1_500_000), "1.50M")
        self.assertEqual(tokenizer.fmt_tokens(2500), "2.5K")
        self.assertEqual(tokenizer.fmt_tokens(42), "42")


class LogsSubagentTest(unittest.TestCase):
    def test_project_and_subagent_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_fixture_projects(tmp)
            sessions, warnings = logs.read_claude_logs(tmp)
            by_id = {s["session_id"]: s for s in sessions}
            s1 = by_id["s1"]
            self.assertEqual(s1["project"], "proj-alpha")
            # sidechain tokens attributed to subagents, still in totals
            self.assertEqual(s1["subagent_input_tokens"], 80000)
            self.assertEqual(s1["subagent_output_tokens"], 20000)
            self.assertEqual(s1["input_tokens"], 230000)
            self.assertEqual(s1["subagent_runs"], 1)
            self.assertEqual(s1["subagent_types"]["Explore"], 1)
            s2 = by_id["s2"]
            self.assertEqual(s2["project"], "proj-beta")
            self.assertEqual(s2["subagent_runs"], 0)
            self.assertEqual(s2["subagent_input_tokens"], 0)

    def test_unknown_subagent_type_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(os.path.join(tmp, "p", "x.jsonl"), [
                assistant_line("2026-10-01T10:00:01", "m", 10, 5,
                               subagent_type=None),
            ])
            sessions, _ = logs.read_claude_logs(tmp)
            # Task tool_use without subagent_type -> not counted (no Task
            # block emitted when subagent_type is None)
            self.assertEqual(sessions[0]["subagent_runs"], 0)


class AttributionTest(unittest.TestCase):
    def _sessions(self, tmp):
        make_fixture_projects(tmp)
        sessions, _ = logs.read_claude_logs(tmp)
        return sessions

    def test_by_project_sorted_by_cost_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = by_project(self._sessions(tmp))
            self.assertEqual([p["project"] for p in projects],
                             ["proj-beta", "proj-alpha"])
            beta = projects[0]
            self.assertEqual(beta["tokens"], 240000)
            self.assertEqual(len(beta["sessions"]), 1)
            alpha = projects[1]
            self.assertEqual(alpha["subagent_runs"], 1)
            self.assertEqual(alpha["subagent_tokens"], 100000)

    def test_cost_estimate_opus_vs_sonnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = by_project(self._sessions(tmp))
            costs = {p["project"]: p["est_cost"] for p in projects}
            # opus session: 200k in * 15 + 40k out * 75 = 3 + 3 = 6
            self.assertAlmostEqual(costs["proj-beta"], 6.0)
            # sonnet session: 230k in * 3 + 35k out * 15 = 0.69+0.525
            self.assertAlmostEqual(costs["proj-alpha"], 1.215)

    def test_rates_file_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            rf = os.path.join(tmp, "rates.json")
            with open(rf, "w") as fh:
                json.dump({"label": "mine",
                           "default": {"input": 1.0, "output": 1.0},
                           "patterns": []}, fh)
            card = load_rate_card(rf)
            self.assertEqual(card["label"], "mine")
            self.assertAlmostEqual(
                estimate_cost(1_000_000, 1_000_000, "claude-opus-4-6", card),
                2.0)
            # built-in card untouched
            self.assertAlmostEqual(
                estimate_cost(1_000_000, 0, "claude-opus-4-6"), 15.0)

    def test_rate_for_pattern_match(self):
        rin, rout = rate_for("claude-haiku-4-5")
        self.assertEqual((rin, rout), (1.0, 5.0))
        rin, rout = rate_for("some-unknown-model-9")
        self.assertEqual((rin, rout), (3.0, 15.0))

    def test_by_agent_share_and_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = by_agent(self._sessions(tmp))
            # total = 230k+35k + 240k = 505k; subagent = 100k
            self.assertEqual(result["total_tokens"], 505000)
            self.assertEqual(result["subagent_tokens"], 100000)
            self.assertAlmostEqual(result["share"], 100000 / 505000)
            self.assertEqual(result["runs"], 1)
            self.assertEqual(len(result["agents"]), 1)
            top = result["agents"][0]
            self.assertEqual(top["agent_type"], "Explore")
            self.assertAlmostEqual(top["tokens"], 100000)
            self.assertAlmostEqual(top["share_of_subagent"], 1.0)

    def test_by_agent_no_subagents(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_jsonl(os.path.join(tmp, "p", "x.jsonl"), [
                assistant_line("2026-10-01T10:00:01", "m", 10, 5),
            ])
            sessions, _ = logs.read_claude_logs(tmp)
            result = by_agent(sessions)
            self.assertEqual(result["share"], 0.0)
            self.assertEqual(result["agents"], [])
            text = agent_report_text(result, "legacy", 1.3, None)
            self.assertIn("No subagent activity detected", text)

    def test_by_project_empty(self):
        text = project_report_text([], "legacy", 1.3,
                                   {"label": "built-in"})
        self.assertIn("No sessions found", text)

    def test_reports_show_dual_tokenizer_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = self._sessions(tmp)
            card = load_rate_card()
            pt = project_report_text(by_project(sessions), "legacy", 1.3,
                                     card)
            self.assertIn("claude-4.7, x1.3", pt)
            self.assertIn("(legacy)", pt)
            at = agent_report_text(by_agent(sessions), "legacy", 1.3, card)
            self.assertIn("claude-4.7, x1.3", at)


class ReportCliTest(unittest.TestCase):
    def _run(self, args, cwd="/tmp"):
        env = dict(os.environ, PYTHONPATH=SRC)
        p = subprocess.run([sys.executable, "-m", "blackbox_bill"] + args,
                           capture_output=True, text=True, cwd=cwd, env=env,
                           timeout=30)
        return p.returncode, p.stdout, p.stderr

    def _fixture_csv(self, tmp):
        p = os.path.join(tmp, "b.csv")
        with open(p, "w") as fh:
            fh.write("date,model,pool,input_tokens,output_tokens,cost_usd,requests\n"
                     "2026-09-28 10:00:00,claude-opus-5.5,Cursor Models,"
                     "1000000,200000,3.50,10\n")
        return p

    def test_report_requires_mode_flag(self):
        code, out, err = self._run(
            ["report", "--claude-dir", "/tmp/definitely-not-here"])
        self.assertEqual(code, 2)
        self.assertIn("--by-project", err)

    def test_report_by_project_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_fixture_projects(tmp)
            code, out, err = self._run(
                ["report", "--by-project", "--claude-dir", tmp])
            self.assertEqual(code, 0, err[:300])
            self.assertIn("proj-alpha", out)
            self.assertIn("proj-beta", out)
            self.assertIn("x1.3", out)
            self.assertIn("est. $6.00", out)

    def test_report_by_agent_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_fixture_projects(tmp)
            code, out, err = self._run(
                ["report", "--by-agent", "--claude-dir", tmp, "--top", "3"])
            self.assertEqual(code, 0, err[:300])
            self.assertIn("Subagent share", out)
            self.assertIn("Explore", out)
            self.assertIn("19.8%", out)

    def test_report_markdown_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_fixture_projects(tmp)
            md = os.path.join(tmp, "r.md")
            code, out, err = self._run(
                ["report", "--by-agent", "--claude-dir", tmp,
                 "--markdown", md])
            self.assertEqual(code, 0, err[:300])
            self.assertTrue(os.path.exists(md))
            self.assertIn("Subagent", open(md).read())

    def test_report_bad_tokenizer(self):
        code, out, err = self._run(
            ["report", "--by-agent", "--tokenizer", "nope"])
        self.assertEqual(code, 2)

    def test_bill_mode_backward_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._fixture_csv(tmp)
            code, out, err = self._run(
                ["--csv", p, "--no-logs"], cwd=tmp)
            self.assertEqual(code, 0, err[:300])
            self.assertIn("cursor-models", out)

    def test_bill_mode_tokenizer_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._fixture_csv(tmp)
            code, out, err = self._run(
                ["--csv", p, "--no-logs", "--tokenizer", "claude-4.7"],
                cwd=tmp)
            self.assertEqual(code, 0, err[:300])
            self.assertIn("Tokenizer-aware token figures", out)
            # 1.2M measured under 4.7 -> ~923.1K legacy
            self.assertIn("923.1K (legacy)", out)
            self.assertIn("1.20M (claude-4.7, x1.3)", out)

    def test_bill_mode_factor_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._fixture_csv(tmp)
            code, out, err = self._run(
                ["--csv", p, "--no-logs", "--tokenizer-factor", "2.0"],
                cwd=tmp)
            self.assertEqual(code, 0, err[:300])
            self.assertIn("2.40M (claude-4.7, x2)", out)


if __name__ == "__main__":
    unittest.main()
