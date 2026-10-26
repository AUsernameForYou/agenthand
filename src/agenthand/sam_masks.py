"""SAM 3.1 mask wire format: THE PARSER SWAP BOUNDARY.

Everything agenthand knows about how SAM 3.1 returns segmentation —
special-token text with base85 mask payloads — lives in this module and
nowhere else. Behind the boundary sits Meta's official parser package,
``meta-sam-parser`` (PyPI, by Meta Platforms, Inc.; the Python
implementation of the SAM 3 segmentation protocol, companion to the
TypeScript parser). If Meta revises the wire format, or the parser is
ever swapped for another implementation, this module is the only file
that changes: the rest of agenthand consumes ``DecodedMask`` objects —
a full-frame binary raster plus record metadata — and never sees a
payload string.

Wire format (as parsed by the official package): the model's output
text is one line per frame, ``<Nf>`` then comma-separated records
``id<|box;x1=..;y1=..;x2=..;y2=..;w=<frameW>;h=<frameH>|><|mask;x=0;y=0;data=<H>,<W>,<payload>|>``.
Box corners are inclusive on the wire; the parser normalizes them to
half-open bounds. The mask raster is BOX-LOCAL: its own height/width,
placed at the record's bounds inside the frame. The payload's first
character selects the encoding: ``~`` lossless (coverage raster,
thresholded at >= 129 by the decoder) or ``!`` one_bit. An empty lane
(no records) is a valid response meaning "nothing matched".

Assumption recorded for the live verification: image responses use the
same ``<0f>`` frame-zero line as video frame 0 (the parser's image
format requires frame zero), and the segmentation text arrives as the
Responses API's ``output_text``.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

# Byte-translation table: any nonzero raster byte becomes 255.
_TO_FULL = bytes(0 if i == 0 else 255 for i in range(256))


class SamOutputError(RuntimeError):
    """Raised when SAM output text cannot be parsed or decoded at all.

    Distinct from a zero-match: the model answered, but its answer is
    not usable SAM wire text (or a mask payload failed the parser's
    strict decode). Callers treat this like identify's unparseable
    response — the billed reservation stays counted.
    """


@dataclass
class DecodedMask:
    """One mask record decoded and placed into the full frame."""

    object_id: str
    # Half-open bounds inside the frame, in pixels: (left, top, right, bottom).
    bounds: tuple[int, int, int, int]
    # Mask pixels inside the record's own raster (before frame placement).
    mask_pixels: int
    # Full-frame binary mask, mode "L": 255 where the mask is on, else 0.
    image: Image.Image


def _place_in_frame(
    raster: bytes,
    width: int,
    height: int,
    bounds: tuple[int, int, int, int],
    frame_width: int,
    frame_height: int,
) -> Image.Image:
    """Place a box-local 0/1 raster into a full-frame "L" image (0/255).

    The raster is clipped against the frame edges explicitly (PIL's
    paste does not clip out-of-frame boxes), so a record whose bounds
    overhang the frame still lands correctly.
    """
    left, top, right, bottom = bounds
    local = Image.frombytes("L", (width, height), raster.translate(_TO_FULL))
    frame = Image.new("L", (frame_width, frame_height), 0)
    # Intersect the destination box with the frame.
    dst_left, dst_top = max(left, 0), max(top, 0)
    dst_right, dst_bottom = min(right, frame_width), min(bottom, frame_height)
    if dst_right <= dst_left or dst_bottom <= dst_top:
        return frame
    src_box = (
        dst_left - left,
        dst_top - top,
        dst_left - left + (dst_right - dst_left),
        dst_top - top + (dst_bottom - dst_top),
    )
    frame.paste(local.crop(src_box), (dst_left, dst_top))
    return frame


def parse_sam_masks(
    output_text: str, frame_width: int, frame_height: int
) -> list[DecodedMask]:
    """Decode every mask record in one SAM image response.

    Returns one DecodedMask per mask record, in response order; an
    empty list is a normal zero-match outcome, not an error. Raises
    SamOutputError when the text cannot be parsed or a payload fails
    strict decoding.
    """
    # Imported here so the swap point is a single, obvious place: to
    # replace the parser, replace what happens inside this function.
    from meta_sam_parser import (
        CompletedOutcome,
        SegmentationMaskRecord,
        decode_mask_to_raster,
        image_segmentation_format,
    )

    parser = image_segmentation_format().create_parser()
    try:
        parser.push(output_text or "")
        result = parser.finish(CompletedOutcome()).result
    except Exception as exc:
        raise SamOutputError(
            f"SAM output could not be parsed as segmentation text: {exc}"
        ) from exc

    masks: list[DecodedMask] = []
    for record in result.records:
        if not isinstance(record, SegmentationMaskRecord):
            continue
        try:
            raster = decode_mask_to_raster(record.mask)
        except Exception as exc:
            raise SamOutputError(
                f"SAM mask for object {record.object_id!r} failed strict "
                f"decoding: {exc}"
            ) from exc
        bounds = (
            int(record.bounds.left),
            int(record.bounds.top),
            int(record.bounds.right),
            int(record.bounds.bottom),
        )
        masks.append(
            DecodedMask(
                object_id=record.object_id,
                bounds=bounds,
                mask_pixels=sum(raster),
                image=_place_in_frame(
                    raster,
                    record.mask.width,
                    record.mask.height,
                    bounds,
                    frame_width,
                    frame_height,
                ),
            )
        )
    return masks
