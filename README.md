# agenthand

An MCP server for AI agents that help people sell things.

The seller never opens agenthand. Their agent calls it. A seller hands their
agent a messy product photo; agenthand turns it into listing-ready material
through three tools:

| Tool | What it does |
|---|---|
| `identify` | Looks at a product photo and returns a structured description plus a segmentation-ready noun-phrase concept (with alternates) for the product. |
| `cut_out` | Segments the product out of its photo using that concept and returns a transparent PNG, referenced by an asset handle. |
| `draw` | Places the real product, pixel-untouched, into a generated scene (a chair in a regal room, a game lot on a clean table) and returns the composite with a fidelity receipt. |

## Fidelity Lock

`draw` never regenerates the product. The design invariant:

1. Segmentation extracts the real product foreground and its mask.
2. The product layer is stored untouched, with sha256 hashes recorded for the
   source, the foreground, and the mask.
3. The image model generates ONLY the background scene plate.
4. Deterministic code composites the original product layer onto that plate.
5. The result ships with a fidelity receipt: source hash, foreground hash,
   mask hash, the transformations applied, the count of changed foreground
   pixels, the model calls made, and their cost.

The claim is precise: the product layer was never regenerated and its
foreground hash matches the source. Edge pixels blend during compositing;
the receipt says exactly what happened instead of overclaiming.

A bounded mask-recovery loop guards `cut_out`: the mask is quality-checked,
and on a poor mask the tool retries once with an alternate concept phrase
from `identify`. Every attempt is traced in the receipt.

## Asset handles

Tools never pass image bytes back and forth. Every image is stored locally
and referenced as `asset://<id>`. Handles chain between tools:
`identify` reads one, `cut_out` returns a new one, `draw` consumes it.

## Cost receipts and the spend cap

Every tool call appends a receipt to a local JSONL ledger: tool, model,
tokens, USD estimate, and the pricing-table version used. Prices live in one
module (`src/agenthand/pricing.py`) so a price change is a one-line edit with
a version bump.

The server is fail-closed on spend. A total cap (default **$150.00**, the
hackathon credit amount) is checked against the ledger before any billable
call; a call that would push spend past the cap refuses before it runs.

## Status

Hackathon build in progress (Meta Global AI Developer Hackathon, Oct 26 -
Nov 4, 2026). The three tools are currently stubbed: they validate the
plumbing (asset handles, receipts, spend cap) and return structured
"not yet wired" results. No external API calls are made yet.

## Run it

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/agenthand          # stdio transport
.venv/bin/pytest             # unit tests
```

Configuration is by environment variable; see `.env.example`.
