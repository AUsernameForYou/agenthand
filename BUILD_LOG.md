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

## 2026-10-26 - identify wired to the Meta Model API (live verification pending key)

- identify now makes the real vision call. `meta_client.py` gained the
  live client path: an OpenAI-compatible client (new `openai` dependency)
  built against https://api.meta.ai/v1 with max_retries=0, so no SDK
  retry can bypass the spend gate. The call runs only as the fn of
  billed_call: the reservation is priced as a worst case (image tokens
  estimated from pixel dimensions at one token per 32x32 patch, plus a
  bounded max_tokens of 1024 at the output price, times two to cover the
  one possible repair call), and settlement is priced from the usage the
  response actually returns — receipts now carry real token counts for
  identify.
- The request asks muse-spark-1.3 for strict structured output
  (json_schema, strict): description, concept (one short noun phrase
  for segmentation), alternates, plus the listing fields from the J1
  framing fix — suggested_title, listing_bullets (3-5), condition_notes.
  The concept/alternates contract is enforced after the call: if the
  only defect is fewer than 2 usable alternates, one text-only repair
  call asks for more phrases (its usage is added to the settlement);
  anything still unusable raises and the reservation stays counted.
- Key handling: the key is read from META_API_KEY (environment, or a
  local .env loaded by config — .env is gitignored, verified; no key
  material exists in the repo). The key check runs BEFORE billed_call:
  with no key configured, identify returns a structured NOT_CONFIGURED
  error and writes nothing to the ledger. An unusable model response
  returns a structured BAD_MODEL_RESPONSE error with the counted cost
  attached. cut_out's internal planning step runs this same identify
  path and degrades to an empty plan (with the error surfaced as
  plan_error) when planning fails.
- Live smoke script at scripts/smoke_identify.py: takes an image path,
  runs identify through the tool layer, prints the structured result
  and the ledger receipt lines. NOT run yet — no API key exists on this
  machine. Live verification of identify waits for the key to be
  provided; everything else about the wiring is covered offline.
- Tests: 76 pass (was 66). New tests/test_identify.py runs the whole
  path against a mocked transport: structured output parses into the
  result shape and settles with non-zero token counts on the receipt;
  a malformed response fails closed with the reservation counted;
  a missing key yields NOT_CONFIGURED with zero ledger writes; an
  over-cap ledger refuses before any API call; the alternates shortfall
  is repaired once (usage summed across both calls) and fails closed
  when the repair also falls short. The two scaffold-era tests that
  encoded identify's stub shape were updated to the wired contract.

## 2026-10-26 - First live identify call: truncation found and fixed (live re-verification pending)

- First live smoke (scripts/smoke_identify.py, real key, real call):
  HTTP 200 from api.meta.ai, but the tool returned BAD_MODEL_RESPONSE —
  "Identify response is not valid JSON: Unterminated string starting
  at line 1 column 461". The response was cut off mid-string at
  max_tokens=1024. The fail-closed path behaved as designed: the
  reservation was written before the call and stayed counted.
- Settlement check against the real ledger (data/ledger.jsonl): the
  smoke call (call_id ff934d79819b4af89fb6725f6f936151) has exactly one
  line, the pending reservation at $0.01323, and no settled line —
  correct for a raised call under reserve-then-settle: no settlement is
  written on failure and ledger_total_usd counts the reservation
  exactly once (total verified at $0.01323; not dropped, not doubled).
- Fixes:
  - max_tokens raised 1024 -> 2048 (IDENTIFY_MAX_TOKENS);
    project_identify_cost_usd prices from the same constant, so the
    worst-case reservation tracks the new bound (6x4 test image:
    $0.01323 -> $0.0188), keeping the x2 repair-call factor.
  - The identify prompt now demands brevity inside the schema fields:
    description at most 40 words, each listing bullet at most 12
    words, condition_notes at most 25 words. Schema unchanged.
  - Raw-response capture: when an identify response body cannot be
    parsed as JSON, the full raw text is saved to
    data/debug/identify-<call_id>.txt (named via a context var set by
    billed_call, so the file correlates with the ledger call) and the
    BAD_MODEL_RESPONSE error result carries debug_path. Raw model text
    is never written to the ledger.
- Test hermeticity: the repo .env now holds a real key for manual
  smokes, and config's dotenv loader was leaking it into
  tests/test_tools.py — its cut_out internal-plan test made real API
  calls during two suite runs in this session (spend reserved in tmp
  ledgers; the real ledger was not touched). The module's autouse
  fixture now deletes META_API_KEY and pins the dotenv loader off, so
  the suite is fully offline again.
- Tests: 77 pass (was 76), including a new offline test with a mocked
  truncated JSON body asserting BAD_MODEL_RESPONSE, the debug file's
  path and contents, no raw text in the ledger, and the reservation
  counted exactly once. Live re-verification pending: re-run
  scripts/smoke_identify.py with the key configured.
