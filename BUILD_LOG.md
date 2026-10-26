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

## 2026-10-26 - Junction-1 Gauntlet fix list

The scaffold went through the J1 Gauntlet (four model lanes: Grok, GPT,
Claude opus, Claude Fable; verdict GO WITH CHANGES, all four lanes
converging on the same four defects). This change set implements the
adopted fix list. Still zero external API calls: model wiring waits for
the hackathon credits to land.

- Billing choke point: new module `meta_client.py`. Every billable call
  goes through `billed_call`, which reserves the projected worst-case
  cost as a pending ledger receipt under an fcntl file lock, runs the
  call, then settles with the actual cost priced from the provider's
  usage. A call that raises leaves its reservation counted. Tools no
  longer touch the ledger directly. The module documents that the future
  SDK client must be built with max_retries=0 so retries cannot bypass
  the gate.
- Ledger hardening: receipts gained phase (final/pending/settled) and
  call_id; totals count settled actuals over their reservations and
  unsettled reservations at projection. Negative or non-finite amounts
  (including JSON NaN lines) fail closed, as do NaN/inf projections and
  caps.
- Pricing contract: unknown models raise instead of estimating $0.
  Cached input tokens are now priced as the cached subset of total input
  (fresh remainder at the fresh rate), fixing a double count under the
  old convention. Receipt usage fields (input/output/cached tokens,
  images) are actually stored now; the scaffold's receipt helper dropped
  them.
- Cap: working default is $120.00 under a $150.00 hard ceiling; the env
  var can only lower it, and unparseable or non-finite values fail
  closed.
- Asset store: the single rewritten assets.json index is replaced by
  per-asset sidecar JSON files written via temp file + os.replace; the
  legacy index is still read as a fallback. Records gained kind constants
  (source_photo / foreground_png / mask_png / background_plate /
  composite), the source_asset_id lineage chain, and a pixel_sha256 over
  decoded RGBA bytes alongside the file hash. verify_integrity checks
  both hashes and raises on mismatch.
- Ingest and egress: new `ingest.py`. A tool's image argument may be an
  asset handle, an http(s) URL, or a base64 data URI; ingest enforces
  byte and pixel caps, applies EXIF orientation, and normalizes to 8-bit
  RGBA PNG. Assets are registered as MCP resources at their own
  asset://<id> URIs, and draw returns an image preview with its result.
- Fidelity Lock as code: new `composite.py`. draw pre-validation refuses
  any asset that is not a foreground_png chained to a source_photo, and
  verifies stored hashes before compositing. The background-plate
  function takes only a prompt and dimensions (a deterministic local
  placeholder until muse-image is wired). The compositor fits the plate
  to the product, never scales the product, and the fidelity receipt
  reports file and pixel hashes for source/foreground/mask, the measured
  changed-opaque-pixel count (must be 0), and the edge-band count. The
  claim wording is now precise throughout: the stored extracted
  foreground reaches the composite unchanged (it is not a claim about
  the source photo's hash).
- Framing: README and docstrings reordered draw-first, identify last as
  the planning step inside the chain. cut_out's concept parameter is now
  optional; omitted, it runs the planning step internally.
- Tests: 66 pass (was 23), covering the unknown-model raise, NaN/inf/negative
  refusals, reservation-on-exception, a two-thread gate race, usage
  round-trip, sidecar and legacy-index reads, ingest caps and EXIF
  normalization, and the composite invariant test (solid-color fake
  plate, known foreground, zero changed opaque pixels). A live stdio
  smoke test exercises tools/list, resource templates, all three tools,
  and reading the composite back through its resource URI.
