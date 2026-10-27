"""SAM wire parsing, mask QA, and foreground extraction: offline units.

The canned payloads below are real SAM 3 wire text. The one_bit
payloads were produced with the official parser package's own raster
encoder and round-tripped through meta_sam_parser's strict decoder
during development; the lossless example is copied from the package's
published documentation. No network, no key: these tests exercise
sam_masks (the parser swap boundary) and segmentation only.
"""

import os

os.environ.setdefault("AGENTHAND_DATA_DIR", "/tmp/agenthand-test-placeholder")

from PIL import Image  # noqa: E402

from agenthand import segmentation  # noqa: E402
from agenthand.sam_masks import parse_sam_masks  # noqa: E402

# Frame is 8x6 in the small fixtures.
# GOOD: box-local 4x6 all-ones raster over the left half.
GOOD_TEXT = (
    "<0f>0<|box;x1=0;y1=0;x2=3;y2=5;w=8;h=6|>"
    "<|mask;x=0;y=0;data=6,4,!!!!!'y8_cbF.|>\n"
)
# FULL: the whole 8x6 frame.
FULL_TEXT = (
    "<0f>0<|box;x1=0;y1=0;x2=7;y2=5;w=8;h=6|>"
    "<|mask;x=0;y=0;data=6,8,!!!!!'y:A%B`S|>\n"
)
# FRAGMENTED: two 2x3 blocks at opposite corners of the frame.
FRAG_TEXT = (
    "<0f>0<|box;x1=0;y1=0;x2=7;y2=5;w=8;h=6|>"
    "<|mask;x=0;y=0;data=6,8,!!!!!+dhFaV_wsyCio|>\n"
)
# ZERO: a real record whose raster is all zeros.
ZERO_TEXT = (
    "<0f>0<|box;x1=0;y1=0;x2=3;y2=5;w=8;h=6|>"
    "<|mask;x=0;y=0;data=6,4,!!!!!&!!!!!|>\n"
)
# TWO RECORDS: a 2x2 blob (object 0) and the good block (object 1).
TWO_RECORDS_TEXT = (
    "<0f>0<|box;x1=6;y1=4;x2=7;y2=5;w=8;h=6|>"
    "<|mask;x=0;y=0;data=2,2,!!!!!&t.SKi|>"
    ",1<|box;x1=0;y1=0;x2=3;y2=5;w=8;h=6|>"
    "<|mask;x=0;y=0;data=6,4,!!!!!'y8_cbF.|>\n"
)
# The lossless example from meta-sam-parser's published docs (320x334).
DOCS_LOSSLESS_TEXT = (
    "<0f>0<|box;x1=211;y1=228;x2=270;y2=254;w=320;h=334|>"
    "<|mask;x=0;y=0;data=27,60,~!!!!M!0c[0o91w?q1!pIH4pPRVp2B3'7`e.ioeAf6-k/"
    "#Xd8%dX9x(|>\n"
)


def _mask_image(text=GOOD_TEXT, size=(8, 6)):
    masks = parse_sam_masks(text, *size)
    assert len(masks) == 1
    return masks[0]


# -- mask correctness gate: overlay, review signals, golden IoU -------------


def _left_half_mask(size=(8, 6)):
    mask = Image.new("L", size, 0)
    px = mask.load()
    for y in range(size[1]):
        for x in range(size[0] // 2):
            px[x, y] = 255
    return mask


def test_mask_iou_against_a_golden_fixture():
    golden = _left_half_mask()
    decoded = _mask_image(GOOD_TEXT).image
    assert segmentation.mask_iou(decoded, golden) == 1.0
    # The wrong-object case (Fable: "the table, not the mug"): the
    # fragmented mask overlaps the golden only at one corner block.
    wrong = _mask_image(FRAG_TEXT).image
    assert segmentation.mask_iou(wrong, golden) < 0.5


def test_mask_overlay_draws_the_outline_and_nothing_else():
    source = Image.new("RGBA", (8, 6), (200, 30, 30, 255))
    overlay = segmentation.mask_overlay_image(source, _left_half_mask())
    px = overlay.load()
    outline = segmentation.OVERLAY_OUTLINE_RGBA
    assert px[3, 3] == outline  # the mask's right boundary column
    assert px[0, 0] == outline  # frame corner of the selection
    assert px[1, 1] == (200, 30, 30, 255)  # interior: source untouched
    assert px[6, 3] == (200, 30, 30, 255)  # outside: source untouched


def test_review_mask_reports_geometry_without_warnings_for_a_clean_mask():
    review = segmentation.review_mask(_left_half_mask())
    assert review["mask_pixels"] == 24
    assert review["coverage"] == 0.5
    assert review["bbox"] == [0, 0, 4, 6]
    assert review["bbox_fill"] == 1.0
    assert review["touches_frame_edges"] == ["left", "top", "bottom"]
    assert review["warnings"] == []


def test_review_mask_flags_a_frame_shaped_selection():
    # A border ring: touches every edge and fills a third of its box —
    # the classic "selected the background/frame, not the product".
    ring = Image.new("L", (20, 14), 0)
    px = ring.load()
    for x in range(20):
        px[x, 0] = px[x, 13] = 255
    for y in range(14):
        px[0, y] = px[19, y] = 255
    review = segmentation.review_mask(ring)
    assert review["touches_frame_edges"] == ["left", "right", "top", "bottom"]
    assert any("all four frame edges" in w for w in review["warnings"])
    assert any("bounding box" in w for w in review["warnings"])


# -- wire parsing -------------------------------------------------------------


def test_parse_good_mask_places_box_local_raster_in_frame():
    mask = _mask_image()
    assert mask.object_id == "0"
    assert mask.bounds == (0, 0, 4, 6)  # inclusive wire corners -> half-open
    assert mask.mask_pixels == 24
    px = mask.image.load()
    assert px[0, 0] == 255 and px[3, 5] == 255  # inside the box
    assert px[4, 0] == 0 and px[7, 5] == 0  # outside the box


def test_parse_empty_lane_is_a_zero_match_not_an_error():
    assert parse_sam_masks("", 8, 6) == []
    assert parse_sam_masks("no objects found", 8, 6) == []


def test_parse_docs_lossless_example_decodes():
    masks = parse_sam_masks(DOCS_LOSSLESS_TEXT, 320, 334)
    assert len(masks) == 1
    mask = masks[0]
    assert mask.bounds == (211, 228, 271, 255)
    assert mask.mask_pixels == 1539  # the docs example's decoded coverage
    px = mask.image.load()
    assert px[0, 0] == 0  # far outside the box


def test_parse_multiple_records_keeps_all_in_order():
    masks = parse_sam_masks(TWO_RECORDS_TEXT, 8, 6)
    assert [m.object_id for m in masks] == ["0", "1"]
    assert [m.mask_pixels for m in masks] == [4, 24]


# -- mask QA --------------------------------------------------------------------


def test_qa_accepts_the_good_mask():
    assessment = segmentation.assess_mask(_mask_image().image)
    assert assessment.accepted
    assert assessment.rejection is None
    assert assessment.mask_pixels == 24
    assert assessment.coverage == 0.5
    assert assessment.components == 1
    assert assessment.dominant_fraction == 1.0


def test_qa_rejects_full_frame():
    assessment = segmentation.assess_mask(_mask_image(FULL_TEXT).image)
    assert not assessment.accepted
    assert assessment.rejection == "full_frame"
    assert assessment.coverage == 1.0


def test_qa_rejects_fragmented_mask():
    assessment = segmentation.assess_mask(_mask_image(FRAG_TEXT).image)
    assert not assessment.accepted
    assert assessment.rejection == "fragmented"
    assert assessment.components == 2
    assert assessment.dominant_fraction == 0.5


def test_qa_rejects_empty_mask():
    assessment = segmentation.assess_mask(_mask_image(ZERO_TEXT).image)
    assert not assessment.accepted
    assert assessment.rejection == "empty"
    assert assessment.mask_pixels == 0


# -- foreground extraction -------------------------------------------------------


def test_extract_foreground_keeps_source_pixels_where_mask_is_on():
    source = Image.new("RGBA", (8, 6), (120, 120, 120, 255))
    px = source.load()
    for y in range(6):
        for x in range(4):
            px[x, y] = (200, 30, 30, 255)  # the "product": left half
    foreground = segmentation.extract_foreground(source, _mask_image().image)
    fg = foreground.load()
    for y in range(6):
        for x in range(4):
            assert fg[x, y] == (200, 30, 30, 255)  # byte-for-byte the source
        for x in range(4, 8):
            assert fg[x, y] == (0, 0, 0, 0)  # transparent elsewhere
