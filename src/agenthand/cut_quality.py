"""Fail-closed stageability policy, separate from pixel-preservation proof.

Version 1 uses fixed conservative topology/source thresholds. It is a bounded
heuristic, not a claim of semantic recognition or corpus-wide calibration.
"""

from __future__ import annotations
import numpy as np
from PIL import Image
from . import mask_evidence as evidence

VERSION = "agenthand.cut-quality/1"
INSTRUCTIONS = {
    "SUPPORT_NOT_CAPTURED": "Move bins and clutter away from under the product. Photograph from a lower angle with every leg and foot visible against unobstructed, contrasting floor.",
    "TOP_OCCLUDED": "Remove everything resting on the product. Photograph its complete top and edges without objects overlapping them.",
    "TOP_PERFORATED": "Clear the top and photograph from an angle that shows the entire top surface and its edges without gaps or overlapping clutter.",
    "MASK_FRAGMENTED": "Place one complete product against a plain contrasting background, leave space around every edge, and keep the whole product in frame.",
}


def grade_cut(
    source: Image.Image | np.ndarray | None,
    mask: Image.Image | np.ndarray,
    *,
    occluders: list[evidence.TypedMask] | None = None,
) -> dict:
    alpha = np.asarray(mask.convert("L") if isinstance(mask, Image.Image) else mask)
    if alpha.ndim != 2:
        raise ValueError("cut mask must be a single-channel source-frame raster")
    metrics, body, gaps = evidence.geometry(alpha)
    area = metrics["mask_pixels"]
    codes: list[str] = []
    fatal = False
    if (
        not area
        or metrics["coverage"] >= 0.98
        or metrics["coverage"] < 0.001
        or metrics["dominant_fraction"] < 0.75
    ):
        codes.append("MASK_FRAGMENTED")
        fatal = True
    if metrics["top_island_pixels"] > max(4, 0.001 * area):
        codes.append("TOP_OCCLUDED")
        fatal = True
    footprints = evidence.top_footprints(alpha, occluders or []) if occluders else []
    proven_top = sum(int(r.silhouette.sum()) for r in footprints)
    metrics["proven_top_footprint_pixels"] = proven_top
    if metrics["top_concavity_pixels"] > max(12, 0.005 * area):
        codes.append("TOP_OCCLUDED")
        fatal |= metrics["top_concavity_pixels"] - proven_top > max(12, 0.005 * area)
    if metrics["enclosed_hole_pixels"] > max(4, 0.0005 * area):
        codes.append("TOP_PERFORATED")
        fatal |= metrics["enclosed_hole_pixels"] > 0.4 * area
    metrics["observed_support_pixels"] = 0
    metrics["uncaptured_support_pixels"] = 0
    metrics["ambiguous_support_pixels"] = 0
    if source is not None:
        rgb = np.asarray(
            source.convert("RGB") if isinstance(source, Image.Image) else source
        )
        if rgb.shape != (*alpha.shape, 3):
            raise ValueError("source and cut mask frames must match")
        regions, refusals, _ = evidence.detect_regions(rgb, alpha)
        supports = sum(int(r.silhouette.sum()) for r in regions if r.cls == "support")
        ambiguous = 0
        # A bounded dark mass can hide a support. Broad, border-connected
        # background alone is not evidence that a product has legs: applying
        # that assumption to screens/flat objects is a false reshoot.
        if area:
            x0, y0, x1, y1 = metrics["body_bbox"]
            if y1 < alpha.shape[0]:
                luma = rgb.mean(axis=2)
                band = luma[y1:, x0:x1]
                dark = band < 0.5 * np.percentile(band, 90)
                for b, c in evidence._components(dark):
                    height = b[3] - b[1]
                    mean_width = int(c.sum()) / max(1, height)
                    if b[1] > 3 or height < evidence.SUPPORT_MIN_HEIGHT_PX:
                        continue
                    thin = (
                        height / max(1, mean_width) >= evidence.SUPPORT_MIN_ASPECT
                        and mean_width
                        <= evidence.SUPPORT_MAX_WIDTH_FRACTION * (x1 - x0)
                    )
                    bounded = b[3] < band.shape[0]
                    if (not thin and bounded) or (
                        thin
                        and (
                            not bounded
                            or height > evidence.SUPPORT_MAX_HEIGHT_FRACTION * (y1 - y0)
                        )
                    ):
                        ambiguous += int(c.sum())
        metrics["observed_support_pixels"] = supports
        metrics["uncaptured_support_pixels"] = supports
        metrics["ambiguous_support_pixels"] = ambiguous
        if supports > max(4, 0.0005 * area) or ambiguous > max(64, 0.01 * area):
            codes.append("SUPPORT_NOT_CAPTURED")
            fatal |= ambiguous > max(64, 0.01 * area)
    codes = list(dict.fromkeys(codes))
    return {
        "version": VERSION,
        "grade": "RESHOOT" if fatal else "REPAIRABLE" if codes else "PASS",
        "codes": codes,
        "instructions": [INSTRUCTIONS[c] for c in codes],
        "source_evidence": source is not None,
        "metrics": metrics,
    }
