"""Paper-fill reconstruction: generated paper only where the cut is empty.

Decision 93 (Daniel, 2026-10-29) is the spec: when a cut loses part of
an object — thin legs SAM missed, holes where objects sitting on the
product were excluded — fill ONLY the missing regions with the brand
wallpaper motif, shaped like the missing thing and placed from the
source photo's own evidence, so a viewer can tell at a glance what is
real (receipted, untouched) and what is reconstruction (obviously
generated). Paper never lands on a pixel the foreground occupies at
any alpha: the edge band blends with the plate exactly as it does
without paper, and a 1 px guard ring keeps paper off the silhouette
entirely.

The design follows the paper-fill Gauntlet (2026-10-29, 4 of 4 lanes
SHIP WITH CHANGES; synthesis in the hackathon workspace):

- Detection is deterministic code over the stored source and mask.
  No model calls happen here or anywhere in the draw path.
- Holes are topology: enclosed empty components of the foreground
  alpha (flood-fill complement), i.e. the footprints occluders left
  in the product's surface.
- Supports are OBSERVED silhouettes: dark, thin, elongated components
  in the source band below the product that touch it. Nothing is
  mirrored, transferred, or invented; a support the photo does not
  show is refused, and the refusal is recorded.
- Rendering samples the backdrop motif's own geometry (72 px,
  #00AFE1) at full strength, so paper can never be mistaken for the
  40% backdrop plate. Circles stay true in paper coordinates:
  supports use a measured width-profile cylindrical chart
  (U = r(v) * theta), flat regions use screen-space sampling.
  Shading is clamped to [SHADE_MIN, SHADE_MAX] and derived from a
  heavily blurred luminance envelope, so no printed detail from the
  source ghosts into the paper, and paper never reaches the plate's
  brightness (which would camouflage it) or black.
- The layer is stored as its own asset (KIND_RECONSTRUCTION) with a
  region map (KIND_REGION_MAP); the draw receipt discloses both,
  plus per-region provenance and every refusal. The verifier
  recounts the layer and rebuilds the delivered file from its three
  named layers.

All thresholds are pinned constants, not per-object tuning.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

from . import backdrop
from .assets import (
    HANDLE_PREFIX,
    KIND_MASK,
    KIND_RECONSTRUCTION,
    KIND_REGION_MAP,
    AssetStore,
)

RENDERER_VERSION = "agenthand.paperfill/1"

# The paper print: the backdrop motif's geometry at full strength.
PAPER_MOTIF_DIAMETER_PX = backdrop.WALLPAPER_MOTIF_DIAMETER_PX
PAPER_MOTIF_OPACITY = 1.0
PAPER_MOTIF_COLOR = "#00AFE1"

# Shading clamp: paper is always visibly paper (never as bright as
# the plate's ground, never crushed to black).
SHADE_MIN = 0.55
SHADE_MAX = 0.92

# Paper keeps this many pixels away from any foreground pixel, so the
# edge band's blend partner is provably the plate alone.
GUARD_RING_PX = 1

# Hole gates (fractions of the mask's own area).
MIN_HOLE_FRACTION = 0.0005
MAX_HOLE_FRACTION = 0.40

# Support gates, applied to dark components in the band below the
# product (see detect_regions).
SUPPORT_TOUCH_DILATE_PX = 3
SUPPORT_MIN_ASPECT = 4.0
SUPPORT_MAX_WIDTH_FRACTION = 0.06
SUPPORT_MAX_HEIGHT_FRACTION = 0.6
SUPPORT_MAX_COUNT = 6
SUPPORT_MIN_HEIGHT_PX = 12
# Receipt hygiene: support refusals below this size merge into one
# record per code with summed pixels, so a noisy photo cannot flood
# the receipt (floor grain on a real photo produces hundreds). The
# threshold is the larger of an absolute floor and a fraction of the
# mask area, so it scales with the product like the other gates.
REFUSAL_AGGREGATE_FLOOR_PX = 64
REFUSAL_AGGREGATE_FRACTION = 0.001
# A support candidate must be much darker than the typical
# under-product background: under half the band's median luma. A
# percentile threshold collapses when the dark thing is large (its
# own luma becomes the percentile), which would make big dark
# objects vanish from candidacy instead of being refused on record.
SUPPORT_DARK_MEDIAN_FRACTION = 0.5

# Region-map class codes (the region map asset is an L image).
REGION_CLASS_CODES = {"hole": 1, "support": 2, "occluder": 3}

# Motif sampling tile: the backdrop motif's lattice periods are
# pitch (112 px) horizontally and 2 * pitch vertically, so a tile of
# 2 * pitch in both axes sampled modulo reproduces the infinite
# tiling exactly.
_MOTIF_TILE_PX = 2 * round(PAPER_MOTIF_DIAMETER_PX * 1.55)


@dataclass
class Region:
    """One accepted reconstruction region, in source-frame pixels."""

    cls: str  # "hole" | "support" | "occluder"
    silhouette: np.ndarray  # bool, cropped to bbox
    provenance: dict = field(default_factory=dict)
    pixels: int = 0  # visible paper pixels, set by the renderer
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # half-open


@dataclass
class Refusal:
    code: str
    detail: str
    pixels: int = 0


def _bbox_of(silhouette: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.where(silhouette)
    if len(xs) == 0:
        return (0, 0, 0, 0)
    return (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)


def _components(binary: np.ndarray):
    """Yield (bbox, mask) per 8-connected component, mask cropped.

    Full-frame masks per component are not an option at 24 MP: a
    few dozen components would cost gigabytes. Cropped masks keep
    each component at its own size; Region silhouettes follow the
    same convention (silhouette is cropped to Region.bbox).
    """
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        binary.astype(np.uint8), connectivity=8
    )
    for i in range(1, count):
        x, y, w, h, _area = (int(v) for v in stats[i])
        yield (x, y, x + w, y + h), labels[y : y + h, x : x + w] == i


def detect_regions(
    source_rgb: np.ndarray, fg_alpha: np.ndarray
) -> tuple[list[Region], list[Refusal], int]:
    """Find the regions paper may fill: enclosed holes and supports.

    ``source_rgb`` is the decoded source photo (H, W, 3); ``fg_alpha``
    the foreground's alpha channel. Returns (regions, refusals,
    unfilled_gap_pixels). Everything not accepted stays empty and is
    accounted: either as a recorded refusal or in the unfilled count.
    """
    height, width = fg_alpha.shape
    product_any = fg_alpha > 0
    mask_area = int(np.count_nonzero(product_any))
    regions: list[Region] = []
    refusals: list[Refusal] = []
    unfilled = 0
    if mask_area == 0:
        return regions, refusals, unfilled

    # ---- Holes: empty components the border flood cannot reach. ----
    empty = ~product_any
    hole_candidates: list[tuple[tuple[int, int, int, int], np.ndarray]] = []
    too_small = 0
    for bbox, comp in _components(empty):
        touches_border = (
            bbox[0] == 0 or bbox[1] == 0 or bbox[2] == width or bbox[3] == height
        )
        if touches_border:
            continue
        area = int(np.count_nonzero(comp))
        if area < MIN_HOLE_FRACTION * mask_area:
            too_small += area
            continue
        hole_candidates.append((bbox, comp))
    if too_small:
        refusals.append(
            Refusal(
                "HOLE_TOO_SMALL",
                f"enclosed gaps under {MIN_HOLE_FRACTION} of the mask "
                "area are not papered",
                too_small,
            )
        )
        unfilled += too_small
    hole_total = sum(int(np.count_nonzero(c)) for _b, c in hole_candidates)
    if hole_total > MAX_HOLE_FRACTION * mask_area:
        refusals.append(
            Refusal(
                "HOLES_TOO_LARGE",
                f"enclosed gaps cover {hole_total} px, over "
                f"{MAX_HOLE_FRACTION} of the mask; the mask is wrong, "
                "not the product",
                hole_total,
            )
        )
        unfilled += hole_total
    else:
        for bbox, comp in hole_candidates:
            regions.append(
                Region(
                    cls="hole",
                    silhouette=comp,
                    provenance={"basis": "mask_topology"},
                    bbox=bbox,
                )
            )

    # ---- Supports: observed dark thin components below the body. ----
    bx0, by0, bx1, by1 = _bbox_of(product_any)
    body_w, body_h = bx1 - bx0, by1 - by0
    widen = int(0.05 * body_w)
    cx0, cx1 = max(0, bx0 - widen), min(width, bx1 + widen)
    # Per-column bottom of the product; the band is what hangs below
    # it in the same column. Columns with no product have no band, so
    # scenery beside the object (the fireplace in the hero photo) is
    # excluded by construction.
    band = np.zeros((height, width), bool)
    bottoms = np.full(width, -1, dtype=np.int64)
    for x in range(cx0, cx1):
        rows = np.where(product_any[:, x])[0]
        if len(rows):
            bottoms[x] = rows.max()
            band[bottoms[x] + 1 :, x] = True
    if band.any():
        luma = (
            0.2126 * source_rgb[:, :, 0].astype(np.float64)
            + 0.7152 * source_rgb[:, :, 1].astype(np.float64)
            + 0.0722 * source_rgb[:, :, 2].astype(np.float64)
        )
        band_luma = luma[band]
        dark_limit = SUPPORT_DARK_MEDIAN_FRACTION * float(np.median(band_luma))
        candidates = band & (luma < dark_limit)
        dilated_product = cv2.dilate(
            product_any.astype(np.uint8),
            np.ones((3, 3), np.uint8),
            iterations=SUPPORT_TOUCH_DILATE_PX,
        ) > 0
        accepted: list[Region] = []
        for cbox, comp in _components(candidates):
            area = int(np.count_nonzero(comp))
            touch_zone = dilated_product[cbox[1] : cbox[3], cbox[0] : cbox[2]]
            if not (comp & touch_zone).any():
                refusals.append(
                    Refusal(
                        "SUPPORT_NO_TOUCH",
                        "dark component below the product does not "
                        "touch it; not evidence of a support",
                        area,
                    )
                )
                unfilled += area
                continue
            comp_h = cbox[3] - cbox[1]
            mean_width = area / max(comp_h, 1)
            aspect = comp_h / max(mean_width, 1e-9)
            reason = None
            if comp_h < SUPPORT_MIN_HEIGHT_PX:
                reason = (
                    f"height {comp_h} px under {SUPPORT_MIN_HEIGHT_PX}; "
                    "too short to be a support"
                )
            elif aspect < SUPPORT_MIN_ASPECT:
                reason = (
                    f"aspect {aspect:.2f} under {SUPPORT_MIN_ASPECT}; "
                    "not a thin member"
                )
            elif mean_width > SUPPORT_MAX_WIDTH_FRACTION * body_w:
                reason = (
                    f"mean width {mean_width:.1f} px over "
                    f"{SUPPORT_MAX_WIDTH_FRACTION} of the body width; "
                    "too wide to be a support"
                )
            elif comp_h > SUPPORT_MAX_HEIGHT_FRACTION * body_h:
                reason = (
                    f"height {comp_h} px over "
                    f"{SUPPORT_MAX_HEIGHT_FRACTION} of the body height"
                )
            elif cbox[3] >= height:
                reason = "component reaches the frame bottom; its foot is not in the photo"
            if reason is not None:
                refusals.append(Refusal("SUPPORT_REJECTED", reason, area))
                unfilled += area
                continue
            accepted.append(
                Region(
                    cls="support",
                    silhouette=comp,
                    provenance={
                        "basis": "source_silhouette",
                        "mean_width_px": round(float(mean_width), 2),
                        "aspect": round(float(aspect), 2),
                    },
                    bbox=cbox,
                )
            )
        if len(accepted) > SUPPORT_MAX_COUNT:
            total = sum(int(np.count_nonzero(r.silhouette)) for r in accepted)
            refusals.append(
                Refusal(
                    "SUPPORT_COUNT_OUT_OF_RANGE",
                    f"{len(accepted)} support candidates passed the "
                    f"gates; the cap is {SUPPORT_MAX_COUNT}",
                    total,
                )
            )
            unfilled += total
        else:
            regions.extend(accepted)

    # Receipt hygiene: speck-sized support refusals merge into one
    # record per code with summed pixels, so the refusal list stays
    # readable and the accounting is unchanged.
    aggregate_under = max(
        REFUSAL_AGGREGATE_FLOOR_PX,
        int(REFUSAL_AGGREGATE_FRACTION * mask_area),
    )
    merged: list[Refusal] = []
    small: dict[str, list[Refusal]] = {}
    for refusal in refusals:
        if refusal.pixels < aggregate_under and refusal.code.startswith("SUPPORT"):
            small.setdefault(refusal.code, []).append(refusal)
        else:
            merged.append(refusal)
    for code, group in small.items():
        if len(group) == 1:
            merged.append(group[0])
        else:
            merged.append(
                Refusal(
                    code,
                    f"{len(group)} small components aggregated",
                    sum(r.pixels for r in group),
                )
            )
    refusals = merged

    return regions, refusals, unfilled


def _motif_tile() -> np.ndarray:
    """One periodic tile of the backdrop motif, RGBA float in [0, 1].

    The paper print is the backdrop's own arc geometry at full
    strength: white ground, cyan arcs. Sampled modulo the tile size
    it reproduces backdrop._motif_overlay's infinite tiling.
    """
    overlay = backdrop._motif_overlay(
        _MOTIF_TILE_PX, _MOTIF_TILE_PX, PAPER_MOTIF_DIAMETER_PX, 255
    )
    return np.asarray(overlay).astype(np.float64) / 255.0


def _paper_rgb(tile: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Motif colour at paper coordinates (u, v): white + cyan arcs."""
    xi = np.mod(np.rint(u).astype(np.int64), _MOTIF_TILE_PX)
    yi = np.mod(np.rint(v).astype(np.int64), _MOTIF_TILE_PX)
    sample = tile[yi, xi]  # (..., 4) RGBA in [0, 1]
    arc = sample[:, :, 3:4]
    cyan = np.array(backdrop.WALLPAPER_CYAN, dtype=np.float64) / 255.0
    return (1.0 - arc) * 1.0 + arc * cyan[None, None, :]


def _render_region(
    region: Region,
    source_rgb: np.ndarray,
    tile: np.ndarray,
    blurred_luma: np.ndarray | None = None,
) -> np.ndarray:
    """Render one region's paper as an RGB float array over its bbox.

    Shading is clamped to [SHADE_MIN, SHADE_MAX] in every path:
    supports take a Lambert term across their measured width profile;
    flat regions take a heavily blurred luminance envelope of the
    source, normalised per region so no printed detail survives.
    """
    x0, y0, x1, y1 = region.bbox
    local = region.silhouette
    h, w = local.shape
    ys, xs = np.mgrid[y0:y1, x0:x1].astype(np.float64)
    if region.cls == "support":
        coords = np.column_stack(np.where(region.silhouette)).astype(np.float64)
        coords[:, 0] += y0  # silhouette coords are bbox-local
        coords[:, 1] += x0
        centroid = coords.mean(axis=0)
        centered = coords - centroid
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        axis = vt[0]
        if axis[0] < 0:  # axis points down the member
            axis = -axis
        perp = np.array([-axis[1], axis[0]])
        rel_y = (ys - centroid[0]) * axis[0] + (xs - centroid[1]) * axis[1]
        rel_x = (ys - centroid[0]) * perp[0] + (xs - centroid[1]) * perp[1]
        # Measured width profile: half-width per axial bin.
        bins = np.rint(rel_y).astype(np.int64)
        half = np.zeros_like(rel_y)
        centre_off = np.zeros_like(rel_y)
        for b in np.unique(bins[local]):
            sel = local & (bins == b)
            if sel.any():
                lats = rel_x[sel]
                half[sel] = max((lats.max() - lats.min()) / 2.0, 0.5)
                centre_off[sel] = (lats.max() + lats.min()) / 2.0
        s = np.clip((rel_x - centre_off) / half, -1.0, 1.0)
        theta = np.arcsin(s)
        u = half * theta
        v = rel_y - rel_y[local].min()
        key = np.deg2rad(-35.0)
        shade = 0.55 + 0.45 * np.maximum(0.0, np.cos(theta - key))
        shade = np.clip(shade, SHADE_MIN, SHADE_MAX)
        provenance_axis = float(np.rad2deg(np.arctan2(axis[1], axis[0])))
        region.provenance.setdefault("axis_deg", round(provenance_axis, 2))
    else:
        u, v = xs, ys
        if blurred_luma is None:
            luma = (
                0.2126 * source_rgb[:, :, 0].astype(np.float64)
                + 0.7152 * source_rgb[:, :, 1].astype(np.float64)
                + 0.0722 * source_rgb[:, :, 2].astype(np.float64)
            ) / 255.0
            blurred_luma = cv2.GaussianBlur(
                luma, (0, 0), sigmaX=PAPER_MOTIF_DIAMETER_PX / 2
            )
        env = blurred_luma[y0:y1, x0:x1]
        vals = env[local]
        lo, hi = float(vals.min()), float(vals.max())
        if hi - lo < 1e-9:
            norm = np.full_like(env, 0.925)
        else:
            norm = 0.85 + 0.15 * (env - lo) / (hi - lo)
        shade = np.clip(norm, SHADE_MIN, SHADE_MAX)
    rgb = _paper_rgb(tile, u, v) * shade[:, :, None]
    out = np.zeros((h, w, 4), dtype=np.uint8)
    out[:, :, :3] = np.clip(np.rint(rgb * 255.0), 0, 255).astype(np.uint8)
    out[:, :, 3] = np.where(local, 255, 0)
    return out


def render_reconstruction(
    source_rgb: np.ndarray,
    fg_alpha: np.ndarray,
    regions: list[Region],
) -> tuple[Image.Image, Image.Image, int]:
    """Render the paper layer and region map for accepted regions.

    The clip is the last operation: paper alpha is zeroed wherever
    the foreground has any alpha, plus a GUARD_RING_PX ring around
    it, so the delivered edge band blends with the plate alone.
    Returns (layer RGBA, region map L, unfilled_pixels) where
    unfilled counts region pixels the clip removed.
    """
    height, width = fg_alpha.shape
    allowed = fg_alpha == 0
    if GUARD_RING_PX:
        near_product = cv2.dilate(
            (fg_alpha > 0).astype(np.uint8),
            np.ones((3, 3), np.uint8),
            iterations=GUARD_RING_PX,
        ) > 0
        allowed &= ~near_product
    tile = _motif_tile()
    # The flat-region light envelope is a full-frame blur; compute
    # it once for all flat regions, not once per region.
    blurred_luma = None
    if any(r.cls != "support" for r in regions):
        luma = (
            0.2126 * source_rgb[:, :, 0].astype(np.float64)
            + 0.7152 * source_rgb[:, :, 1].astype(np.float64)
            + 0.0722 * source_rgb[:, :, 2].astype(np.float64)
        ) / 255.0
        blurred_luma = cv2.GaussianBlur(
            luma, (0, 0), sigmaX=PAPER_MOTIF_DIAMETER_PX / 2
        )
    layer = np.zeros((height, width, 4), dtype=np.uint8)
    region_map = np.zeros((height, width), dtype=np.uint8)
    claimed = np.zeros((height, width), bool)
    unfilled = 0
    # Claim order: occluders, then supports, then holes — a pixel is
    # paper of exactly one class, so the region map is a partition.
    order = {"occluder": 0, "support": 1, "hole": 2}
    for region in sorted(regions, key=lambda r: order[r.cls]):
        rendered = _render_region(region, source_rgb, tile, blurred_luma)
        x0, y0, x1, y1 = region.bbox
        take = (
            (rendered[:, :, 3] > 0)
            & allowed[y0:y1, x0:x1]
            & ~claimed[y0:y1, x0:x1]
        )
        want = int(np.count_nonzero(rendered[:, :, 3] > 0))
        got = int(np.count_nonzero(take))
        unfilled += want - got
        if got == 0:
            region.pixels = 0
            continue
        block = layer[y0:y1, x0:x1]
        block[take] = rendered[take]
        layer[y0:y1, x0:x1] = block
        region_map[y0:y1, x0:x1][take] = REGION_CLASS_CODES[region.cls]
        claimed[y0:y1, x0:x1] |= take
        region.pixels = got
    assert not ((layer[:, :, 3] > 0) & (fg_alpha > 0)).any()
    return (
        Image.fromarray(layer, "RGBA"),
        Image.fromarray(region_map, "L"),
        unfilled,
    )


def build_reconstruction(
    store: AssetStore,
    foreground_handle: str,
    occluder_mask_handles: tuple[str, ...] = (),
) -> dict:
    """Detect, render, and store the paper layer for a foreground.

    Walks the foreground's lineage exactly as draw does (the same
    admission rule), reads the source photo and foreground from the
    store, and stores the layer (parented to the mask record, or to
    the foreground when the mask is its own alpha) and the region map
    (parented to the layer). Occluder masks, when supplied, must be
    mask assets chained to the same source; their silhouettes become
    occluder regions. No model calls; no billing.

    Returns the report draw stamps into the receipt: status
    ("applied" when any paper was laid, else "refused" when refusals
    were recorded, else "none"), the stored handles (None when no
    paper), per-region and refusal records, and counts.
    """
    from .composite import FidelityLockError, validate_foreground

    chain = validate_foreground(store, foreground_handle)
    foreground_record = chain[0]
    source_record = chain[-1]
    mask_record = next((r for r in chain if r.kind == KIND_MASK), None)

    foreground = store.read_image(foreground_handle)
    source = store.read_image(HANDLE_PREFIX + source_record.asset_id).convert("RGB")
    if source.size != foreground.size:
        raise FidelityLockError(
            "source and foreground frames differ; paper-fill works on "
            "the product's own frame only"
        )
    source_rgb = np.asarray(source)
    fg_alpha = np.asarray(foreground.getchannel("A"))

    regions, refusals, unfilled = detect_regions(source_rgb, fg_alpha)

    for handle in occluder_mask_handles:
        record = store.get_record(handle)
        if record.kind != KIND_MASK:
            raise FidelityLockError(
                f"occluder {handle!r} is kind {record.kind!r}, not {KIND_MASK}"
            )
        if record.source_asset_id != source_record.asset_id:
            raise FidelityLockError(
                f"occluder {handle!r} is not chained to this foreground's source"
            )
        store.verify_integrity(handle)
        occ = np.asarray(store.read_image(handle).convert("L")) > 127
        silhouette = occ & (fg_alpha == 0)
        if silhouette.any():
            obox = _bbox_of(silhouette)
            regions.append(
                Region(
                    cls="occluder",
                    silhouette=silhouette[obox[1] : obox[3], obox[0] : obox[2]],
                    provenance={
                        "basis": "occluder_mask",
                        "occluder_mask_asset": handle,
                    },
                    bbox=obox,
                )
            )

    layer_image, region_map_image, clip_unfilled = render_reconstruction(
        source_rgb, fg_alpha, regions
    )
    unfilled += clip_unfilled
    live_regions = [r for r in regions if r.pixels > 0]
    total_pixels = sum(r.pixels for r in live_regions)

    report: dict = {
        "status": "none",
        "renderer": RENDERER_VERSION,
        "layer_handle": None,
        "region_map_handle": None,
        "guard_ring_px": GUARD_RING_PX,
        "motif": {
            "diameter_px": PAPER_MOTIF_DIAMETER_PX,
            "opacity": PAPER_MOTIF_OPACITY,
            "color": PAPER_MOTIF_COLOR,
        },
        "regions": [
            {
                "id": i + 1,
                "class": r.cls,
                "pixels": r.pixels,
                "bbox": list(r.bbox),
                "provenance": r.provenance,
            }
            for i, r in enumerate(live_regions)
        ],
        "refusals": [
            {"code": r.code, "detail": r.detail, "pixels": r.pixels}
            for r in refusals
        ],
        "reconstruction_pixels": total_pixels,
        "unfilled_gap_pixels": unfilled,
    }
    if total_pixels == 0:
        report["status"] = "refused" if refusals else "none"
        return report

    parent_id = (
        mask_record.asset_id if mask_record is not None else foreground_record.asset_id
    )
    layer_handle = store.put_image(
        layer_image, kind=KIND_RECONSTRUCTION, source_asset_id=parent_id
    )
    layer_record = store.get_record(layer_handle)
    region_map_handle = store.put_image(
        region_map_image, kind=KIND_REGION_MAP, source_asset_id=layer_record.asset_id
    )
    report["status"] = "applied"
    report["layer_handle"] = layer_handle
    report["region_map_handle"] = region_map_handle
    return report
