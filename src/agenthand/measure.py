"""Prototype dimension estimator: one flat object, same plane as the card.

EXPERIMENT ONLY. This is not a fourth MCP tool and is not wired into the
server. It exists to enable the demo's tape-measure beat: Daniel photographs
a flat object lying in the same plane as a printed agenthand card, and this
module estimates the object's height and width.

Method (classical CV, no learned models):

1. Seeds. Several independent projective seeds are generated and each
   must prove itself against the ticks: the sheet's own quad (its
   boundary is a closed curve in the adaptive ink mask, validated by
   projecting the profile's ring centre onto the detected cyan-ring
   centroid); a ring-anchored similarity grid search (rotation x
   scale, then coordinate ascent, then a projective vertex search);
   and, as a fallback, quads built from Hough line pairs bracketing
   the ring.
2. Verification. Under a seed, each of the 8 bracket arms must show
   its 3 white tick cuts where the profile says they must be (bright
   runs along the arm, gated to their expected path parameter).
3. Homography. Tick correspondences are made effectively 1-D: the
   along-path coordinate comes from the bright-run centroid, the
   lateral coordinate from the arm's measured ink-band centreline.
   The four frame corners (intersections of the fitted edge lines)
   enter the fit weighted x6 — border points alone leave a
   homography's far field ill-conditioned. RANSAC rejects outliers;
   observation and fitting iterate twice. Gates refuse sparse
   coverage or a reprojection RMS above 4 px rather than report a
   wild number.
4. Extent. The object's extent (caller-supplied bbox/mask, or the
   prototype's colour-segmentation fallback) is mapped through the
   inverse homography into the card plane, where extents are true
   millimetres.

The range is propagated from the detection residual: the RMS (in px)
becomes a positional sigma in mm at the object's location via the
homography Jacobian, is amplified by (1 + lever)^2 — projective error
grows quadratically with normalized distance from the fiducial, as
the synthetic scenes confirm — and is reported at ~2 sigma plus a 1%
floor. The output always carries the range and a tilde display
string — a bare number is never a valid output of this module.

One more gate sits between the fit and the estimate: the card's
width in image pixels. The card-size battery
(tests/test_measure_card_sizes.py) found a silent lying band — at
117-126 px across, the tick fit still looked healthy (21/24, RMS
~0.25 px) and the module quoted ~+/-6%, but the true size landed
outside the range: below ~130 px the tick cuts are under ~1.3 px
wide and systematic error (warp resampling, quantisation) outgrows
what the RMS-derived sigma models, while the fit reports no sign of
it. So below MIN_CARD_PX the module refuses outright, and through
the caution band up to COMFORT_CARD_PX it widens the range by a
systematic term fitted to the battery's centre errors and says so
in the warnings. A quoted range that can miss is not an acceptable
output of this module.

Limits: same plane only, flat objects only, the card must be fully
visible and unoccluded, and the estimate is only as honest as the
synthetic tests in tests/test_measure.py — no photo-real validation
exists yet.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

DEFAULT_PROFILE_PATH = (
    Path.home()
    / "workspace"
    / "meta-hackathon"
    / "scale-card"
    / "print"
    / "layout-profile.json"
)

METHOD = "card-homography v0 (seeded tick/corner homography, prototype)"

# Pixels-across gate (see the module docstring and the card-size
# battery in tests/test_measure_card_sizes.py): containment held at
# 134 px across the card and broke at 117-126 px, where the centre
# error reached 5-6% while the quoted range said ~6% on the wrong
# dimension — the misses were near, quiet, and invisible to the fit
# statistics. MIN_CARD_PX sits inside the empirical break bracket
# (126 missed / 134 contained). Through the caution band the range
# gains a systematic term that peaks at _SMALL_CARD_SYS_MAX at the
# floor and decays quadratically to zero at COMFORT_CARD_PX; the
# battery's worst centre error inside the band is 2.2% (134 px),
# well under the widened range.
MIN_CARD_PX = 130.0
COMFORT_CARD_PX = 170.0
_SMALL_CARD_SYS_MAX = 0.055

# Arm name -> (vertex key, direction in card-mm space). Ticks in the
# profile are ordered by fraction_from_vertex ascending, which is the
# start->end order along every arm under these directions.
_ARM_GEOMETRY = {
    "TL-H": ("TL", (1.0, 0.0)),
    "TL-V": ("TL", (0.0, 1.0)),
    "TR-H": ("TR", (-1.0, 0.0)),
    "TR-V": ("TR", (0.0, 1.0)),
    "BL-V": ("BL", (0.0, -1.0)),
    "BL-H": ("BL", (1.0, 0.0)),
    "BR-V": ("BR", (0.0, -1.0)),
    "BR-H": ("BR", (-1.0, 0.0)),
}


class MeasureError(RuntimeError):
    """Raised when the card or the object cannot be measured honestly."""


# --------------------------------------------------------------------------
# Profile


def load_card_profile(profile_path: Path, card_id: str) -> dict:
    data = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    cards = data.get("cards", {})
    if card_id not in cards:
        raise MeasureError(
            f"Card {card_id!r} not in profile {profile_path} "
            f"(has: {', '.join(sorted(cards)) or 'none'})"
        )
    return cards[card_id]


def _frame_mm(card: dict) -> dict[str, np.ndarray]:
    verts = card["bracket_frame"]["corner_vertices_centreline_mm"]
    return {k: np.array(v, dtype=np.float64) for k, v in verts.items()}


def _arms(card: dict) -> list[dict]:
    """Bracket arms with their start vertex, direction, and tick list."""
    frame = _frame_mm(card)
    arm_len = float(card["bracket_frame"]["arm_centreline_length_mm"])
    by_arm: dict[str, list[dict]] = {}
    for tick in card["ticks"]:
        by_arm.setdefault(tick["arm"], []).append(tick)
    arms = []
    for name, ticks in sorted(by_arm.items()):
        vertex_key, direction = _ARM_GEOMETRY[name]
        start = frame[vertex_key]
        dirv = np.array(direction, dtype=np.float64)
        arms.append(
            {
                "name": name,
                "start_mm": start,
                "end_mm": start + dirv * arm_len,
                "ticks_mm": [
                    np.array(t["centre_mm"], dtype=np.float64)
                    for t in sorted(ticks, key=lambda t: t["fraction_from_vertex"])
                ],
            }
        )
    return arms


# --------------------------------------------------------------------------
# Sampling helpers


def _project(H: np.ndarray, points: np.ndarray) -> np.ndarray:
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


def _sample_along(gray: np.ndarray, p: np.ndarray, q: np.ndarray, n: int = 240):
    xs = np.linspace(p[0], q[0], n)
    ys = np.linspace(p[1], q[1], n)
    values = np.array(
        [
            cv2.getRectSubPix(gray, (1, 1), (float(x), float(y)))[0, 0]
            for x, y in zip(xs, ys)
        ],
        dtype=np.float64,
    )
    return values, np.stack([xs, ys], axis=1)


def _bright_runs(values: np.ndarray) -> list[tuple[int, int]]:
    low, high = np.percentile(values, 10), np.percentile(values, 90)
    threshold = (low + high) / 2
    above = values > threshold
    runs = []
    start = None
    for i, flag in enumerate(above):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(values) - 1))
    return runs


def _arm_tick_centres(
    gray: np.ndarray,
    start_img: np.ndarray,
    end_img: np.ndarray,
    expected_img: list[np.ndarray],
    tol: float = 0.10,
    max_run_frac: float = 0.12,
) -> list[np.ndarray] | None:
    """Bright tick-cut centres along one arm, matched to expectations.

    ``expected_img`` are the tick positions projected under the current
    transform. A bright run counts only if its centre's path parameter
    lands within tolerance of an expected tick's parameter — this
    rejects the bright paper beyond the arm's end and any off-by-one
    labelling, both of which otherwise poison the homography fit.
    """
    values, points = _sample_along(gray, start_img, end_img)
    kernel = np.ones(5) / 5
    smooth = np.convolve(values, kernel, mode="same")
    runs = _bright_runs(smooth)
    low = float(np.percentile(smooth, 10))
    n = len(values)
    plausible = [
        (a, b)
        for a, b in runs
        if 2 <= (b - a + 1) <= max(6, int(max_run_frac * n))
    ]
    axis = end_img - start_img
    denom = float(axis @ axis)
    if denom <= 0:
        return None

    def param(pt: np.ndarray) -> float:
        return float((pt - start_img) @ axis) / denom

    run_centres = []
    for a, b in plausible:
        weights = np.clip(smooth[a : b + 1] - low, 0, None)
        if weights.sum() <= 0:
            continue
        centre = (points[a : b + 1] * weights[:, None]).sum(axis=0) / weights.sum()
        run_centres.append((param(centre), centre))
    centres: list[np.ndarray] = []
    used: set[int] = set()
    for expected_pt in expected_img:
        target = param(expected_pt)
        best_i, best_d = None, tol
        for i, (p, centre) in enumerate(run_centres):
            if i in used:
                continue
            if abs(p - target) < best_d:
                best_i, best_d = i, abs(p - target)
        if best_i is None:
            return None
        used.add(best_i)
        centres.append(run_centres[best_i][1])
    return centres


def _arm_centreline_points(
    mask: np.ndarray,
    start_img: np.ndarray,
    end_img: np.ndarray,
    half_width: float,
) -> np.ndarray | None:
    """Centreline points of one arm, measured from the ink mask.

    At each path parameter the ink band's lateral centroid is one
    centreline point; tick cuts register as no-band parameters and
    are skipped. These points average hundreds of mask pixels, so
    they are far more precise than any single sampled feature.
    """
    axis = end_img - start_img
    length = float(np.linalg.norm(axis))
    if length <= 0:
        return None
    direction = axis / length
    normal = np.array([-direction[1], direction[0]])
    points = []
    for t in np.linspace(0.08, 0.92, 22):
        base = start_img + t * axis
        ts = np.linspace(-half_width, half_width, 33)
        pts = base[None, :] + ts[:, None] * normal[None, :]
        values = np.array(
            [
                cv2.getRectSubPix(mask, (1, 1), (float(x), float(y)))[0, 0]
                for x, y in pts
            ],
            dtype=np.float64,
        )
        if values.sum() < 0.25 * 255 * len(ts) * 0.3:
            continue
        offset = float((ts * values).sum() / values.sum())
        points.append(base + offset * normal)
    if len(points) < 6:
        return None
    return np.array(points)


def _fit_line(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    centroid = points.mean(axis=0)
    _, _, vt = np.linalg.svd(points - centroid, full_matrices=False)
    return centroid, vt[0]


def _line_intersection(
    p1: np.ndarray, d1: np.ndarray, p2: np.ndarray, d2: np.ndarray
) -> np.ndarray | None:
    A = np.stack([d1, -d2], axis=1)
    if abs(np.linalg.det(A)) < 1e-9:
        return None
    t = np.linalg.solve(A, p2 - p1)
    return p1 + t[0] * d1


_EDGE_ARMS = {
    "top": ("TL-H", "TR-H"),
    "bottom": ("BL-H", "BR-H"),
    "left": ("TL-V", "BL-V"),
    "right": ("TR-V", "BR-V"),
}


def _tick_matches(
    gray: np.ndarray,
    mask: np.ndarray,
    card: dict,
    H: np.ndarray,
    strict: bool = True,
    with_lines: bool = False,
) -> tuple[list, int, list, dict]:
    """(tick pairs, arms hit, corner pairs, edge lines) under H.

    Edge lines (the frame's four sides, fitted from merged arm
    centreline points) are only measured when ``with_lines`` — they
    cost real sampling and the similarity search does not need them.

    ``strict`` gates runs tightly to their expected path parameter —
    right for scoring a fitted homography. The similarity search uses
    loose gating (perspective error alone can exceed the strict gate);
    RANSAC in the fit guards against the looser matches.
    """
    tol, max_run_frac = (0.11, 0.12) if strict else (0.20, 0.30)
    matched: list[tuple[np.ndarray, np.ndarray]] = []
    arms_hit = 0
    arm_points: dict[str, np.ndarray] = {}
    arm_lines: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    stroke_mm = float(card["bracket_frame"]["stroke_width_mm"])
    arm_len_mm = float(card["bracket_frame"]["arm_centreline_length_mm"])
    pending: list[tuple[dict, list[np.ndarray]]] = []
    for arm in _arms(card):
        start_img = _project(H, arm["start_mm"][None])[0]
        end_img = _project(H, arm["end_mm"][None])[0]
        direction = end_img - start_img
        arm_len_img = float(np.linalg.norm(direction))
        half_width = 0.75 * stroke_mm * (arm_len_img / arm_len_mm)
        if with_lines:
            pts = _arm_centreline_points(mask, start_img, end_img, half_width)
            if pts is not None:
                arm_points[arm["name"]] = pts
                arm_lines[arm["name"]] = _fit_line(pts)
        mask_values, _ = _sample_along(mask, start_img, end_img, n=120)
        if float((mask_values > 0).mean()) < 0.55:
            continue
        expected_img = [
            _project(H, t[None])[0] for t in arm["ticks_mm"]
        ]
        centres = _arm_tick_centres(
            gray, start_img, end_img, expected_img,
            tol=tol, max_run_frac=max_run_frac,
        )
        if centres is None:
            continue
        arms_hit += 1
        pending.append((arm, centres))
    for arm, centres in pending:
        line = arm_lines.get(arm["name"])
        for mm_pt, img_pt in zip(arm["ticks_mm"], centres):
            if line is not None:
                # Keep the accurate along-path coordinate; take the
                # lateral coordinate from the measured centreline.
                line_pt, line_dir = line
                along = float((img_pt - line_pt) @ line_dir)
                img_pt = line_pt + along * line_dir
            matched.append((mm_pt, img_pt))
    # Frame edges from merged arm centreline points; corners from
    # intersecting them. Tick points alone (interior to the edges,
    # sometimes clustered) leave the homography's perspective terms
    # ill-conditioned; the edges and corners pin the configuration.
    frame = _frame_mm(card)
    edges: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    corners: list[tuple[np.ndarray, np.ndarray]] = []
    if with_lines:
        for edge, arm_names in _EDGE_ARMS.items():
            pts = [arm_points[a] for a in arm_names if a in arm_points]
            if pts:
                merged = np.concatenate(pts, axis=0)
                if len(merged) >= 8:
                    edges[edge] = _fit_line(merged)
        for vertex, (e_a, e_b) in {
            "TL": ("top", "left"),
            "TR": ("top", "right"),
            "BL": ("bottom", "left"),
            "BR": ("bottom", "right"),
        }.items():
            if e_a in edges and e_b in edges:
                corner = _line_intersection(
                    edges[e_a][0], edges[e_a][1], edges[e_b][0], edges[e_b][1]
                )
                if corner is not None:
                    corners.append((frame[vertex], corner))
    return matched, arms_hit, corners, edges


# --------------------------------------------------------------------------
# Card detection: ring anchor + similarity search + tick homography


def _ink_mask(gray: np.ndarray) -> np.ndarray:
    blurred = cv2.medianBlur(gray, 3)
    return cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        51,
        12,
    )


def _ring_centroid(image_bgr: np.ndarray) -> tuple[np.ndarray, float] | None:
    """(centroid, pixel area) of the lockup's cyan ring, or None."""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    cyan = cv2.inRange(hsv, (88, 70, 70), (104, 255, 255))
    moments = cv2.moments(cyan)
    if moments["m00"] < 200:
        return None
    centroid = np.array(
        [moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]]
    )
    # m00 sums pixel VALUES (255 per cyan pixel), not the pixel count.
    return centroid, float(moments["m00"]) / 255.0


def _sheet_seed(
    mask: np.ndarray, card: dict, ring_centroid: np.ndarray
) -> np.ndarray | None:
    """Homography seed from the card sheet's own quad, or None.

    The adaptive ink mask fires along the paper/surface step, so
    the sheet's boundary is a thin closed curve in the mask — local
    by construction, hence immune to lighting gradients that defeat
    absolute brightness thresholds. The longest mask contours are
    tried as quads; a candidate is accepted only if the profile's
    ring centre projects onto the detected ring centroid (the
    tabletop boundary and the object's outline also produce long
    contours, but they put the ring in the wrong place).
    """
    contours, _ = cv2.findContours(
        mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    contours = sorted(
        contours, key=lambda c: cv2.arcLength(c, True), reverse=True
    )[:6]
    w = float(card["card_width_mm"])
    h = float(card["card_height_mm"])
    sheet_mm = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
    ring_mm = np.array(card["ring"]["centre_mm"], dtype=np.float64)
    for contour in contours:
        if cv2.arcLength(contour, True) < 400:
            continue
        approx = None
        for eps_factor in (0.01, 0.02, 0.03):
            candidate = cv2.approxPolyDP(
                contour, eps_factor * cv2.arcLength(contour, True), True
            )
            if len(candidate) == 4:
                approx = candidate
                break
        if approx is None:
            continue
        pts = approx.reshape(-1, 2).astype(np.float64)
        sums = pts.sum(axis=1)
        diffs = pts[:, 0] - pts[:, 1]
        ordered = np.array(
            [
                pts[np.argmin(sums)],   # TL
                pts[np.argmax(diffs)],  # TR
                pts[np.argmax(sums)],   # BR
                pts[np.argmin(diffs)],  # BL
            ]
        )
        H, _ = cv2.findHomography(sheet_mm, ordered, 0)
        if H is None:
            continue
        projected_ring = _project(H.astype(np.float64), ring_mm[None])[0]
        if np.linalg.norm(projected_ring - ring_centroid) <= 30:
            return H.astype(np.float64)
    return None


def _hough_quad_seed(
    gray: np.ndarray,
    mask: np.ndarray,
    card: dict,
    ring_centroid: np.ndarray,
    scale0: float,
) -> np.ndarray | None:
    """Projective seed from Hough line quads, or None.

    Fallback for scenes where both the similarity search and the
    sheet-contour seed fail (strong perspective + degradation). The
    sheet's sides are among the longest straight edges present;
    candidate quads are built from line pairs bracketing the ring,
    filtered by the ring-projection check, and the survivor with
    the best loose tick score wins. Decoys are harmless: a wrong
    rectangle puts the ring in the wrong place or scores no ticks.
    """
    edges = cv2.Canny(mask, 50, 150)
    segments = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=80, minLineLength=110, maxLineGap=20
    )
    if segments is None:
        return None
    segs = []
    for x1, y1, x2, y2 in segments.reshape(-1, 4):
        p = np.array([x1, y1], dtype=np.float64)
        q = np.array([x2, y2], dtype=np.float64)
        length = float(np.linalg.norm(q - p))
        if length < 110:
            continue
        direction = (q - p) / length
        if direction[0] < 0:
            direction = -direction
        normal = np.array([-direction[1], direction[0]])
        rho = float(normal @ p)
        segs.append((length, rho, normal, direction))
    if len(segs) < 4:
        return None
    segs.sort(key=lambda s: -s[0])
    segs = segs[:28]
    # Two orientation families by normal angle.
    ref = math.atan2(segs[0][2][1], segs[0][2][0])
    fam_a, fam_b = [], []
    for s in segs:
        ang = math.atan2(s[2][1], s[2][0])
        if abs(math.sin(ref - ang)) < 0.35:
            fam_a.append(s)
        else:
            fam_b.append(s)
    if len(fam_a) < 2 or len(fam_b) < 2:
        return None

    w = float(card["card_width_mm"])
    h = float(card["card_height_mm"])
    sheet_mm = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
    ring_mm = np.array(card["ring"]["centre_mm"], dtype=np.float64)

    def pairs_bracketing(fam):
        # Expected separation: the sheet dimension along the
        # family's normal direction.
        n_mean = np.mean([s[2] for s in fam], axis=0)
        expected_px = (h if abs(n_mean[1]) > abs(n_mean[0]) else w) * scale0
        out = []
        for i in range(len(fam)):
            for j in range(i + 1, len(fam)):
                r1, r2 = fam[i][1], fam[j][1]
                n = fam[i][2]
                centre_side = float(n @ ring_centroid)
                lo, hi = min(r1, r2), max(r1, r2)
                if lo < centre_side < hi and 0.45 * expected_px < hi - lo < 2.2 * expected_px:
                    out.append((fam[i], fam[j]))
        return out

    quads = []
    for a1, a2 in pairs_bracketing(fam_a):
        for b1, b2 in pairs_bracketing(fam_b):
            corners = []
            ok = True
            for s_a in (a1, a2):
                for s_b in (b1, b2):
                    pt = _line_intersection(
                        s_a[2] * s_a[1], s_a[3], s_b[2] * s_b[1], s_b[3]
                    )
                    if pt is None:
                        ok = False
                        break
                    corners.append(pt)
                if not ok:
                    break
            if not ok or len(corners) != 4:
                continue
            pts = np.array(corners)
            sums = pts.sum(axis=1)
            diffs = pts[:, 0] - pts[:, 1]
            ordered = np.array(
                [
                    pts[np.argmin(sums)],
                    pts[np.argmax(diffs)],
                    pts[np.argmax(sums)],
                    pts[np.argmin(diffs)],
                ]
            )
            H, _ = cv2.findHomography(sheet_mm, ordered, 0)
            if H is None:
                continue
            H = H.astype(np.float64)
            projected_ring = _project(H, ring_mm[None])[0]
            if np.linalg.norm(projected_ring - ring_centroid) > 30:
                continue
            quads.append(H)
    best = None
    for H in quads[:12]:
        t, a, _, _ = _tick_matches(gray, mask, card, H, strict=False)
        score = a + 0.02 * len(t)
        if best is None or score > best[0]:
            best = (score, H)
    return best[1] if best is not None else None


def _similarity(
    ring_mm: np.ndarray, centre_img: np.ndarray, theta: float, scale: float
) -> np.ndarray:
    """3x3 similarity mapping card-mm -> image, anchored at the ring."""
    c, s = math.cos(theta), math.sin(theta)
    R = np.array([[c, -s], [s, c]])
    t = centre_img - scale * (R @ ring_mm)
    H = np.eye(3)
    H[:2, :2] = scale * R
    H[:2, 2] = t
    return H


def _detect_card(
    gray: np.ndarray, image_bgr: np.ndarray, card: dict
) -> tuple[np.ndarray, list[tuple[np.ndarray, np.ndarray]], dict[str, np.ndarray]]:
    mask = _ink_mask(gray)
    ring = _ring_centroid(image_bgr)
    if ring is None:
        raise MeasureError("Cyan ring not found; card not detectable.")
    centre, ring_area_px = ring
    ring_mm = np.array(card["ring"]["centre_mm"], dtype=np.float64)
    frame = _frame_mm(card)

    # Coarse scale from the ring itself: its annulus area is known in
    # mm^2 from the profile, so the equivalent pixel radius gives
    # px-per-mm with no dependence on scene ink (decoy-proof).
    r_out = float(card["ring"]["outer_radius_mm"])
    r_in = float(card["ring"]["inner_radius_mm"])
    ring_area_mm2 = math.pi * (r_out**2 - r_in**2)
    scale0 = math.sqrt(ring_area_px / ring_area_mm2)

    if int((mask > 0).sum()) < 500:
        raise MeasureError("Too little ink; card not detectable.")

    def objective(params: np.ndarray) -> tuple[float, list, int]:
        dx, dy, theta, log_scale = params
        H = _similarity(
            ring_mm, centre + np.array([dx, dy]), theta, math.exp(log_scale)
        )
        ticks, arms_hit, corners, _ = _tick_matches(
            gray, mask, card, H, strict=False
        )
        score = arms_hit + 0.02 * (len(ticks) + len(corners))
        return score, ticks + corners, arms_hit

    # Coarse grid over rotation x scale, ring-anchored. The tick
    # objective is flat zero until the arms land within ~half a stroke
    # width, so a local ascent needs a seed inside the basin; ink
    # moments proved too ill-conditioned to provide one. The ring
    # anchor breaks the frame's 180-degree symmetry (the frame centre
    # is offset from the ring centre), so a full 360-degree sweep is
    # decisive, not ambiguous.
    grid: list[tuple[float, np.ndarray]] = []
    for theta_deg in range(0, 360, 10):
        for factor in (0.86, 0.92, 0.98, 1.04, 1.10):
            trial = np.array(
                [
                    0.0,
                    0.0,
                    math.radians(theta_deg),
                    math.log(scale0 * factor),
                ]
            )
            trial_value, _, _ = objective(trial)
            grid.append((trial_value, trial))
    grid.sort(key=lambda g: -g[0])
    # Top seeds in DISTINCT rotation buckets: under strong
    # perspective the single best similarity cell can sit in a weak
    # basin while the true basin scores slightly lower.
    seeds: list[np.ndarray] = []
    for _, trial in grid:
        theta_deg = math.degrees(trial[2]) % 360
        if all(
            min(abs(theta_deg - s), 360 - abs(theta_deg - s)) >= 20
            for s in (math.degrees(p[2]) % 360 for p in seeds)
        ):
            seeds.append(trial)
        if len(seeds) >= 3:
            break

    frame_keys = ("TL", "TR", "BR", "BL")
    frame_src = np.array([frame[k] for k in frame_keys])

    def loose_score(H_try: np.ndarray) -> float:
        t, a, c, _ = _tick_matches(gray, mask, card, H_try, strict=False)
        return a + 0.02 * (len(t) + len(c))

    def refine_seed(params0: np.ndarray) -> list[np.ndarray]:
        """Similarity ascent + projective vertex search from a seed.

        Returns [H_similarity, H_projective]. A similarity cannot
        express perspective; the vertex search (8 dof, same loose
        objective) can, starting from the similarity's projection.
        """
        params = params0.copy()
        value, _, _ = objective(params)
        steps = np.array([8.0, 8.0, math.radians(1.5), 0.03])
        for _round in range(4):
            improved = True
            while improved:
                improved = False
                for i in range(4):
                    for sign in (1.0, -1.0):
                        trial = params.copy()
                        trial[i] += sign * steps[i]
                        trial_value, _, _ = objective(trial)
                        if trial_value > value + 1e-9:
                            params, value = trial, trial_value
                            improved = True
            steps /= 2
        dx, dy, theta, log_scale = params
        H_sim = _similarity(
            ring_mm, centre + np.array([dx, dy]), theta, math.exp(log_scale)
        )
        vertices = _project(H_sim, frame_src)
        best_val = loose_score(H_sim)
        for step in (16.0, 8.0, 4.0, 2.0):
            improved = True
            while improved:
                improved = False
                for vi in range(4):
                    for axis in range(2):
                        for sign in (1.0, -1.0):
                            trial_v = vertices.copy()
                            trial_v[vi, axis] += sign * step
                            H_try, _ = cv2.findHomography(
                                frame_src, trial_v, 0
                            )
                            if H_try is None:
                                continue
                            val = loose_score(H_try.astype(np.float64))
                            if val > best_val + 1e-9:
                                vertices, best_val = trial_v, val
                                improved = True
        H_proj, _ = cv2.findHomography(frame_src, vertices, 0)
        return [H_sim, H_proj.astype(np.float64)]

    # Every seed's transforms run the full fit pipeline below and
    # the better strict result wins — a search can land in a false
    # basin, and a wild fit must never be reported when a sane one
    # exists.
    seed_observations = []
    H_sheet = _sheet_seed(mask, card, centre)
    if H_sheet is not None:
        t, a, c, _ = _tick_matches(gray, mask, card, H_sheet, strict=False)
        if a >= 4 and len(t) >= 10:
            seed_observations.append((t, c))
    for seed in seeds:
        for H_seed in refine_seed(seed):
            t, a, c, _ = _tick_matches(
                gray, mask, card, H_seed, strict=False
            )
            if a >= 4 and len(t) >= 10:
                seed_observations.append((t, c))
    if not seed_observations:
        H_hough = _hough_quad_seed(gray, mask, card, centre, scale0)
        if H_hough is not None:
            t, a, c, _ = _tick_matches(
                gray, mask, card, H_hough, strict=False
            )
            if a >= 4 and len(t) >= 10:
                seed_observations.append((t, c))
    if not seed_observations:
        raise MeasureError(
            "Tick verification failed (card too degraded or occluded)."
        )


    # Homography fitting. Points on a rectangle's border leave a
    # homography's far field ill-conditioned, and the four corners
    # (line intersections, the most precise measurements available)
    # must not be outvoted by fifteen tick centroids: RANSAC first
    # for outlier rejection, then least squares with each corner
    # repeated as a weight. Re-observe and refit twice.
    def fit(tick_pairs, corner_pairs):
        pool = list(tick_pairs) + list(corner_pairs)
        src = np.array([m for m, _ in pool], dtype=np.float64)
        dst = np.array([p for _, p in pool], dtype=np.float64)
        H_r, inliers = cv2.findHomography(src, dst, cv2.RANSAC, 3.0)
        if H_r is None:
            return None
        if inliers is not None and int(inliers.sum()) >= 8:
            keep = inliers.reshape(-1).astype(bool)
            n_ticks = len(tick_pairs)
            tick_pairs = [
                p for p, k in zip(tick_pairs, keep[:n_ticks]) if k
            ]
            corner_pairs = [
                p for p, k in zip(corner_pairs, keep[n_ticks:]) if k
            ]
        weighted = list(tick_pairs) + list(corner_pairs) * 6
        src = np.array([m for m, _ in weighted], dtype=np.float64)
        dst = np.array([p for _, p in weighted], dtype=np.float64)
        H_l, _ = cv2.findHomography(src, dst, 0)
        return (H_l if H_l is not None else H_r).astype(np.float64)

    def pipeline(tick_pairs, corner_pairs):
        """Fit + strict refits; returns (H, tick_pairs, rms) or None.

        The span gate lives here: points must cover the frame, or
        any extent away from them is extrapolation, not measurement.
        """
        H = fit(tick_pairs, corner_pairs)
        if H is None:
            return None
        for _ in range(2):
            new_ticks, _, new_corners, _ = _tick_matches(
                gray, mask, card, H, strict=True, with_lines=True
            )
            if len(new_ticks) >= 10:
                tick_pairs, corner_pairs = new_ticks, new_corners
            H_new = fit(tick_pairs, corner_pairs)
            if H_new is None:
                break
            H = H_new
        observed = tick_pairs + corner_pairs
        src = np.array([m for m, _ in observed], dtype=np.float64)
        span = src.max(axis=0) - src.min(axis=0)
        frame_span = frame["BR"] - frame["TL"]
        if (
            span[0] < 0.6 * frame_span[0]
            or span[1] < 0.6 * frame_span[1]
            or len(tick_pairs) < 12
        ):
            return None
        tick_src = np.array([m for m, _ in tick_pairs], dtype=np.float64)
        tick_dst = np.array([p for _, p in tick_pairs], dtype=np.float64)
        projected = _project(H, tick_src)
        rms = float(
            np.sqrt(np.mean(np.linalg.norm(projected - tick_dst, axis=1) ** 2))
        )
        return H, tick_pairs, rms

    results = []
    for t0, c0 in seed_observations:
        result = pipeline(t0, c0)
        if result is not None and result[2] <= 4.0:
            results.append(result)
    if not results:
        raise MeasureError(
            "Card fit did not converge to a trustworthy homography "
            "(coverage or residual gates failed)."
        )
    results.sort(key=lambda r: (-len(r[1]), r[2]))
    H, tick_pairs, rms = results[0]
    quad = {
        k: _project(H, v[None])[0] for k, v in frame.items()
    }
    return H, tick_pairs, quad, rms


# --------------------------------------------------------------------------
# Object extent


def _extent_from_points_mm(points_mm: np.ndarray) -> tuple[float, float, np.ndarray]:
    lo = points_mm.min(axis=0)
    hi = points_mm.max(axis=0)
    return float(hi[0] - lo[0]), float(hi[1] - lo[1]), (lo + hi) / 2


def _object_extent(
    image_bgr: np.ndarray,
    H: np.ndarray,
    quad_img: dict[str, np.ndarray],
    bbox: tuple[float, float, float, float] | None,
    mask_obj: np.ndarray | None,
    warnings: list[str],
) -> tuple[float, float, np.ndarray]:
    H_inv = np.linalg.inv(H)
    if mask_obj is not None:
        ys, xs = np.nonzero(mask_obj)
        if len(xs) < 10:
            raise MeasureError("Supplied mask is empty.")
        pts = _project(H_inv, np.stack([xs, ys], axis=1).astype(np.float64))
        return _extent_from_points_mm(pts)
    if bbox is not None:
        x, y, w, h = bbox
        corners = np.array(
            [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64
        )
        warnings.append(
            "Extent from a caller-supplied image bbox: a bbox around a "
            "rotated object overestimates; a mask is tighter."
        )
        return _extent_from_points_mm(_project(H_inv, corners))

    # Prototype fallback: colour segmentation. The object is the largest
    # saturated, non-card contour outside the card quad.
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    candidate = ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] > 40)).astype(np.uint8) * 255
    quad = np.array(
        [quad_img[k] for k in ("TL", "TR", "BR", "BL")], dtype=np.int32
    )
    cv2.fillPoly(candidate, [quad], 0)
    candidate = cv2.morphologyEx(
        candidate, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)
    )
    candidate = cv2.morphologyEx(
        candidate, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)
    )
    contours, _ = cv2.findContours(
        candidate, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    if not contours:
        raise MeasureError(
            "Auto extent found no object (colour segmentation); "
            "supply a bbox or mask."
        )
    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < 0.0005 * image_bgr.shape[0] * image_bgr.shape[1]:
        raise MeasureError("Auto extent contour is implausibly small.")
    warnings.append(
        "Extent by colour segmentation (prototype fallback): the object "
        "must be colour-distinct from the surface and the card."
    )
    pts = largest.reshape(-1, 2).astype(np.float64)
    return _extent_from_points_mm(_project(H_inv, pts))


def _card_width_px(H: np.ndarray, card: dict) -> float:
    """The card sheet's width in image pixels under H.

    Mean of the top and bottom edge lengths of the projected sheet
    corners — the same quantity the card-size battery records as its
    independent variable, so the gate thresholds and the battery's
    pixel counts are directly comparable.
    """
    w = float(card["card_width_mm"])
    h = float(card["card_height_mm"])
    corners = _project(
        H, np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float64)
    )
    top = float(np.linalg.norm(corners[1] - corners[0]))
    bottom = float(np.linalg.norm(corners[2] - corners[3]))
    return (top + bottom) / 2


# --------------------------------------------------------------------------
# Public API


def _load_image(source: str) -> np.ndarray:
    path = source
    if source.startswith("asset://"):
        from .assets import AssetStore
        from .config import get_data_dir

        store = AssetStore(get_data_dir())
        path = str(store.resolve(source))
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        # HEIC and friends: fall back to Pillow (pillow-heif registers
        # itself in ingest; here a plain decode attempt is enough).
        try:
            from PIL import Image

            rgb = np.array(Image.open(path).convert("RGB"))
            img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as exc:
            raise MeasureError(f"Could not decode image: {path}") from exc
    return img


def estimate(
    source: str,
    card_id: str = "AH-46",
    profile_path: Path = DEFAULT_PROFILE_PATH,
    bbox: tuple[float, float, float, float] | None = None,
    mask: np.ndarray | None = None,
) -> dict:
    """Estimate a flat object's size from a photo with the card in plane.

    Returns centre estimates (width_mm, height_mm), a single range_pct
    covering both dimensions, per-dimension ranges, a tilde display
    string, the method string, the detected card's width in image
    pixels (card_width_px), and warnings. The display string is the
    only user-facing form: it always carries the range. A card that
    spans too few pixels in frame raises MeasureError instead — see
    MIN_CARD_PX.
    """
    card = load_card_profile(profile_path, card_id)
    image = _load_image(source)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    warnings: list[str] = ["Prototype: flat object in the same plane as the card only."]
    H, matched, quad_img, rms_px = _detect_card(gray, image, card)
    n_ticks = len(matched)

    # Pixels-across gate: refuse the silent lying band outright, and
    # carry a systematic widening term through the caution band (see
    # the constants at the top of this module). The fit statistics
    # cannot see this failure mode — the RMS stays ~0.25 px while
    # the centre walks off — so the gate keys on card size alone.
    card_px = _card_width_px(H, card)
    if card_px < MIN_CARD_PX:
        raise MeasureError(
            f"Card too small in frame (~{card_px:.0f} px across; the "
            f"fit stays quiet but the range stops covering the true "
            f"size below ~{MIN_CARD_PX:.0f} px) — move closer or use "
            "a bigger card."
        )
    sys_frac = 0.0
    if card_px < COMFORT_CARD_PX:
        band_t = (COMFORT_CARD_PX - card_px) / (COMFORT_CARD_PX - MIN_CARD_PX)
        sys_frac = _SMALL_CARD_SYS_MAX * band_t * band_t
        warnings.append(
            f"Card is small in frame (~{card_px:.0f} px across): the "
            "range is widened for small-card systematic error — a "
            "bigger card or a closer shot would tighten it."
        )
    if n_ticks < 24:
        warnings.append(
            f"Only {n_ticks}/24 tick cuts matched; the range reflects "
            "the fit residual, but a cleaner photo would tighten it."
        )

    width_mm, height_mm, centre_mm = _object_extent(
        image, H, quad_img, bbox, mask, warnings
    )
    if width_mm <= 0 or height_mm <= 0:
        raise MeasureError("Degenerate object extent.")

    # Propagate the detection residual into a range at the object.
    centre_img = _project(H, centre_mm[None])[0]
    eps = 1.0  # mm
    jx = (_project(H, (centre_mm + [eps, 0])[None])[0] - centre_img) / eps
    jy = (_project(H, (centre_mm + [0, eps])[None])[0] - centre_img) / eps
    px_per_mm = float((np.linalg.norm(jx) + np.linalg.norm(jy)) / 2)
    if px_per_mm <= 0:
        raise MeasureError("Degenerate homography at the object location.")
    sigma_mm = rms_px / px_per_mm
    frame = _frame_mm(card)
    frame_centroid = np.mean(list(frame.values()), axis=0)
    frame_diag = float(np.linalg.norm(frame["BR"] - frame["TL"]))
    lever = float(np.linalg.norm(centre_mm - frame_centroid)) / frame_diag

    def half_range(dim: float) -> float:
        # Projective error growth outside the fiducial hull is
        # quadratic in the normalized distance (the inverse map's
        # Jacobian grows with (1 + lever)^2) — verified against the
        # synthetic scenes: ~1px at the frame became ~7px at an
        # object 1.5 frame-diagonals away. A linear lever term
        # understates the range and the "contained" tests catch it.
        u = sigma_mm * (1.0 + lever) ** 2
        # sys_frac is a bias, not noise, so it adds linearly rather
        # than in quadrature; it is zero once the card is comfortably
        # large in frame.
        return 2.0 * u + 0.01 * dim + sys_frac * dim

    err_w, err_h = half_range(width_mm), half_range(height_mm)
    range_pct = max(err_w / width_mm, err_h / height_mm) * 100.0
    range_pct = float(np.ceil(range_pct * 2) / 2)  # round up to 0.5

    def fmt(dim: float, err: float) -> str:
        lo, hi = dim - err, dim + err
        if dim >= 100:
            return f"~{dim / 10:.1f} cm ({lo / 10:.1f}–{hi / 10:.1f} cm)"
        return f"~{dim:.0f} mm ({lo:.0f}–{hi:.0f} mm)"

    return {
        "width_mm": round(width_mm, 1),
        "height_mm": round(height_mm, 1),
        "range_pct": range_pct,
        "width_range_mm": [round(width_mm - err_w, 1), round(width_mm + err_w, 1)],
        "height_range_mm": [round(height_mm - err_h, 1), round(height_mm + err_h, 1)],
        "display": f"{fmt(width_mm, err_w)} wide × {fmt(height_mm, err_h)} high",
        "method": METHOD,
        "card_id": card_id,
        "ticks_matched": n_ticks,
        "card_width_px": round(card_px),
        "reprojection_rms_px": round(rms_px, 2),
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agenthand.measure",
        description="Prototype: estimate a flat object's size from a photo "
        "with an agenthand card in the same plane.",
    )
    parser.add_argument("image", help="Photo path or asset:// handle")
    parser.add_argument(
        "card", help="Card id from the layout profile (e.g. AH-46, AH-57)"
    )
    parser.add_argument(
        "--profile",
        type=Path,
        default=DEFAULT_PROFILE_PATH,
        help="Layout profile JSON (default: the scale-card print profile)",
    )
    parser.add_argument(
        "--bbox",
        type=str,
        default=None,
        help="Object bbox in image pixels, x,y,w,h (default: auto-detect)",
    )
    args = parser.parse_args(argv)
    bbox = None
    if args.bbox:
        parts = tuple(float(v) for v in args.bbox.split(","))
        if len(parts) != 4:
            parser.error("--bbox must be x,y,w,h")
        bbox = parts  # type: ignore[assignment]
    try:
        result = estimate(
            args.image, card_id=args.card, profile_path=args.profile, bbox=bbox
        )
    except MeasureError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
