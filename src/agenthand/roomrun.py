"""ROOM RUN — the multi-item room intake layer (prototype, not an MCP tool).

agenthand started as a one-photo chain: identify -> cut_out -> draw turns
ONE product photo into a staged listing with a Fidelity Lock receipt. The
product Daniel locked on 2026-10-28 (tracker decision 51) is room-scale:
the seller walks a room, names what goes ("this cup, this bear, this
chair"), and gets one staged listing per item — every item with its own
receipt. This module is that intake layer. It orchestrates the three
shipped tools over a SET of room photos (stills, plus frames sampled
from a walkthrough video); it adds no model calls of its own and is NOT
a fourth MCP tool — the three-tool surface stays exactly as shipped.

Pipeline:

1. Sources. Stills from the given files/dirs, plus frames sampled from
   each given video with ffmpeg (fixed interval, timestamps recorded).
   A video frame is just another source photo with provenance.
2. Plan pass. identify runs once per source photo. Its plan powers
   photo ranking, auto-enumerate mode, and listing copy (when the
   plan's concept actually matches the item — see the honesty note
   below). A photo whose plan fails is still ingested and probed;
   it just ranks blind.
3. Per item. Candidate photos are ranked by token overlap between the
   item's name and each photo's plan text (stills before frames on
   ties — they are sharper and larger). cut_out probes the top
   candidates with the item's name as the SAM concept (plus bounded
   deterministic variants: the photo plan's own phrase when it overlaps
   the name, a colour-stripped form, the head noun). Every probe is a
   sighting, success or failure, and lands in the manifest.
4. De-dup. One named item = one listing: its successful sightings
   across photos are the same physical item seen from several angles;
   the primary sighting (most opaque foreground pixels, the closest /
   clearest view) is the one staged, the rest are recorded as
   also-seen-in. In auto-enumerate mode, per-photo candidates are
   grouped by concept similarity — a group merges ONLY on strong
   evidence (high token similarity, or similar names whose masks on
   near-duplicate viewpoints overlap); the grey zone keeps both
   candidates and flags them possible duplicates. Across items, two
   listings whose primary foregrounds are pixel-identical, or whose
   primary masks on the same photo almost perfectly overlap, are
   flagged as possible duplicates of each other. NOTHING is ever
   silently merged away: a flag keeps both listings and says why.
5. Stage. draw runs the primary foreground with ONE shared scene
   prompt for the whole room (the room's plate style), so the set of
   listings reads as one room's worth of staging. The per-item
   fidelity receipt is the draw tool's own, persisted beside the
   composite in the store and linked from the manifest.
6. Verify. A staged item is not believed on draw's say-so: the
   repo's independent verifier (composite.verify_receipt_from_disk)
   re-reads the item's stored source, foreground, and composite
   files, re-checks their hashes, and recounts the changed opaque
   pixels from disk. Its verdict lands in the manifest as the item's
   "verification" field, distinct from the draw receipt it checks.
   An item whose verification fails is reported verify_failed, not
   staged — the demo-ready Gauntlet's fix #1: a room run "verifies
   end to end" only when an independent path re-proves each item.
7. Manifest. JSON + a readable summary: room id, sources, per-item
   status (staged / failed / verify_failed, with the stage and error
   that failed it), receipt links and pixel counts, the independent
   verification per item, sightings, duplicate flags, and the run's
   total cost read as the delta of the run's own cost ledger across
   the run — the amounts the spend cap actually counted, failed
   attempts included. Partial failure is a first-class outcome: a
   room where four items stage and two honestly fail is a successful
   room run with a truthful manifest.

Honesty notes (v1 limits, stated rather than smoothed over):

- identify plans ONE primary subject per photo. In a crowded room
  photo its listing copy describes that subject, so per-item listing
  copy is attached ONLY when the photo plan's concept matches the
  item; otherwise the manifest says copy was not generated for the
  item instead of dressing it in another item's words. Auto-enumerate
  likewise surfaces one candidate per viewpoint; several viewpoints
  of one room are what surface several items.
- The seller's name is the segmentation concept. SAM sometimes wants
  a different phrasing than a human uses; the bounded variant list is
  the recovery, and a name no variant can segment is a failed item
  with NO_MATCH in the manifest, never a quietly substituted object.

Spend discipline: every model call goes through the shipped tools, so
every dollar flows through meta_client.billed_call under its
fail-closed cap — this module never touches the API directly. Room
runs keep their OWN ledger (default: ledger.jsonl inside the output
dir; --ledger overrides): during a run, AGENTHAND_LEDGER_PATH points
at the room ledger so every receipt the run causes lands there, and
the server's cumulative ledger — the one the published proof run's
accounting lives in — is never touched by an experiment (demo-ready
Gauntlet separation gate). The per-run budget (default $1.00) plus
the worst-case projection checked before the first call are the
binding bounds on room spend; the manifest's total is the room
ledger's delta across the run.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import config, ingest, ledger
from .assets import AssetStore

# The shared plate style for a room run: one scene prompt for every
# item, so a room's listings stage into the same visual world. draw
# wraps it in its own fixed "empty scene, clear central surface"
# instruction; this string only has to name the room.
DEFAULT_SCENE = (
    "a bright, calm room with a plain warm-white wall and a light "
    "wooden table surface, soft daylight"
)

# Bounds on the probing work (and therefore the spend) per item.
MAX_PHOTOS_PER_ITEM = 3
MAX_VARIANTS_PER_PHOTO = 2
MAX_SAM_CALLS_PER_ITEM = 6

# De-dup evidence thresholds. Merging two enumerated candidates into
# one item needs name similarity at or above MERGE_SIMILARITY (or the
# mask-overlap corroboration in group_candidates); similarity in
# [FLAG_SIMILARITY, MERGE_SIMILARITY) is the grey zone — keep both,
# flag both.
MERGE_SIMILARITY = 0.6
FLAG_SIMILARITY = 0.4
MASK_IOU_FLAG = 0.9

# Leading adjectives the variant generator may strip to widen a SAM
# phrase ("gray storage box" -> "storage box"). Colours and sizes are
# the words sellers add that SAM least needs.
_STRIPPABLE_LEADERS = {
    "black", "white", "gray", "grey", "red", "orange", "yellow",
    "green", "blue", "purple", "pink", "brown", "wooden", "wood",
    "large", "small", "big", "little", "vintage", "antique",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Pure helpers (no store, no API — unit-tested offline)
# ---------------------------------------------------------------------------


def tokens_of(text: str | None) -> list[str]:
    """Lowercase word tokens of a name or phrase, order preserved."""
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _token_set(text: str | None) -> set[str]:
    return set(tokens_of(text))


def token_similarity(a: str | None, b: str | None) -> float:
    """Jaccard similarity of two phrases' token sets (0.0–1.0).

    A shared token counts when the tokens are equal or one is a
    prefix of the other with length >= 4 ("book" ~ "bookcase"), which
    is how a seller's short name meets identify's longer concepts.
    """
    set_a, set_b = _token_set(a), _token_set(b)
    if not set_a or not set_b:
        return 0.0

    def _matches(token: str, others: set[str]) -> bool:
        return any(
            token == other
            or (len(token) >= 4 and other.startswith(token))
            or (len(other) >= 4 and token.startswith(other))
            for other in others
        )

    shared = sum(1 for t in set_a if _matches(t, set_b))
    union = len(set_a) + len(set_b) - shared
    return shared / union if union else 0.0


def plan_text(plan: dict | None) -> str:
    """All the searchable text of an identify plan, joined."""
    if not plan:
        return ""
    parts = [
        plan.get("description") or "",
        plan.get("concept") or "",
        plan.get("suggested_title") or "",
        " ".join(plan.get("listing_bullets") or []),
        plan.get("condition_notes") or "",
    ]
    return " ".join(parts)


def plan_matches_item(plan: dict | None, item_name: str) -> bool:
    """True when the plan's own concept names this item.

    Listing copy is only honest when the plan was ABOUT the item;
    a crowded photo's plan describes its primary subject, which may
    be a different object entirely.
    """
    if not plan or not plan.get("concept"):
        return False
    return token_similarity(plan["concept"], item_name) >= MERGE_SIMILARITY


def concept_variants(item_name: str, plan: dict | None = None) -> list[str]:
    """Bounded SAM-phrase variants for one item, best first.

    Order: the seller's name (lowercased); the photo plan's own
    concept when it overlaps the name (identify's phrasing is tuned
    for SAM); the colour/size-stripped form; the head noun alone.
    Duplicates removed, order preserved.
    """
    name = " ".join(tokens_of(item_name))
    variants: list[str] = []
    if name:
        variants.append(name)
    if plan and plan.get("concept") and plan_matches_item(plan, item_name):
        concept = " ".join(tokens_of(plan["concept"]))
        if concept and concept not in variants:
            variants.append(concept)
    words = name.split()
    if len(words) > 1 and words[0] in _STRIPPABLE_LEADERS:
        stripped = " ".join(words[1:])
        if stripped not in variants:
            variants.append(stripped)
    if len(words) > 1:
        head = words[-1]
        if head not in variants:
            variants.append(head)
    return variants


def bbox_iou(a: list[float] | None, b: list[float] | None) -> float:
    """IoU of two [x0, y0, x1, y1] boxes (any consistent units)."""
    if not a or not b:
        return 0.0
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def normalized_bbox(bbox: list | None, width: int | None, height: int | None):
    """A mask bbox in source pixels -> frame-relative [0..1] coords."""
    if not bbox or not width or not height:
        return None
    return [
        bbox[0] / width,
        bbox[1] / height,
        bbox[2] / width,
        bbox[3] / height,
    ]


@dataclass
class Candidate:
    """One enumerated candidate item from one photo's plan (auto mode)."""

    name: str
    photo_index: int
    plan: dict


def group_candidates(candidates: list[Candidate]) -> list[dict]:
    """Group auto-enumerated candidates into items, conservatively.

    Returns a list of groups: {"name", "photo_indices", "flags"}.
    A candidate joins a group only on strong evidence: concept
    similarity >= MERGE_SIMILARITY with the group's name. Similarity
    in the grey zone does NOT merge — the candidate starts its own
    group and both groups are flagged as possible duplicates of each
    other. This is the "when unsure, keep both and flag" rule.
    """
    groups: list[dict] = []
    for cand in candidates:
        matched = None
        grey: list[dict] = []
        for group in groups:
            sim = token_similarity(cand.name, group["name"])
            if sim >= MERGE_SIMILARITY:
                matched = group
                break
            if sim >= FLAG_SIMILARITY:
                grey.append(group)
        if matched is not None:
            if cand.photo_index not in matched["photo_indices"]:
                matched["photo_indices"].append(cand.photo_index)
            continue
        group = {
            "name": cand.name,
            "photo_indices": [cand.photo_index],
            "flags": [],
        }
        for other in grey:
            flag = (
                f"possible duplicate of '{other['name']}' "
                f"(name similarity in the grey zone; kept separate)"
            )
            group["flags"].append(flag)
            other["flags"].append(
                f"possible duplicate of '{cand.name}' "
                f"(name similarity in the grey zone; kept separate)"
            )
        groups.append(group)
    return groups


# ---------------------------------------------------------------------------
# Sources: stills + sampled video frames
# ---------------------------------------------------------------------------


@dataclass
class PhotoSource:
    """One source photo in the pool, with provenance and its plan."""

    path: Path
    origin: str  # "still" | "video_frame"
    video_path: str | None = None
    timestamp_s: float | None = None
    handle: str | None = None
    width: int | None = None
    height: int | None = None
    plan: dict | None = None
    plan_error: dict | None = None

    def manifest_entry(self, index: int) -> dict:
        plan = None
        if self.plan:
            plan = {
                "description": self.plan.get("description"),
                "concept": self.plan.get("concept"),
                "suggested_title": self.plan.get("suggested_title"),
            }
        return {
            "index": index,
            "path": str(self.path),
            "origin": self.origin,
            "video": self.video_path,
            "timestamp_s": self.timestamp_s,
            "handle": self.handle,
            "width": self.width,
            "height": self.height,
            "plan": plan,
            "plan_error": self.plan_error,
        }


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif"}


def collect_stills(paths: list[Path]) -> list[Path]:
    """Expand files/dirs into a sorted list of image files."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(
                sorted(
                    p for p in path.rglob("*") if p.suffix.lower() in _IMAGE_SUFFIXES
                )
            )
        elif path.is_file():
            found.append(path)
    return found


def sample_video_frames(
    video_path: Path,
    out_dir: Path,
    *,
    every_seconds: float = 2.0,
    max_frames: int = 8,
    ffmpeg: str | None = None,
) -> tuple[list[tuple[Path, float]], str | None]:
    """Sample frames from a video with ffmpeg at a fixed interval.

    Returns (frames, note): frames are (path, timestamp_s) pairs.
    When ffmpeg is unavailable or the video cannot be probed, returns
    ([], reason) — video intake degrades to stills-only, loudly, in
    the manifest notes rather than failing the room.
    """
    ffmpeg = ffmpeg if ffmpeg is not None else shutil.which("ffmpeg")
    if not ffmpeg:
        return [], "ffmpeg not found on PATH; video frames not sampled"
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(video_path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    try:
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except Exception:
        return [], f"ffprobe could not read {video_path}; frames not sampled"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[Path, float]] = []
    t = 0.0
    while t < duration and len(frames) < max_frames:
        frame_path = out_dir / f"{video_path.stem}-t{t:05.1f}s.jpg"
        result = subprocess.run(
            [
                ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "3", str(frame_path), "-y",
            ],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and frame_path.exists() and frame_path.stat().st_size:
            frames.append((frame_path, round(t, 2)))
        t += every_seconds
    if not frames:
        return [], f"ffmpeg extracted no frames from {video_path}"
    return frames, None


# ---------------------------------------------------------------------------
# The chain: shipped tools behind injectable adapters (offline tests
# substitute fakes; production uses server.identify/cut_out/draw).
# ---------------------------------------------------------------------------


def _default_verify(store: AssetStore, composite_handle: str) -> dict:
    """The production verifier: the repo's independent receipt check.

    composite.verify_receipt_from_disk re-reads the stored source,
    foreground, and composite files, re-checks every file against its
    recorded hashes, and recounts the changed opaque pixels fresh
    from disk — nothing is taken from draw's in-memory receipt except
    the claims being checked. The receipt persisted beside the
    composite is the one graded, so what verifies is what a judge
    with the store would find.
    """
    from . import composite

    return composite.verify_receipt_from_disk(store, composite_handle)


def _run_verification(ctx: "_RunContext", composite_handle: str) -> dict:
    """Run the verifier over one staged item; shape its manifest field.

    The returned dict is the manifest's "verification" record —
    deliberately distinct from the item's "receipt" (draw's own
    numbers): these are the independent recount's numbers. A
    verifier that cannot run at all (no persisted receipt, an
    unknown handle) is a FAILED verification, never an exception
    that aborts the room and never a silent pass: an item nobody
    could check has not been shown to be intact.
    """
    try:
        report = ctx.verify_fn(ctx.store, composite_handle)
    except Exception as exc:  # noqa: BLE001 — verifier boundary, as in composite
        return {
            "ok": False,
            "changed_opaque_pixels": None,
            "opaque_pixels": None,
            "color_profile_preserved": None,
            "problems": [
                f"the independent verifier could not run: "
                f"{type(exc).__name__}: {exc}"
            ],
            "checks_failed": [],
        }
    recomputed = report.get("recomputed") or {}
    checks = report.get("checks") or {}
    return {
        "ok": bool(report.get("ok")),
        "changed_opaque_pixels": recomputed.get("changed_opaque_pixels"),
        "opaque_pixels": recomputed.get("opaque_pixels"),
        "color_profile_preserved": recomputed.get("color_profile_preserved"),
        "problems": list(report.get("problems") or []),
        "checks_failed": sorted(k for k, v in checks.items() if not v),
    }


def _default_chain() -> dict:
    from . import server

    def identify_fn(image_ref: str) -> dict:
        return server.identify(image_ref)

    def cut_out_fn(handle: str, concept: str) -> dict:
        return server.cut_out(handle, concept)

    def draw_fn(handle: str, scene: str) -> dict:
        result = server.draw(handle, scene)
        return result[0] if isinstance(result, list) else result

    return {"identify": identify_fn, "cut_out": cut_out_fn, "draw": draw_fn}


def _data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _call_cost(result: dict) -> float:
    """What one tool call counted as spend, success or failure."""
    if not isinstance(result, dict):
        return 0.0
    if result.get("cost_usd") is not None:
        return float(result["cost_usd"])
    error = result.get("error") or {}
    if error.get("cost_counted_usd") is not None:
        return float(error["cost_counted_usd"])
    return 0.0


def _opaque_pixels(store: AssetStore, handle: str | None) -> int | None:
    """Opaque (alpha > 0) pixel count of a stored foreground image."""
    if not handle:
        return None
    try:
        import numpy as np

        image = store.read_image(handle)
        alpha = np.asarray(image.getchannel("A"))
        return int(np.count_nonzero(alpha > 0))
    except Exception:
        return None


@dataclass
class _RunContext:
    store: AssetStore
    chain: dict
    scene: str
    verify_fn: object = None  # (store, composite_handle) -> report dict
    max_photos_per_item: int = MAX_PHOTOS_PER_ITEM
    max_sam_calls_per_item: int = MAX_SAM_CALLS_PER_ITEM
    sam_calls: int = 0  # per item; reset by the item loop
    flags: list[str] = field(default_factory=list)


def _rank_photos(item_name: str, sources: list[PhotoSource]) -> list[int]:
    """Source indices in probe order for one item.

    Ranked by plan-text evidence for the item, strongest signal
    first: a photo whose plan names the item's HEAD NOUN (the item's
    last token — "hat", "suitcase", "box") outranks every photo
    whose plan merely shares a modifier with the name. The flat
    overlap this replaced let a colour word do exactly that damage
    in the first real room run: three photos whose plans mentioned
    "black" (a suitcase's trim, a railing, a purifier's grille)
    outranked every photo that actually held the black hat, because
    the hat-bearing plans said "hat" and never "black" — and the
    probe cap then spent itself on the wrong photos. Within the
    head-noun tier, token overlap of the full name decides; ties
    break toward stills (sharper, larger) and then toward the
    earlier source. The full ordering is returned — mentioning
    photos first, then every silent photo as coverage fill (stills
    before frames) — and the caller's probe cap bounds how far down
    the order a run actually probes. A silent plan is weak evidence
    of absence: identify describes only a photo's primary subject,
    so the bookcase photo whose plan says "bookshelf" can still hold
    the hat on its top shelf.
    """
    item_tokens = tokens_of(item_name)
    head_noun = item_tokens[-1] if item_tokens else None

    def _matches(token: str, others: set[str]) -> bool:
        return any(
            token == other
            or (len(token) >= 4 and other.startswith(token))
            or (len(other) >= 4 and token.startswith(other))
            for other in others
        )

    def score(idx: int) -> float:
        plan = sources[idx].plan
        if not plan:
            return 0.0
        text_tokens = _token_set(plan_text(plan))
        item_token_set = _token_set(item_name)
        if not text_tokens or not item_token_set:
            return 0.0
        shared = sum(1 for t in item_token_set if _matches(t, text_tokens))
        overlap = shared / len(item_token_set)
        head_bonus = 1.0 if head_noun and _matches(head_noun, text_tokens) else 0.0
        return head_bonus + overlap

    scored = [(score(i), i) for i in range(len(sources))]
    order = sorted(
        scored,
        key=lambda pair: (
            -pair[0],
            0 if sources[pair[1]].origin == "still" else 1,
            pair[1],
        ),
    )
    return [i for _, i in order]


def _probe_item(
    ctx: _RunContext,
    item_name: str,
    sources: list[PhotoSource],
    candidates: list[int],
    *,
    stop_on_success: bool,
) -> list[dict]:
    """cut_out probes for one item across its candidate photos.

    Returns the sighting records (successes and failures). Bounded by
    the context's per-item SAM call budget: variants are tried
    best-first per photo. With stop_on_success, probing ends at the
    first photo that cuts cleanly (the cheaper mode); without it,
    every candidate is probed so the manifest carries the full
    cross-photo sighting evidence the de-dup record is built from.
    """
    sightings: list[dict] = []
    for idx in candidates:
        source = sources[idx]
        if source.handle is None:
            sightings.append(
                {
                    "photo_index": idx,
                    "status": "error",
                    "concept_used": None,
                    "foreground_asset": None,
                    "mask_asset": None,
                    "mask_overlay_asset": None,
                    "opaque_pixels": None,
                    "bbox_normalized": None,
                    "mask_warnings": [],
                    "cost_usd": 0.0,
                    "error": {"code": "NO_HANDLE", "message": "photo was not ingested"},
                }
            )
            continue
        variants = concept_variants(item_name, source.plan)
        tried = 0
        photo_cost = 0.0
        success = None
        last_error = None
        for variant in variants:
            if tried >= MAX_VARIANTS_PER_PHOTO or ctx.sam_calls >= ctx.max_sam_calls_per_item:
                break
            tried += 1
            ctx.sam_calls += 1
            result = ctx.chain["cut_out"](source.handle, variant)
            photo_cost += _call_cost(result)
            if result.get("status") == "ok":
                success = (variant, result)
                break
            last_error = result.get("error") or {"code": "UNKNOWN", "message": str(result)}
        if success is not None:
            variant, result = success
            review = result.get("mask_review") or {}
            sightings.append(
                {
                    "photo_index": idx,
                    "status": "ok",
                    "concept_used": variant,
                    "foreground_asset": result.get("output_asset"),
                    "mask_asset": result.get("mask_asset"),
                    "mask_overlay_asset": result.get("mask_overlay_asset"),
                    "opaque_pixels": _opaque_pixels(ctx.store, result.get("output_asset")),
                    "bbox_normalized": normalized_bbox(
                        review.get("bbox"), source.width, source.height
                    ),
                    "mask_warnings": review.get("warnings", []),
                    "cost_usd": photo_cost,
                    "error": None,
                }
            )
            if stop_on_success:
                break
        else:
            sightings.append(
                {
                    "photo_index": idx,
                    "status": "error",
                    "concept_used": variants[0] if variants else None,
                    "variants_tried": tried,
                    "foreground_asset": None,
                    "mask_asset": None,
                    "mask_overlay_asset": None,
                    "opaque_pixels": None,
                    "bbox_normalized": None,
                    "mask_warnings": [],
                    "cost_usd": photo_cost,
                    "error": last_error,
                }
            )
    return sightings


def _process_item(
    ctx: _RunContext,
    item: dict,
    sources: list[PhotoSource],
    *,
    probe_all: bool,
) -> dict:
    """Run one item through probe -> primary -> draw; build its record."""
    name = item["name"]
    record: dict = {
        "name": name,
        "status": "failed",
        "stage_reached": "probe",
        "failure": None,
        "sightings": [],
        "primary_photo_index": None,
        "also_seen_in": [],
        "output_asset": None,
        "receipt": None,
        "receipt_file": None,
        "verification": None,
        "listing": None,
        "possible_duplicate_of": [],
        "flags": list(item.get("flags", [])),
        "cost_usd": 0.0,
    }
    ctx.sam_calls = 0
    sightings = _probe_item(
        ctx,
        name,
        sources,
        item["candidate_photo_indices"],
        stop_on_success=not probe_all,
    )
    record["sightings"] = sightings
    record["cost_usd"] += sum(s.get("cost_usd", 0.0) for s in sightings)

    successes = [s for s in sightings if s["status"] == "ok"]
    if not successes:
        errors = [s.get("error") or {} for s in sightings if s["status"] == "error"]
        first = errors[0] if errors else {}
        record["failure"] = {
            "stage": "cut_out",
            "code": first.get("code", "NO_MATCH"),
            "message": (
                f"no candidate photo yielded a cut-out for {name!r} "
                f"({len(sightings)} photo(s) probed)"
            ),
        }
        return record

    # Primary = the clearest view: most opaque product pixels; ties
    # toward stills, then the earlier photo.
    def _primary_key(s: dict) -> tuple:
        source = sources[s["photo_index"]]
        return (
            -(s.get("opaque_pixels") or 0),
            0 if source.origin == "still" else 1,
            s["photo_index"],
        )

    primary = sorted(successes, key=_primary_key)[0]
    record["primary_photo_index"] = primary["photo_index"]
    record["also_seen_in"] = [
        s["photo_index"] for s in successes if s is not primary
    ]

    # Listing copy only when the primary photo's plan was about this
    # item (see the module docstring's honesty note).
    plan = sources[primary["photo_index"]].plan
    if plan_matches_item(plan, name):
        record["listing"] = {
            "title": plan.get("suggested_title"),
            "bullets": plan.get("listing_bullets") or [],
            "condition_notes": plan.get("condition_notes"),
            "source": "photo_plan",
            "note": None,
        }
    else:
        record["listing"] = {
            "title": None,
            "bullets": [],
            "condition_notes": None,
            "source": "none",
            "note": (
                "the source photo's identify plan described a different "
                "primary subject; per-item listing copy was not generated "
                "rather than borrowed from another item"
            ),
        }

    record["stage_reached"] = "draw"
    draw_result = ctx.chain["draw"](primary["foreground_asset"], ctx.scene)
    record["cost_usd"] += _call_cost(draw_result)
    if draw_result.get("status") != "ok":
        error = draw_result.get("error") or {}
        record["failure"] = {
            "stage": "draw",
            "code": error.get("code", "DRAW_FAILED"),
            "message": error.get("message", "draw failed"),
        }
        return record

    receipt = draw_result.get("fidelity_receipt") or {}
    record["output_asset"] = draw_result.get("output_asset")
    record["receipt"] = {
        "source_asset": receipt.get("source_asset"),
        "foreground_pixel_sha256": receipt.get("foreground_pixel_sha256"),
        "opaque_pixels": receipt.get("opaque_pixels"),
        "changed_opaque_pixels": receipt.get("changed_opaque_pixels"),
        "color_profile_preserved": receipt.get("color_profile_preserved"),
        "cost_usd": draw_result.get("cost_usd"),
    }
    receipt_path = ctx.store.receipt_path(record["output_asset"])
    record["receipt_file"] = str(receipt_path) if receipt_path.exists() else None

    # Verify loop (demo-ready Gauntlet fix #1): draw's receipt is
    # draw grading its own work. The item counts as staged only when
    # the independent verifier, re-reading the stored files, agrees.
    record["stage_reached"] = "verify"
    verification = _run_verification(ctx, record["output_asset"])
    record["verification"] = verification
    if verification["ok"]:
        record["status"] = "staged"
        record["stage_reached"] = "done"
    else:
        record["status"] = "verify_failed"
        problems = verification["problems"]
        record["failure"] = {
            "stage": "verify",
            "code": "VERIFY_FAILED",
            "message": (
                "the staged output did not pass independent verification: "
                + ("; ".join(problems) if problems else "verifier reported failure")
            ),
        }
    return record


def _flag_cross_item_duplicates(records: list[dict], sources: list[PhotoSource]) -> None:
    """Flag (never merge) items that look like the same physical object.

    Evidence: identical primary-foreground pixel hashes, or primary
    masks on the SAME photo whose normalized bboxes almost perfectly
    overlap (two names segmenting one object).
    """
    staged = [r for r in records if r["status"] == "staged"]
    by_hash: dict[str, list[dict]] = {}
    for record in staged:
        fg_hash = (record.get("receipt") or {}).get("foreground_pixel_sha256")
        if fg_hash:
            by_hash.setdefault(fg_hash, []).append(record)
    for group in by_hash.values():
        if len(group) > 1:
            for record in group:
                others = [g["name"] for g in group if g is not record]
                record["possible_duplicate_of"] = sorted(
                    set(record["possible_duplicate_of"]) | set(others)
                )
                record["flags"].append(
                    "primary foreground pixel hash is identical to "
                    + ", ".join(others)
                    + "; listings kept separate pending a human call"
                )
    primaries = {}
    for record in staged:
        for sighting in record["sightings"]:
            if sighting["photo_index"] == record["primary_photo_index"]:
                primaries[record["name"]] = (record, sighting)
    names = list(primaries)
    for i, name_a in enumerate(names):
        for name_b in names[i + 1 :]:
            rec_a, sight_a = primaries[name_a]
            rec_b, sight_b = primaries[name_b]
            if sight_a["photo_index"] != sight_b["photo_index"]:
                continue
            iou = bbox_iou(
                sight_a.get("bbox_normalized"), sight_b.get("bbox_normalized")
            )
            if iou >= MASK_IOU_FLAG:
                for rec, other in ((rec_a, name_b), (rec_b, name_a)):
                    if other not in rec["possible_duplicate_of"]:
                        rec["possible_duplicate_of"].append(other)
                    rec["flags"].append(
                        f"primary masks on photo {sight_a['photo_index']} "
                        f"overlap {other!r} with IoU {iou:.2f}; kept separate"
                    )


def run_room(
    still_paths: list[Path],
    video_paths: list[Path],
    out_dir: Path,
    *,
    item_names: list[str] | None = None,
    auto: bool = False,
    scene: str = DEFAULT_SCENE,
    room_id: str | None = None,
    budget_usd: float = 1.00,
    frame_every_seconds: float = 2.0,
    max_frames_per_video: int = 8,
    max_photos_per_item: int = MAX_PHOTOS_PER_ITEM,
    probe_all_candidates: bool = True,
    store: AssetStore | None = None,
    chain: dict | None = None,
    spend_probe=None,
    verify_fn=None,
    ledger_path: Path | None = None,
) -> dict:
    """Run one room end to end and return (and write) its manifest.

    probe_all_candidates: when True (the default), every ranked
    candidate photo is probed for sightings even after a success —
    that cross-photo evidence is what the de-dup record is built
    from. When False, probing stops at the first success per item
    (cheaper, thinner manifest).

    ledger_path: the run's own cost ledger. The default is
    ledger.jsonl inside out_dir — room runs are experiments and
    never spend against the server's cumulative ledger (the one the
    published proof run's accounting lives in). For the run's
    duration AGENTHAND_LEDGER_PATH points at this ledger, so every
    receipt billed_call writes for the run lands in it; the previous
    value is restored on exit. The manifest records the path and its
    total is this ledger's delta across the run.

    verify_fn: (store, composite_handle) -> verifier report. The
    default is the repo's independent verifier
    (composite.verify_receipt_from_disk) — every staged item is
    re-proven from the stored files before the manifest calls it
    staged.
    """
    store = store if store is not None else AssetStore(config.get_data_dir())
    chain = chain if chain is not None else _default_chain()
    verify_fn = verify_fn if verify_fn is not None else _default_verify
    room_id = room_id or f"room-{uuid.uuid4().hex[:8]}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    room_ledger = (
        Path(ledger_path) if ledger_path is not None else out_dir / "ledger.jsonl"
    )

    previous_ledger_env = os.environ.get("AGENTHAND_LEDGER_PATH")
    os.environ["AGENTHAND_LEDGER_PATH"] = str(room_ledger)
    try:
        return _run_room_body(
            still_paths,
            video_paths,
            out_dir,
            item_names=item_names,
            auto=auto,
            scene=scene,
            room_id=room_id,
            budget_usd=budget_usd,
            frame_every_seconds=frame_every_seconds,
            max_frames_per_video=max_frames_per_video,
            max_photos_per_item=max_photos_per_item,
            probe_all_candidates=probe_all_candidates,
            store=store,
            chain=chain,
            spend_probe=spend_probe,
            verify_fn=verify_fn,
            room_ledger=room_ledger,
        )
    finally:
        if previous_ledger_env is None:
            os.environ.pop("AGENTHAND_LEDGER_PATH", None)
        else:
            os.environ["AGENTHAND_LEDGER_PATH"] = previous_ledger_env


def _run_room_body(
    still_paths: list[Path],
    video_paths: list[Path],
    out_dir: Path,
    *,
    item_names: list[str] | None,
    auto: bool,
    scene: str,
    room_id: str,
    budget_usd: float,
    frame_every_seconds: float,
    max_frames_per_video: int,
    max_photos_per_item: int,
    probe_all_candidates: bool,
    store: AssetStore,
    chain: dict,
    spend_probe,
    verify_fn,
    room_ledger: Path,
) -> dict:
    from . import server  # local import: server pulls in the MCP SDK

    if spend_probe is None:

        def spend_probe() -> float:  # noqa: F811 — closure default
            return ledger.ledger_total_usd(room_ledger)

    spend_before = spend_probe()

    # -- sources ------------------------------------------------------------
    sources: list[PhotoSource] = []
    video_notes: list[dict] = []
    for path in collect_stills([Path(p) for p in still_paths]):
        sources.append(PhotoSource(path=path, origin="still"))
    frames_dir = out_dir / "frames"
    for video in video_paths:
        video = Path(video)
        frames, note = sample_video_frames(
            video,
            frames_dir / video.stem,
            every_seconds=frame_every_seconds,
            max_frames=max_frames_per_video,
        )
        video_notes.append(
            {
                "path": str(video),
                "frames_sampled": len(frames),
                "frame_every_seconds": frame_every_seconds,
                "note": note,
            }
        )
        for frame_path, timestamp_s in frames:
            sources.append(
                PhotoSource(
                    path=frame_path,
                    origin="video_frame",
                    video_path=str(video),
                    timestamp_s=timestamp_s,
                )
            )

    # -- worst-case projection gate -----------------------------------------
    from . import meta_client

    def _dims(source: PhotoSource) -> tuple[int, int]:
        from PIL import Image

        with Image.open(source.path) as img:
            return img.width, img.height

    identify_projection = 0.0
    for source in sources:
        try:
            w, h = _dims(source)
        except Exception:
            w, h = 1200, 1600
        source.width, source.height = w, h
        identify_projection += (
            meta_client.project_identify_cost_usd(server.IDENTIFY_MODEL, w, h)
            * meta_client.IDENTIFY_MAX_ATTEMPTS
        )
    n_items_estimate = len(item_names) if item_names else len(sources)
    per_item_projection = (
        MAX_SAM_CALLS_PER_ITEM
        * meta_client.project_cut_out_cost_usd(server.CUT_OUT_MODEL, 1)
        + meta_client.project_draw_cost_usd(server.DRAW_BACKGROUND_MODEL)
    )
    worst_case = identify_projection + n_items_estimate * per_item_projection
    notes: list[str] = []
    if worst_case > budget_usd:
        raise RuntimeError(
            f"Room run refused before any call: worst-case projection "
            f"${worst_case:.4f} exceeds the ${budget_usd:.2f} run budget "
            f"({len(sources)} photos, ~{n_items_estimate} items). Reduce "
            "the item set or the photo pool, or raise --budget-usd."
        )
    notes.append(
        f"worst-case projection ${worst_case:.4f} within the ${budget_usd:.2f} run budget"
    )

    # -- plan pass ------------------------------------------------------------
    for source in sources:
        result = chain["identify"](_data_uri(source.path))
        if result.get("status") == "ok":
            source.plan = result
            source.handle = result.get("image")
        else:
            source.plan_error = result.get("error") or {
                "code": "IDENTIFY_FAILED",
                "message": str(result),
            }
            try:
                source.handle = ingest.ingest(_data_uri(source.path), store)
            except Exception as exc:  # unreadable photo: record, move on
                source.plan_error = {
                    "code": "INGEST_FAILED",
                    "message": f"{source.plan_error.get('message', '')} | ingest: {exc}",
                }

    # -- work list -------------------------------------------------------------
    items: list[dict] = []
    if auto:
        candidates = [
            Candidate(name=source.plan["concept"], photo_index=i, plan=source.plan)
            for i, source in enumerate(sources)
            if source.plan and source.plan.get("concept")
        ]
        for group in group_candidates(candidates):
            items.append(
                {
                    "name": group["name"],
                    "candidate_photo_indices": group["photo_indices"],
                    "flags": group["flags"],
                }
            )
        mode = "auto"
    else:
        mode = "named"
        for name in item_names or []:
            items.append(
                {
                    "name": name,
                    "candidate_photo_indices": _rank_photos(name, sources),
                    "flags": [],
                }
            )
    # Cap candidate probing per item (rank order preserved). The cap
    # is also the spend bound the projection gate priced.
    candidate_cap = min(max_photos_per_item, MAX_PHOTOS_PER_ITEM)
    for item in items:
        item["candidate_photo_indices"] = item["candidate_photo_indices"][
            :candidate_cap
        ]

    ctx = _RunContext(
        store=store,
        chain=chain,
        scene=scene,
        verify_fn=verify_fn,
        max_photos_per_item=max_photos_per_item,
    )

    records: list[dict] = []
    budget_stopped = False
    for item in items:
        if spend_probe() - spend_before > budget_usd:
            budget_stopped = True
        if budget_stopped:
            records.append(
                {
                    "name": item["name"],
                    "status": "failed",
                    "stage_reached": "none",
                    "failure": {
                        "stage": "budget",
                        "code": "BUDGET_STOP",
                        "message": (
                            f"run spend crossed the ${budget_usd:.2f} budget "
                            "before this item was attempted"
                        ),
                    },
                    "sightings": [],
                    "primary_photo_index": None,
                    "also_seen_in": [],
                    "output_asset": None,
                    "receipt": None,
                    "receipt_file": None,
                    "verification": None,
                    "listing": None,
                    "possible_duplicate_of": [],
                    "flags": list(item.get("flags", [])),
                    "cost_usd": 0.0,
                }
            )
            continue
        records.append(
            _process_item(ctx, item, sources, probe_all=probe_all_candidates)
        )

    _flag_cross_item_duplicates(records, sources)

    total_cost = round(spend_probe() - spend_before, 6)
    manifest = {
        "room_id": room_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "room_style": scene,
        "budget_usd": budget_usd,
        "ledger_path": str(room_ledger),
        "total_cost_usd": total_cost,
        "sources": [s.manifest_entry(i) for i, s in enumerate(sources)],
        "videos": video_notes,
        "items": records,
        "notes": notes,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    (out_dir / "summary.txt").write_text(summarize_manifest(manifest), encoding="utf-8")
    return manifest


def summarize_manifest(manifest: dict) -> str:
    """The readable half of the manifest."""
    lines = [
        f"ROOM RUN {manifest['room_id']} — {manifest['created_at']}",
        f"mode: {manifest['mode']} | sources: {len(manifest['sources'])} "
        f"| items: {len(manifest['items'])} | total cost: ${manifest['total_cost_usd']:.6f}",
        f"room style: {manifest['room_style']}",
        "",
    ]
    for entry in manifest["sources"]:
        origin = entry["origin"]
        if entry["timestamp_s"] is not None:
            origin += f" @ {entry['timestamp_s']}s"
        concept = (entry["plan"] or {}).get("concept") or "—"
        lines.append(f"  photo {entry['index']}: [{origin}] {entry['path']} — plan: {concept}")
    lines.append("")
    staged = 0
    for item in manifest["items"]:
        if item["status"] == "staged":
            staged += 1
            receipt = item["receipt"] or {}
            seen = f" (also seen in photos {item['also_seen_in']})" if item["also_seen_in"] else ""
            lines.append(
                f"  STAGED  {item['name']}: photo {item['primary_photo_index']}{seen} — "
                f"{receipt.get('opaque_pixels')} opaque px, "
                f"{receipt.get('changed_opaque_pixels')} changed, "
                f"profile preserved: {receipt.get('color_profile_preserved')} — "
                f"${item['cost_usd']:.6f}"
            )
            lines.append(f"          receipt: {item['receipt_file']}")
            verification = item.get("verification") or {}
            lines.append(
                f"          verified independently: "
                f"{verification.get('opaque_pixels')} opaque px recounted from disk, "
                f"{verification.get('changed_opaque_pixels')} changed"
            )
        elif item["status"] == "verify_failed":
            failure = item["failure"] or {}
            lines.append(
                f"  VERIFY FAILED  {item['name']}: the staged output did not "
                f"pass independent verification — {failure.get('message')}"
            )
            lines.append(f"          receipt: {item['receipt_file']}")
        else:
            failure = item["failure"] or {}
            lines.append(
                f"  FAILED  {item['name']}: stage {failure.get('stage')} "
                f"[{failure.get('code')}] {failure.get('message')}"
            )
        for flag in item.get("flags", []):
            lines.append(f"          flag: {flag}")
    lines.append("")
    lines.append(f"{staged} of {len(manifest['items'])} items staged.")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="room_run",
        description=(
            "ROOM RUN: stage every named item in a room's photos, one "
            "Fidelity Lock receipt per item, one manifest per room."
        ),
    )
    parser.add_argument(
        "--photos",
        nargs="+",
        default=[],
        help="Still photos (files or folders) of the room.",
    )
    parser.add_argument(
        "--video",
        action="append",
        default=[],
        help="Walkthrough video; frames are sampled into the photo pool. Repeatable.",
    )
    parser.add_argument(
        "--items",
        default="",
        help='Comma-separated item names as the seller would say them ("straw hat, orange suitcase").',
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Auto-enumerate mode: candidate items come from each photo's identify plan.",
    )
    parser.add_argument("--scene", default=DEFAULT_SCENE, help="Shared room plate style.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output dir for manifest.json + summary.txt + sampled frames (default: ./roomrun-<room_id>).",
    )
    parser.add_argument("--room-id", default=None)
    parser.add_argument("--budget-usd", type=float, default=1.00)
    parser.add_argument("--frame-every", type=float, default=2.0)
    parser.add_argument("--max-frames-per-video", type=int, default=8)
    parser.add_argument(
        "--first-success-only",
        action="store_true",
        help="Stop probing an item at its first clean cut (cheaper, thinner de-dup evidence).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Isolate the asset store here (AGENTHAND_DATA_DIR).",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help=(
            "The run's cost ledger (default: ledger.jsonl inside the "
            "output dir). Room runs are isolated from the server's "
            "cumulative ledger by default; point this at that ledger "
            "only if the run's spend should deliberately count there."
        ),
    )
    args = parser.parse_args(argv)

    if args.data_dir:
        os.environ["AGENTHAND_DATA_DIR"] = str(args.data_dir)

    item_names = [name.strip() for name in args.items.split(",") if name.strip()]
    if not args.auto and not item_names:
        parser.error("name the items with --items, or use --auto to enumerate")
    if not args.photos and not args.video:
        parser.error("give at least one --photos file/folder or --video")

    room_id = args.room_id or f"room-{uuid.uuid4().hex[:8]}"
    out_dir = args.out or Path.cwd() / f"roomrun-{room_id}"
    try:
        manifest = run_room(
            [Path(p) for p in args.photos],
            [Path(v) for v in args.video],
            out_dir,
            item_names=item_names or None,
            auto=args.auto,
            scene=args.scene,
            room_id=room_id,
            budget_usd=args.budget_usd,
            frame_every_seconds=args.frame_every,
            max_frames_per_video=args.max_frames_per_video,
            probe_all_candidates=not args.first_success_only,
            ledger_path=args.ledger,
        )
    except RuntimeError as exc:
        print(f"ROOM RUN REFUSED: {exc}", file=sys.stderr)
        return 2
    print(summarize_manifest(manifest), end="")
    print(f"manifest: {out_dir / 'manifest.json'}")
    staged = sum(1 for i in manifest["items"] if i["status"] == "staged")
    return 0 if staged == len(manifest["items"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
