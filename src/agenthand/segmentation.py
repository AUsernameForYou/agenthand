"""Segmentation quality control and the bounded mask-recovery loop.

cut_out's model step, between the wire boundary (sam_masks) and the
asset store:

- ``assess_mask`` quality-checks one decoded mask. A usable product
  mask covers a sane fraction of the frame (not empty, not ~the whole
  frame) and is dominated by a single connected region. The thresholds
  are module constants, chosen for product photos: the product is one
  object occupying a real share of its photo.
- ``extract_foreground`` builds the transparent foreground: source
  pixels exactly where the mask is on, transparent black elsewhere.
  Nothing is regenerated — this is the Fidelity Lock's extraction step.
- ``run_attempts`` is the recovery loop the tool contract promises:
  try the concept phrase; on a zero-match or a poor mask, retry ONCE
  with the first alternate phrase. Every attempt is traced (phrase,
  outcome, coverage, QA detail). Total failure raises
  CutOutResultError carrying the trace; the spend for every attempt
  stays counted because run_attempts is the fn of one billed_call
  whose usage reports one image per attempt made.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from PIL import Image

from . import meta_client
from .sam_masks import DecodedMask, SamOutputError, parse_sam_masks

# QA thresholds. Coverage is mask pixels / frame pixels.
MIN_COVERAGE = 0.001  # below this the mask is effectively empty
MAX_COVERAGE = 0.98  # at or above this the "mask" is ~the whole frame
MIN_DOMINANT_FRACTION = 0.75  # largest component / mask pixels


class CutOutResultError(RuntimeError):
    """Raised when no attempt produced a usable mask.

    Attributes:
        code: "NO_MATCH" when the final attempt returned no mask at
            all, "POOR_MASK" when the final attempt returned a mask
            that failed QA, "BAD_MODEL_RESPONSE" when SAM output
            could not be parsed/decoded.
        attempts: The full attempt trace (see run_attempts).
    """

    def __init__(self, message: str, *, code: str, attempts: list[dict]):
        super().__init__(message)
        self.code = code
        self.attempts = attempts


@dataclass
class MaskAssessment:
    mask_pixels: int
    coverage: float  # mask pixels / frame pixels
    components: int  # connected components in the mask
    dominant_fraction: float  # largest component / mask pixels
    accepted: bool
    rejection: str | None  # "empty" | "full_frame" | "fragmented" | None


def _label_components(binary: bytes, width: int, height: int) -> tuple[int, int]:
    """(component_count, largest_component_size) of a 0/1 raster.

    4-connectivity flood fill over the set pixels. Pure Python; photo
    rasters are a few megapixels at most after ingest caps.
    """
    visited = bytearray(len(binary))
    components = 0
    largest = 0
    for start in range(len(binary)):
        if not binary[start] or visited[start]:
            continue
        components += 1
        size = 0
        queue = deque([start])
        visited[start] = 1
        while queue:
            i = queue.popleft()
            size += 1
            x = i % width
            if x > 0 and binary[i - 1] and not visited[i - 1]:
                visited[i - 1] = 1
                queue.append(i - 1)
            if x < width - 1 and binary[i + 1] and not visited[i + 1]:
                visited[i + 1] = 1
                queue.append(i + 1)
            if i >= width and binary[i - width] and not visited[i - width]:
                visited[i - width] = 1
                queue.append(i - width)
            if (
                i < len(binary) - width
                and binary[i + width]
                and not visited[i + width]
            ):
                visited[i + width] = 1
                queue.append(i + width)
        largest = max(largest, size)
    return components, largest


def assess_mask(mask_image: Image.Image) -> MaskAssessment:
    """Quality-check one full-frame binary mask ("L", 255 = on)."""
    width, height = mask_image.size
    frame_pixels = width * height
    binary = bytes(1 if value else 0 for value in mask_image.tobytes())
    mask_pixels = sum(binary)
    coverage = mask_pixels / frame_pixels if frame_pixels else 0.0
    if mask_pixels == 0:
        return MaskAssessment(0, 0.0, 0, 0.0, False, "empty")
    components, largest = _label_components(binary, width, height)
    dominant = largest / mask_pixels
    if coverage < MIN_COVERAGE:
        rejection: str | None = "empty"
    elif coverage >= MAX_COVERAGE:
        rejection = "full_frame"
    elif dominant < MIN_DOMINANT_FRACTION:
        rejection = "fragmented"
    else:
        rejection = None
    return MaskAssessment(
        mask_pixels=mask_pixels,
        coverage=coverage,
        components=components,
        dominant_fraction=dominant,
        accepted=rejection is None,
        rejection=rejection,
    )


def extract_foreground(source: Image.Image, mask_image: Image.Image) -> Image.Image:
    """The product layer: source pixels through the mask, nothing else.

    Where the mask is on, the source pixel is copied byte-for-byte;
    where it is off, the pixel becomes transparent black. The mask is
    binary (0/255), so the composite is an exact selection, not a blend.
    """
    source_rgba = source.convert("RGBA")
    transparent = Image.new("RGBA", source_rgba.size, (0, 0, 0, 0))
    return Image.composite(source_rgba, transparent, mask_image)


def _choose_mask(masks: list[DecodedMask]) -> DecodedMask:
    """Pick the record to QA: the largest mask by pixel count.

    A text-prompted segmentation can return several candidate objects;
    the product is the dominant one. The choice is deterministic (ties
    keep response order) and the record count rides in the trace.
    """
    return max(masks, key=lambda m: m.mask_pixels)


def run_attempts(
    png_bytes: bytes,
    phrases: list[str],
    *,
    model: str,
    frame_width: int,
    frame_height: int,
    client=None,
) -> dict:
    """Run the bounded attempt loop; returns the winning mask + trace.

    ``phrases`` is [concept, first_alternate] at most (the caller caps
    it): the concept is tried first, the alternate is the single
    recovery retry. On success returns a dict with mask_image (the
    winning full-frame "L" mask), mask (the DecodedMask), phrase (the
    phrase that produced it), attempts (the trace), and usage
    ({"images": <attempts made>}). Raises CutOutResultError when every
    attempt fails; a SAM response that cannot be parsed raises
    CutOutResultError with code BAD_MODEL_RESPONSE immediately (a
    different phrase will not fix a broken wire format).

    This performs network I/O (via meta_client.segment_image) and must
    only run as the fn of billed_call.
    """
    attempts: list[dict] = []
    images = 0
    last_code = "NO_MATCH"
    for phrase in phrases:
        output_text, _usage = meta_client.segment_image(
            png_bytes, phrase, model=model, client=client
        )
        images += 1
        trace: dict = {"phrase": phrase, "records": 0}
        try:
            masks = parse_sam_masks(output_text, frame_width, frame_height)
        except SamOutputError as exc:
            trace["outcome"] = "unparseable"
            attempts.append(trace)
            raise CutOutResultError(
                f"SAM output for phrase {phrase!r} could not be used: {exc}",
                code="BAD_MODEL_RESPONSE",
                attempts=attempts,
            ) from exc
        trace["records"] = len(masks)
        if not masks:
            # Zero-match is a normal outcome: it drives the retry.
            trace["outcome"] = "no_match"
            attempts.append(trace)
            last_code = "NO_MATCH"
            continue
        chosen = _choose_mask(masks)
        assessment = assess_mask(chosen.image)
        trace.update(
            {
                "object_id": chosen.object_id,
                "coverage": round(assessment.coverage, 6),
                "mask_pixels": assessment.mask_pixels,
                "components": assessment.components,
                "dominant_fraction": round(assessment.dominant_fraction, 6),
            }
        )
        if not assessment.accepted:
            trace["outcome"] = "poor_mask"
            trace["rejection"] = assessment.rejection
            attempts.append(trace)
            last_code = "POOR_MASK"
            continue
        trace["outcome"] = "accepted"
        attempts.append(trace)
        return {
            "mask_image": chosen.image,
            "mask": chosen,
            "phrase": phrase,
            "attempts": attempts,
            "usage": {"images": images},
        }
    raise CutOutResultError(
        f"No usable mask after {len(attempts)} attempt(s); "
        f"final outcome: {last_code}.",
        code=last_code,
        attempts=attempts,
    )
