# blackbox-bill

**Explain where your AI-coding-tool money went.** A stdlib-only Python CLI that reads your Cursor billing export and your local session logs, then tells you — in plain English — why your bill looks the way it does.

No network. No API keys. Deterministic: every explanation cites the exact rows it was computed from.

## The problem

Cursor's billing pools confuse everyone:

- u/josh3com on r/ClaudeCode: *"CURSOR TEAM PLEASE HELP - Cursor support keeps telling me my 100% full usage pool is from grok bot two months in a row"* — Grok Bot spend kept landing in "Other Models" with no explanation in the app.
- u/FerretValuable9718: *"Why is the 'Other Models' usage being used so much with so little time?"* — screenshot showed **Other Models 100% used while Cursor Models sat at 16%**.
- r/AI_Agents: *"My coding agent hit a cold-start 503, found a Gemini key in my repo, and burned $40 while I slept"* — spend routed to a provider the user never intended.
- dev.to: *"$402 on one ticket"* in headless Claude Code — *"nothing in the app said so"*, so the author built manual bookkeeping.
- r/ClaudeCode, October 2026: Fable 5.1 requests silently served by 5.5 — the model on the invoice doesn't match the model you asked for.

Forums are full of people doing billing archaeology by hand. blackbox-bill automates the archaeology.

## What it does

```
blackbox-bill --csv cursor-export.csv
```

Given a Cursor billing export CSV (columns vary — the parser is deliberately liberal), it prints:

1. **Pool-normalized summary** — first-party Cursor pool vs "Other Models" pool vs API-key spend, per model, per day. Free-form pool labels are normalized to the three canonical pools; unrecognized labels are reported as `unknown`, never silently dropped.
2. **Plain-English explanations** from rule-based anomaly detection:
   - *"Why your 'Other Models' pool drained first"* — auto-model routing sent X% of requests to non-Cursor models.
   - *"The grok-4 spend landed in unexpected pool(s)"* — one model billed across multiple pools.
   - *"'fable-5.1' requests were routed to 'fable-5.5'"* — silent version swaps, with the N local sessions listed.
   - *"Unattended burn: $X spent while no interactive session was open"* — the "$40 while I slept" case.
   - *"Sudden pool switch on <date>"* — same usage pattern, different bucket mid-cycle.
3. **Local-log cross-check** — joins bill rows against your `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/` by time window, reporting matched vs orphaned spend.
4. **Plan-switch what-if simulator**:

```
blackbox-bill --csv cursor-export.csv --whatif cursor:ultra
```

re-prices observed token usage under a different plan's rates and prints the delta vs the actual bill. Bundled rate cards are **illustrative placeholders, not real Cursor pricing** — pass `--rates-file rates.json` with your own numbers (see `src/blackbox_bill/whatif.py` for the format).

```
blackbox-bill --csv export.csv --markdown report.md --since 2026-09-01
```

Exit codes: `0` ok, `2` usage error (unreadable CSV, unknown plan). Missing local log directories are warnings, not errors — the bill alone is still explainable.

## Non-goals (what this is NOT)

- **[agentcost-cli](https://dev.to/justinli2026/how-i-built-a-unified-cost-tracker-for-ai-coding-agents-claude-code-cursor-codex-1nid)** does multi-tool budget observability dashboards with WARN/OVERRUN budget lines. blackbox-bill has no dashboards, no budget lines, no MCP server — it explains one bill, once.
- **resume-budget-guard** (sibling project) enforces cumulative spend caps across Claude Code `--resume` runs. blackbox-bill never blocks or enforces anything; it is read-only analysis.
- **ccusage** covers Claude Code usage attribution (daily/monthly/session/blocks, per-model breakdown). blackbox-bill deliberately does not re-do Claude Code attribution — its job is the *cross-tool routing black box* (Cursor pools, unexpected providers, silent model swaps).

One sentence: the others tell you *how much* you spent or *stop* you from spending; blackbox-bill tells you *why the bill looks wrong*.

## Honest limitations

- **Cursor changes its export format.** The parser fuzzy-matches headers and fails soft on missing columns, but a radically new format may need new aliases — contributions welcome.
- **Routing rules are inferred, not official.** Explanations are heuristics built from observed community patterns ("Other Models drains first", "Grok Bot mis-bucketing"). They are not Cursor/OpenAI/Anthropic documentation, and every explanation says so.
- **Times are local-timezone best-effort.** Bill timestamps and local log timestamps are compared as wall-clock times on the same machine. If your export is in UTC and your logs are local, widen `--window-hours` or convert first.
- **The join is evidence, not proof.** A bill row with no matching local session might be a background agent, a headless run, another machine, or a leaked key — the tool reports the gap; you investigate.
- **Token-based re-pricing is approximate.** The what-if simulator re-prices tokens at flat rates; real plans have request-based pricing, overage rules, and weekly pools (e.g. SuperGrok bundles) that flat rates cannot model. Use `--rates-file` for your own numbers.

## Install

```bash
pip install blackbox-bill
# or from source:
pip install .
```

Zero dependencies — Python 3.9+ stdlib only.

## Example output

```
blackbox-bill v0.1.0 — where the money went
============================================================
Bill rows: 5   requests: 68   billed total: $27.00

## Pool-normalized summary
cursor-models     2 rows       8 reqs  $     3.00
    claude-opus-5.5                    8 reqs  $    3.00
other-models      3 rows      60 reqs  $    24.00
    grok-4                            40 reqs  $   16.00
    gemini-4                          20 reqs  $    8.00

## Explanations

[pool-drain] Why your 'Other Models' pool drained first
Auto-model routing sent most of your billable work to non-Cursor models...
Evidence: 3 row(s), 60 request(s), $24.00.
...
```

## Development

```bash
python3 tests/test_blackbox_bill.py   # 41 checks, <1s
```

## License

MIT
