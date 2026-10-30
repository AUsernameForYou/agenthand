"""Fail-closed stageability policy, separate from pixel-preservation proof.

Version 1 uses fixed conservative topology/source thresholds. It is a bounded
heuristic, not a claim of semantic recognition or corpus-wide calibration.
"""
from __future__ import annotations
import numpy as np
from PIL import Image
from . import mask_evidence as evidence

VERSION = 'agenthand.cut-quality/1'
INSTRUCTIONS = {
    'SUPPORT_NOT_CAPTURED': 'Move bins and clutter away from under the product. Photograph from a lower angle with every leg and foot visible against unobstructed, contrasting floor.',
    'TOP_OCCLUDED': 'Remove everything resting on the product. Photograph its complete top and edges without objects overlapping them.',
    'TOP_PERFORATED': 'Clear the top and photograph from an angle that shows the entire top surface and its edges without gaps or overlapping clutter.',
    'MASK_FRAGMENTED': 'Place one complete product against a plain contrasting background, leave space around every edge, and keep the whole product in frame.',
}

def grade_cut(source: Image.Image | np.ndarray | None, mask: Image.Image | np.ndarray) -> dict:
    alpha = np.asarray(mask.convert('L') if isinstance(mask, Image.Image) else mask)
    if alpha.ndim != 2:
        raise ValueError('cut mask must be a single-channel source-frame raster')
    metrics, body, gaps = evidence.geometry(alpha)
    area = metrics['mask_pixels']
    codes: list[str] = []
    fatal = False
    if not area or metrics['coverage'] >= .98 or metrics['coverage'] < .001 or metrics['dominant_fraction'] < .75:
        codes.append('MASK_FRAGMENTED'); fatal = True
    if metrics['top_island_pixels'] > max(4, .001*area):
        codes.append('TOP_OCCLUDED'); fatal = True
    if metrics['top_concavity_pixels'] > max(12, .005*area):
        codes.append('TOP_OCCLUDED'); fatal = True
    if metrics['enclosed_hole_pixels'] > max(4, .0005*area):
        codes.append('TOP_PERFORATED')
        fatal |= metrics['enclosed_hole_pixels'] > .4*area
    metrics['observed_support_pixels'] = 0
    metrics['uncaptured_support_pixels'] = 0
    metrics['ambiguous_support_pixels'] = 0
    if source is not None:
        rgb = np.asarray(source.convert('RGB') if isinstance(source, Image.Image) else source)
        if rgb.shape != (*alpha.shape, 3):
            raise ValueError('source and cut mask frames must match')
        regions, refusals, _ = evidence.detect_regions(rgb, alpha)
        supports = sum(int(r.silhouette.sum()) for r in regions if r.cls == 'support')
        ambiguous = sum(r.pixels for r in refusals if r.code == 'SUPPORT_REJECTED')
        # A broad dark shadow can dominate the median and vanish from the
        # legacy renderer's detector. A bright-floor contrast control catches
        # it for refusal only; it does not authorize paper over that shadow.
        if not supports and area:
            x0,y0,x1,y1 = metrics['body_bbox']
            if y1 < alpha.shape[0]:
                luma = rgb.mean(axis=2)
                band = luma[y1:,x0:x1]
                dark = band < .5 * np.percentile(band,90)
                for b,c in evidence._components(dark):
                    if b[1] <= 3 and b[3]-b[1] >= 12 and c.sum()/max(1,b[3]-b[1]) > .06*(x1-x0):
                        ambiguous = max(ambiguous,int(c.sum()))
        metrics['observed_support_pixels'] = supports
        metrics['uncaptured_support_pixels'] = supports
        metrics['ambiguous_support_pixels'] = ambiguous
        if supports > max(4, .0005*area) or ambiguous > max(64, .01*area):
            codes.append('SUPPORT_NOT_CAPTURED')
            fatal |= ambiguous > max(64, .01*area)
    codes = list(dict.fromkeys(codes))
    return {'version': VERSION, 'grade': 'RESHOOT' if fatal else 'REPAIRABLE' if codes else 'PASS',
            'codes': codes, 'instructions': [INSTRUCTIONS[c] for c in codes],
            'source_evidence': source is not None, 'metrics': metrics}
