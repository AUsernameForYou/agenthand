# Changelog

All notable changes to agenthand are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project uses
semantic versioning.

## [0.1.0] - 2026-10-27

First public release. agenthand is an MCP server (stdio transport) that
gives AI agents three tools for turning a seller's messy product photos
into listing-ready material. The seller never opens it; their agent
calls it.

### Added

- `identify` — reads a photo and plans the listing shot: a structured
  description of the item, the cut-out concept with alternates, a
  suggested title, listing bullets, and condition notes.
- `cut_out` — cuts the product out of its photo and returns a
  transparent PNG behind an `asset://` handle. The mask is
  quality-checked, with one bounded retry on a poor mask, and the
  result includes a mask review (bounding box, fill, frame-edge
  contact, warnings).
- `draw` (Fidelity Lock) — stages the real product into a generated
  scene without regenerating the product. The stored foreground is
  composited untouched onto a generated background plate, and the
  result ships with a fidelity receipt: hashes for the source photo,
  the foreground, and the mask, plus the changed-pixel count measured
  on the delivered file. The receipt also states what the count
  excluded.
- `agenthand-verify` — a standalone checker that re-derives a fidelity
  receipt from the files on disk, and can verify an exported
  source/final/mask triple with no server involved. It passes only on
  exactly zero changed pixels with real coverage.
- One billing choke point for every model call: reserve-then-settle
  accounting against a local ledger, a fail-closed spend cap, and a
  cost receipt per call. A response with no usable usage settles at
  its reserved projection rather than $0, and an unpriced model raises
  instead of estimating.
- Ingest by asset handle, http(s) URL, or base64 data URI, with size
  caps, EXIF orientation applied, and HEIC support. Colour profiles
  are preserved end to end and recorded on the receipt.
- Images chain between tools as `asset://` handles instead of bytes,
  and every asset is readable by the client as an MCP resource at its
  own URI.
