# Build Log

## Provenance

agenthand was created fresh, from an empty directory, inside the Meta Global
AI Developer Hackathon build window (Oct 26 - Nov 4, 2026). It is original
work written during the window for this event. No code was copied from any
earlier project; design ideas documented before the window (tool contracts,
the Fidelity Lock compositing design, pricing research) were planning notes,
and every line of implementation here was written in-window. This repository
starts with a single scaffolding commit so the in-window history is complete
and auditable.

## 2026-10-26 - Project scaffolding

- Created the project skeleton: pyproject (Python 3.12, official MCP Python
  SDK), src layout, README, .gitignore, .env.example (placeholder names only,
  no keys anywhere in the repo).
- MCP server over stdio with the three tools - identify, cut_out, draw -
  registered and stubbed. Stubs return structured "not yet wired" results
  and make no external calls.
- Asset handle system: images stored locally under a data dir, referenced
  as asset://<id>, sha256 recorded per asset at ingest, with an integrity
  re-check the Fidelity Lock receipt will rely on.
- Cost receipts: every tool call appends a JSONL receipt (tool, model,
  tokens, USD estimate, pricing-table version). Pricing table holds the
  verified Meta Model API prices: muse-spark-1.3 $1.25/$4.25 per 1M in/out
  (cached $0.15), muse-wonder-1.0 $4/$20 (cached $0.20), muse-image-1.0
  $0.01/image, sam-3.1 $2.50 per 1,000 images.
- Fail-closed spend cap: TOTAL_SPEND_CAP_USD defaults to $150.00 (the
  hackathon credit amount, the only authorized spend). The gate checks the
  ledger before any billable call and refuses calls that would exceed it;
  a corrupt ledger also refuses, since unknown spend state means no spend.
- Unit tests cover pricing math, receipt logging from stub tool calls, the
  asset store, and the cap refusing a seeded over-cap ledger.
- Installed mcp 2.3.0 (Python SDK v2; the high-level server class is
  MCPServer, renamed from FastMCP in v1) and pytest 9.1.1 into a project
  venv. 23 unit tests pass, and a live stdio smoke test (initialize,
  tools/list, one stub call) confirms the server answers as an MCP server
  and writes its receipt.
