"""Metric-informed plates (Dan's order, 2026-11-03; tracker
decision 152): the generated plate is painted INFORMED BY
MEASURED METRICS from the product photo, not blind from the
scene text.

Mirror of the Rust build's `platemetrics` module; same
vocabularies, tolerances, clause wording and refusal text.

The failure this fixes is measured, not hypothetical: the
library suitcase source photo is a high-angle flat lay, but the
generated plates came out eye-level (the local geometric angle
estimate said "geometry inconclusive; eye level assumed"), so
the verbatim product read tipped and floating in its own staged
photo (redraw candidates A and B, 2026-11-03). A text-only steer
from an inconclusive estimate is a guess; this module replaces
the guess with a measurement and then verifies the result.

Three parts, wired by `server`:
1. MEASURE AT CUT: `cut_out` runs one Muse Spark vision read of
   the source photo (`meta_client.vision_plate_metrics`)
   producing a PlateMetrics record — camera elevation and
   azimuth, key light direction, the ground line, the product's
   frame share, the resting surface's texture period and quality
   register. The record is stored in the mask PNG's provenance
   chunk (beside the concept the scene guard reads) and
   republished on the cut result. A field the read cannot
   support is stored as an explicit unknown (null), never filled
   in by a default.
2. DRAW ENFORCES: `draw` composes the plate prompt with
   system-owned metric clauses (clauses_for) appended AFTER the
   compiled plate instruction, in wording that overrides
   conflicting viewpoint or lighting text. Scene text that
   asserts a contradicting camera or light is screened by
   scene_conflicts and the override is recorded on the receipt;
   the caller's text is never edited.
3. GATE THE RESULT: after compositing, the same vision read
   measures the COMPOSITE; compare checks its camera elevation
   family and light direction against the metrics measured at
   cut. A mismatch retries the draw ONCE with strengthened
   clauses; a second mismatch refuses with
   PLATE_GEOMETRY_MISMATCH and the concrete fix. The gate never
   silently passes a plate it measured as disagreeing.

Honesty rules, matching the rest of the product: unknowns are
stored as unknowns; when nothing was measured there is nothing
to enforce and nothing to gate, and the record says so
(`unmeasured`); when the gate read itself cannot run, the draw
proceeds but the record says `unverified` with the reason —
loud, never silent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .provenance import round6

#: The rule, stated once, carried in every metrics record.
RULE = (
    "the plate is painted from measurements taken from the product "
    "photo itself (camera angle, light direction, ground line), carried "
    "as system-owned clauses the scene text cannot override, and the "
    "finished composite is measured again and checked against them"
)

#: Camera elevations the vision read may report, most downward
#: first. "unknown" is the model's abstention and parses to None.
ELEVATIONS = ("top-down", "high", "eye-level", "low")
#: Camera azimuths relative to the product's front.
AZIMUTHS = (
    "front",
    "front-right",
    "right",
    "back-right",
    "back",
    "back-left",
    "left",
    "front-left",
)
#: Key-light directions relative to the camera.
LIGHT_DIRECTIONS = ("left", "right", "front", "behind", "overhead")
#: The resting surface's quality register (Dan's field note,
#: 2026-11-03): a plate that reads older and dustier than the
#: product's own setting makes the staging look cheap even when
#: every geometric number matches.
SURFACE_QUALITIES = ("premium", "everyday", "worn")

_FIELDS = (
    "camera_elevation",
    "camera_azimuth",
    "light_direction",
    "ground_line",
    "product_frame_share",
    "surface_texture_period",
    "surface_quality",
)


@dataclass
class PlateMetrics:
    """One photo measurement. Every field is optional on purpose:
    a field the read could not support is None (serialized as an
    explicit null), never a default dressed as a measurement."""

    camera_elevation: str | None = None
    camera_azimuth: str | None = None
    light_direction: str | None = None
    #: Where the product meets its resting surface, as a fraction
    #: of the frame height from the top (0.0 top, 1.0 bottom).
    ground_line: float | None = None
    #: The product's share of the frame, 0.0 to 1.0.
    product_frame_share: float | None = None
    #: The width of one repeating surface-texture element (a
    #: plank, board or tile) as a fraction of the frame width;
    #: None when the surface has no repeating texture to measure.
    #: Judged against the product's frame share — the size proxy
    #: the photo itself provides — because one plank wider than
    #: the whole product makes the product read as a miniature
    #: (Dan's field note, 2026-11-03).
    surface_texture_period: float | None = None
    #: The resting surface's quality register in the photo:
    #: "premium" | "everyday" | "worn".
    surface_quality: str | None = None

    @classmethod
    def unknown(cls) -> "PlateMetrics":
        return cls()

    def nothing_to_gate(self) -> bool:
        """True when the record carries nothing the gate can check:
        no camera elevation, no light direction, and no product
        frame share (the share is what surface texture is judged
        against)."""
        return (
            self.camera_elevation is None
            and self.light_direction is None
            and self.product_frame_share is None
        )

    def to_json(self) -> dict:
        """The fields as a dict, unknowns as explicit nulls."""
        return {name: getattr(self, name) for name in _FIELDS}

    @classmethod
    def from_json(cls, value) -> "PlateMetrics":
        """Tolerant read of a stored record (the mask chunk's
        plate_metrics object): each field is validated the same
        way as a fresh model answer, so a hand-edited or stale
        chunk degrades field by field to unknown instead of being
        trusted."""
        if not isinstance(value, dict):
            return cls()
        return cls(
            camera_elevation=_vocab_field(value, "camera_elevation", ELEVATIONS),
            camera_azimuth=_vocab_field(value, "camera_azimuth", AZIMUTHS),
            light_direction=_vocab_field(value, "light_direction", LIGHT_DIRECTIONS),
            ground_line=_fraction_field(value, "ground_line"),
            product_frame_share=_fraction_field(value, "product_frame_share"),
            surface_texture_period=_fraction_field(value, "surface_texture_period"),
            surface_quality=_vocab_field(value, "surface_quality", SURFACE_QUALITIES),
        )


def _vocab_field(value: dict, name: str, vocab) -> str | None:
    raw = value.get(name)
    if not isinstance(raw, str):
        return None
    raw = raw.strip().lower()
    if raw == "unknown":
        return None
    return raw if raw in vocab else None


def _fraction_field(value: dict, name: str) -> float | None:
    n = value.get(name)
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return None
    n = float(n)
    if not math.isfinite(n) or not (0.0 <= n <= 1.0):
        return None
    return round6(n)


#: The vision read's instructions (the schema carries no
#: description keys; the definitions live here, in the prompt).
METRICS_INSTRUCTIONS = (
    "You measure how a product photo was taken. Look at the photo and report only what the photo shows, as JSON.\n"
    'camera_elevation: where the camera sat relative to the product: "top-down" (looking straight down, the resting surface fills the frame), "high" (standing height looking down, the tops of objects visible), "eye-level" (straight on), "low" (below the product, looking up), or "unknown" when the photo does not show it.\n'
    'camera_azimuth: the direction the camera faced relative to the product\'s front: "front", "front-right", "right", "back-right", "back", "back-left", "left", "front-left", or "unknown" when the product\'s front cannot be told.\n'
    'light_direction: where the strongest light comes from, relative to the camera: "left", "right", "front" (behind the camera), "behind" (behind the product), "overhead", or "unknown".\n'
    "ground_line: where the product meets the surface it rests on, as a fraction of the frame height from the top (0.0 is the top edge, 1.0 the bottom edge); null when the product rests on no visible surface.\n"
    "product_frame_share: the share of the frame the product occupies, from 0.0 to 1.0.\n"
    "surface_texture_period: when the surface the product rests on has a repeating texture (floor planks, boards, tiles), the width of ONE element as a fraction of the frame width; null when the surface is plain or has no repeating element.\n"
    'surface_quality: the quality register of the surface the product rests on: "premium" (clean, fine finish, no visible wear), "everyday" (ordinary, lightly used), "worn" (scuffed, dusty, stained or aged), or "unknown".\n'
    'Answer "unknown" or null rather than guessing.'
)

#: The strict JSON schema of one metrics answer.
METRICS_SCHEMA: dict = {
    "name": "plate_metrics",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "camera_elevation": {
                "type": "string",
                "enum": ["top-down", "high", "eye-level", "low", "unknown"],
            },
            "camera_azimuth": {
                "type": "string",
                "enum": [
                    "front",
                    "front-right",
                    "right",
                    "back-right",
                    "back",
                    "back-left",
                    "left",
                    "front-left",
                    "unknown",
                ],
            },
            "light_direction": {
                "type": "string",
                "enum": ["left", "right", "front", "behind", "overhead", "unknown"],
            },
            "ground_line": {"type": ["number", "null"]},
            "product_frame_share": {"type": ["number", "null"]},
            "surface_texture_period": {"type": ["number", "null"]},
            "surface_quality": {
                "type": "string",
                "enum": ["premium", "everyday", "worn", "unknown"],
            },
        },
        "required": [
            "camera_elevation",
            "camera_azimuth",
            "light_direction",
            "ground_line",
            "product_frame_share",
            "surface_texture_period",
            "surface_quality",
        ],
    },
}


def parse_metrics_payload(content: str) -> PlateMetrics:
    """Parse one metrics answer body. A non-JSON or non-object body
    is an error (the caller records the read as unavailable); inside
    an object, each field stands or falls on its own — an
    unsupported value becomes an explicit unknown, never a guess."""
    import json

    try:
        payload = json.loads(content)
    except ValueError as exc:
        raise ValueError(f"Plate metrics response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Plate metrics response is not a JSON object.")
    return PlateMetrics.from_json(payload)


# ---------------------------------------------------------------------------
# Enforcement: the system-owned clauses
# ---------------------------------------------------------------------------


@dataclass
class MetricClauses:
    """The clause parts composed from one measurement, in the order
    they are appended to the plate prompt."""

    camera: str | None = None
    light: str | None = None
    ground: str | None = None
    #: The surface-finish steer from the measured quality register
    #: (Dan's field note): a generated scene's surfaces are told to
    #: read at the register the product's own setting measured,
    #: never dustier.
    finish: str | None = None
    #: Where the product will be placed (decision 153): the surface
    #: is told to run level through the frame's centre at the
    #: measured ground line, its middle kept clear, because the
    #: compositor will set the product down there.
    anchor: str | None = None

    def to_json(self) -> dict:
        return {
            "camera": self.camera,
            "light": self.light,
            "ground": self.ground,
            "finish": self.finish,
            "anchor": self.anchor,
        }

    def parts(self) -> list[str]:
        return [
            part
            for part in (
                self.camera,
                self.light,
                self.ground,
                self.anchor,
                self.finish,
            )
            if part is not None
        ]


def _camera_phrase(elevation: str) -> str:
    return {
        "top-down": "directly overhead looking straight down at the resting surface, a top-down flat-lay view with the surface filling the frame and no horizon visible",
        "high": "at standing height looking steeply down at the resting surface, the tops of objects visible, surface lines converging toward a horizon at or above the top of the frame",
        "eye-level": "at eye level, straight on, the horizon near the middle of the frame and vertical edges staying vertical",
        "low": "low to the ground looking slightly up at the subject, the horizon low in the frame",
    }.get(elevation, "")


def _light_phrase(direction: str) -> str:
    return {
        "left": "from the left side of the frame, shadows falling to the right",
        "right": "from the right side of the frame, shadows falling to the left",
        "front": "from behind the camera, shadows falling behind objects",
        "behind": "from behind the subject, backlit, shadows falling toward the camera",
        "overhead": "from overhead, shadows pooling short beneath objects",
    }.get(direction, "")


def _pct(fraction: float) -> int:
    return int(math.floor(fraction * 100.0 + 0.5))


def clauses_for(metrics: PlateMetrics) -> MetricClauses:
    """Compose the system-owned clauses from the measurement. Unknown
    fields compose nothing: the plate is only ever told what was
    actually measured."""
    camera = (
        f"camera {_camera_phrase(metrics.camera_elevation)}"
        if metrics.camera_elevation is not None
        else None
    )
    light = (
        f"key light {_light_phrase(metrics.light_direction)}"
        if metrics.light_direction is not None
        else None
    )
    ground_parts: list[str] = []
    if metrics.ground_line is not None:
        ground_parts.append(
            "the resting surface meets the subject at about "
            f"{_pct(metrics.ground_line)}% of the frame height from the top, "
            "the surface continuous through that line"
        )
    if metrics.product_frame_share is not None:
        ground_parts.append(
            "the middle of the scene left open at the scale of a subject "
            f"occupying about {_pct(metrics.product_frame_share)}% of the frame"
        )
    if metrics.surface_texture_period is not None:
        ground_parts.append(
            "any repeating surface texture (planks, boards, tiles) repeating "
            f"about every {_pct(metrics.surface_texture_period)}% of the frame "
            "width, at that scale and no wider, so the texture never dwarfs "
            "the subject"
        )
    ground = ", and ".join(ground_parts) if ground_parts else None
    finish = {
        "premium": "surfaces premium and immaculate, fine finish, no dust, stains, scuffs or wear",
        "everyday": "surfaces clean and well kept, no dust, stains, scuffs or heavy wear",
    }.get(metrics.surface_quality or "")
    anchor = (
        "the resting surface runs level through the horizontal centre of "
        f"the frame at about {_pct(metrics.ground_line)}% of the frame "
        "height, its middle kept clear and uncluttered — the product will "
        "be placed there, centred, resting on that line"
        if metrics.ground_line is not None
        else None
    )
    return MetricClauses(
        camera=camera, light=light, ground=ground, finish=finish, anchor=anchor
    )


def clause_text(clauses: MetricClauses, strengthened: bool) -> str | None:
    """The full clause text appended to the plate prompt, after the
    scene text and every other steering clause, so it is the last
    word the plate model reads. `strengthened` is the retry form:
    same measurements, mandatory wording."""
    parts = clauses.parts()
    if not parts:
        return None
    prefix = (
        "Measured plate geometry, from the product photo itself — the previous plate ignored it and failed verification, so this is mandatory and overrides any other viewpoint or lighting wording in this prompt: "
        if strengthened
        else "Measured plate geometry, from the product photo itself — this overrides any other viewpoint or lighting wording in this prompt: "
    )
    return f"{prefix}{'; '.join(parts)}."


# ---------------------------------------------------------------------------
# The conflict screen: scene text asserting against the measurement
# ---------------------------------------------------------------------------

#: Scene phrases that assert a downward camera.
DOWN_ASSERTIONS = (
    "from above",
    "looking down",
    "top-down",
    "top down",
    "overhead",
    "bird's-eye",
    "bird's eye",
    "aerial view",
    "flat lay",
    "flat-lay",
)
#: Scene phrases that assert an eye-level camera.
LEVEL_ASSERTIONS = ("eye-level", "eye level", "straight-on", "straight on")
#: Scene phrases that assert an upward camera.
UP_ASSERTIONS = (
    "from below",
    "low angle",
    "looking up",
    "worm's-eye",
    "worm's eye",
)


def _elevation_family(elevation: str) -> str:
    if elevation in ("top-down", "high"):
        return "down"
    if elevation == "low":
        return "up"
    return "level"


def scene_conflicts(metrics: PlateMetrics, scene_prompt: str) -> list[str]:
    """Screen the caller's scene text for assertions that contradict
    the measured camera or light. Returns the contradicting phrases
    (canonical forms, in scene order of the assertion groups). The
    screen is deliberately narrow — viewpoint and key-light claims
    only — and the outcome is a record, not an edit: the measured
    clauses are appended after the scene text and win by position."""
    scene = scene_prompt.lower()
    found: list[str] = []

    def collect(assertions) -> None:
        for phrase in assertions:
            if phrase in scene and phrase not in found:
                found.append(phrase)

    if metrics.camera_elevation is not None:
        family = _elevation_family(metrics.camera_elevation)
        if family == "down":
            collect(LEVEL_ASSERTIONS)
            collect(UP_ASSERTIONS)
        elif family == "level":
            collect(DOWN_ASSERTIONS)
            collect(UP_ASSERTIONS)
        else:
            collect(DOWN_ASSERTIONS)
            collect(LEVEL_ASSERTIONS)
    if metrics.light_direction == "left" and "from the right" in scene:
        found.append("light from the right")
    elif metrics.light_direction == "right" and "from the left" in scene:
        found.append("light from the left")
    return found


# ---------------------------------------------------------------------------
# The gate: measured metrics vs the composite's own measurement
# ---------------------------------------------------------------------------


@dataclass
class GateComparison:
    """The outcome of comparing the composite's measurement against
    the metrics measured at cut."""

    #: Contradictions beyond tolerance: the gate fails on any.
    mismatches: list[str] = field(default_factory=list)
    #: Differences inside tolerance, recorded for the receipt.
    notes: list[str] = field(default_factory=list)

    def is_pass(self) -> bool:
        return not self.mismatches


def compare(measured: PlateMetrics, observed: PlateMetrics) -> GateComparison:
    """Compare the cut-time measurement against the composite's.
    Tolerance, stated plainly:
    - Camera elevation is gated by FAMILY (looking down / eye
      level / looking up): top-down and high are the same family —
      the degree of a downward angle cannot be told reliably from
      one still — and any cross-family read is a mismatch. A
      composite whose elevation cannot be read at all is a
      mismatch too: an unreadable plate is not a verified plate.
    - Light direction is gated on contradiction: both sides known
      and different is a mismatch, except front vs overhead, which
      wash a scene the same way from a still. A light the gate read
      could not determine is noted, not failed — the angle is the
      axis that tips a product; light is recorded and enforced in
      the prompt either way.
    - Ground line and azimuth steer the prompt but are not gated:
      a ground line more than a quarter-frame off is noted.
    - Surface texture IS gated against the product's own size
      (Dan's field note): a repeating element (one plank, board or
      tile) at least as wide as the product's whole frame share
      makes the product read as a miniature, so it is a mismatch;
      at half the product's share or more it is noted. The check
      runs on the composite's own read — its texture period
      against the product's share (the product layer is pasted
      unscaled, so the share is the same in both photos).
    - The quality register is a scene-quality SIGNAL, noted on
      downgrade (a plate reading dustier than the product's own
      setting), never a refusal ground on its own: register is a
      soft read, and the finish clause already steers it.
    """
    out = GateComparison()
    m_el, o_el = measured.camera_elevation, observed.camera_elevation
    if m_el is not None and o_el is not None:
        if _elevation_family(m_el) != _elevation_family(o_el):
            out.mismatches.append(
                f'camera_elevation: measured "{m_el}", plate "{o_el}"'
            )
        elif m_el != o_el:
            out.notes.append(
                f'camera_elevation: measured "{m_el}", plate "{o_el}" '
                "— same looking-down family, within tolerance"
            )
    elif m_el is not None and o_el is None:
        out.mismatches.append(
            f'camera_elevation: measured "{m_el}", plate angle unreadable'
        )
    m_li, o_li = measured.light_direction, observed.light_direction
    if m_li is not None and o_li is not None and m_li != o_li:
        if (m_li, o_li) in (("front", "overhead"), ("overhead", "front")):
            out.notes.append(
                f'light_direction: measured "{m_li}", plate "{o_li}" '
                "— front and overhead wash a still the same way, within tolerance"
            )
        else:
            out.mismatches.append(
                f'light_direction: measured "{m_li}", plate "{o_li}"'
            )
    elif m_li is not None and o_li is None:
        out.notes.append(
            f'light_direction: measured "{m_li}", plate light unreadable; '
            "not gated on an unreadable light"
        )
    if measured.ground_line is not None and observed.ground_line is not None:
        if abs(measured.ground_line - observed.ground_line) > 0.25:
            out.notes.append(
                f"ground_line: measured {round6(measured.ground_line)}, plate "
                f"{round6(observed.ground_line)} — more than a quarter-frame "
                "apart; noted, not gated"
            )
    # Surface texture scale vs the product's own size: judged on
    # the composite's read (its texture, the product's share).
    share = (
        measured.product_frame_share
        if measured.product_frame_share is not None
        else observed.product_frame_share
    )
    period = observed.surface_texture_period
    if period is not None and share is not None:
        if period >= share:
            out.mismatches.append(
                "surface_texture_scale: one texture element is about "
                f"{_pct(period)}% of the frame width, at least the product's "
                f"whole {_pct(share)}% share — the product reads as a miniature"
            )
        elif period >= share * 0.5:
            out.notes.append(
                "surface_texture_scale: one texture element is about "
                f"{_pct(period)}% of the frame width against the product's "
                f"{_pct(share)}% share — coarse against the product; noted"
            )
    # Quality register: a downgrade is recorded as a scene-quality
    # signal, not gated (the finish clause steers it).
    rank = {"premium": 3, "everyday": 2, "worn": 1}
    if measured.surface_quality is not None and observed.surface_quality is not None:
        if rank.get(observed.surface_quality, 0) < rank.get(measured.surface_quality, 0):
            out.notes.append(
                f'surface_quality: the product\'s setting measured "{measured.surface_quality}", '
                f'the plate reads "{observed.surface_quality}" — a register '
                "downgrade, recorded as a scene-quality signal"
            )
    return out


def grounding_assessment(contact_y: float | None) -> dict:
    """The pre-spend grounding assessment (redraw lane finding,
    2026-11-03): where the product's resting line sits in the
    source frame predicts whether a painted surface can reach it.
    Draw never repositions the product's SHAPE, so a product whose
    contact line sits high (a book photographed at y .17–.33)
    hangs mid-air on any plate whose surface stays in the lower
    frame. The assessment is computed from local geometry before
    any spend, recorded on the draw, and answered two ways: the
    ground clause steers a raised surface to the line, and the
    placement below centres the product on it. Thresholds are
    stated heuristics, not measurements: a contact line at 60% of
    the frame height or lower meets ordinary surfaces (floors,
    tabletops) where plates paint them; 45–60% is counter height;
    above 45% a surface has to be deliberately raised to reach."""
    if contact_y is None:
        return {
            "status": "unknown",
            "anchor": None,
            "contact_y": None,
            "reason": "no product bounding box; grounding not assessed",
        }
    contact_y = round6(contact_y)
    if contact_y >= 0.6:
        return {
            "status": "groundable",
            "anchor": "low",
            "contact_y": contact_y,
            "reason": "the product's resting line sits in the lower frame, where painted surfaces ordinarily run",
        }
    if contact_y >= 0.45:
        return {
            "status": "groundable",
            "anchor": "central",
            "contact_y": contact_y,
            "reason": "the product's resting line sits mid-frame (counter or table height); the ground clause names that line for the plate",
        }
    return {
        "status": "at_risk",
        "anchor": "high",
        "contact_y": contact_y,
        "reason": "the product's resting line sits high in the frame; a painted surface rarely reaches that high, so the product can hang mid-air — the ground clause steers a raised surface (shelf, counter, tabletop) to the line and placement centres the product on it",
    }


# ---------------------------------------------------------------------------
# Placement (decision 153): translation only, offset receipted
# ---------------------------------------------------------------------------


@dataclass
class PlacementPlan:
    """A planned translation of the verbatim product layer to a
    grounded anchor. Translation is the one repositioning the
    Fidelity Lock allows: every product pixel VALUE stays
    byte-identical, only its position changes, and the offset is
    recorded so verification recounts the exact pixels at the
    placed offset. Scale and rotation stay forbidden — they
    resample pixels and would break the proof."""

    #: Horizontal shift in pixels (positive moves the product right).
    dx: int
    #: Vertical shift in pixels (positive moves the product down).
    dy: int
    #: The anchor policy that produced the offset.
    anchor: str
    #: True when the offset is not (0, 0).
    applied: bool
    reason: str
    #: The ground line the base was aligned to, when measured.
    target_ground_line: float | None

    def to_json(self) -> dict:
        return {
            "policy": "translation_only",
            "anchor": self.anchor,
            "dx": self.dx,
            "dy": self.dy,
            "applied": self.applied,
            "target_ground_line": self.target_ground_line,
            "reason": self.reason,
        }


def _round_half_away(x: float) -> int:
    """Round to the nearest integer, halves away from zero (the
    Rust build's `as i32` cast after f64::round)."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def plan_placement(
    frame_width: int,
    frame_height: int,
    bbox: tuple[int, int, int, int],
    metrics: PlateMetrics,
) -> PlacementPlan:
    """Plan the placement of one product: horizontally centred, base
    aligned to the measured ground line (decision 153's default
    policy). The offset is clamped so the product's whole bounding
    box stays inside the frame — a placed product is never
    clipped. Without a measured ground line the base stays where
    the photo put it and only the centring applies."""
    x0, y0, x1, y1 = bbox
    w, h = frame_width, frame_height
    # Centre the bbox horizontally: shift by (frame centre − bbox
    # centre), clamped to keep the box inside the frame.
    dx = _round_half_away((w - (x0 + x1)) / 2.0)
    dx = max(-x0, min(w - x1, dx))
    # Align the bbox's base to the measured ground line.
    if metrics.ground_line is not None:
        target_y1 = _round_half_away(metrics.ground_line * frame_height)
        dy = max(-y0, min(h - y1, target_y1 - y1))
        target_ground_line: float | None = metrics.ground_line
    else:
        dy = 0
        target_ground_line = None
    applied = dx != 0 or dy != 0
    anchor = (
        "centered_on_measured_ground_line"
        if target_ground_line is not None
        else "centered"
    )
    reason = (
        "the product layer is translated, unscaled and unrotated, to centre it on the measured ground line"
        if applied
        else "the product already sits at the anchor; no translation"
    )
    return PlacementPlan(
        dx=dx,
        dy=dy,
        anchor=anchor,
        applied=applied,
        reason=reason,
        target_ground_line=target_ground_line,
    )


def _elevation_human(elevation: str) -> str:
    """How a measured elevation is described to a human fixing a
    refused draw."""
    return {
        "top-down": "from directly overhead",
        "high": "from standing height, looking down",
        "eye-level": "straight on at eye level",
        "low": "from low down, looking up",
    }.get(elevation, "as measured")


def _elevation_scene_hint(elevation: str) -> str:
    """The scene a caller should describe to match a measured
    elevation."""
    return {
        "top-down": "a flat surface photographed straight from above",
        "high": "a surface seen from standing height, looking down",
        "eye-level": "a room photographed straight on at eye level",
        "low": "a scene photographed from below, looking up",
    }.get(elevation, "a scene photographed the way the product was")


def refusal_message(measured: PlateMetrics, mismatches: list[str]) -> str:
    """The PLATE_GEOMETRY_MISMATCH refusal message: what disagreed,
    that the retry already happened, and the concrete fix — the
    house error style (every failure carries its next step)."""
    elevation = measured.camera_elevation or "unknown"
    texture_fix = (
        ", with any planks, boards or tiles at the product's own scale"
        if any(m.startswith("surface_texture_scale") for m in mismatches)
        else ""
    )
    return (
        "The generated background does not match the photo it must sit "
        f"behind: {'; '.join(mismatches)}. "
        "The draw already retried once with the measured camera and light "
        "enforced as mandatory clauses, and the new plate still disagrees, "
        "so the composite would show the product tipped, floating or "
        "shrunk against its own background and it is not being presented. "
        "Retake the product photo at the angle the scene needs, "
        "or describe a scene photographed the way the product was "
        f"({_elevation_human(elevation)}, {_elevation_scene_hint(elevation)})"
        f"{texture_fix} and call draw again."
    )
