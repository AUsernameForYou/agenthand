"""Fixed topology/source controls and the supplied hero regressions."""

from pathlib import Path
import numpy as np
import pytest
from PIL import Image
from agenthand import composite, segmentation
from agenthand.cut_quality import grade_cut
from agenthand.assets import AssetStore, KIND_SOURCE, KIND_FOREGROUND

FIXTURES = Path(__file__).parent / "fixtures" / "wonder"


def scene():
    a = np.zeros((100, 140), np.uint8)
    a[20:65, 25:115] = 255
    src = np.full((100, 140, 3), 180, np.uint8)
    return src, a


@pytest.mark.parametrize(
    "defect,code,expected",
    [
        ("clean", None, "PASS"),
        ("legs_included", None, "PASS"),
        ("legs_missing", "SUPPORT_NOT_CAPTURED", "REPAIRABLE"),
        ("fused_shadow", "SUPPORT_NOT_CAPTURED", "RESHOOT"),
        ("hole", "TOP_PERFORATED", "REPAIRABLE"),
        ("notch", "TOP_OCCLUDED", "RESHOOT"),
        ("island", "TOP_OCCLUDED", "RESHOOT"),
        ("fragmented", "MASK_FRAGMENTED", "RESHOOT"),
    ],
)
def test_fixed_controls(defect, code, expected):
    src, a = scene()
    if defect in ("legs_missing", "legs_included"):
        for x in (35, 100):
            src[65:87, x : x + 4] = 15
            if defect == "legs_included":
                a[65:87, x : x + 4] = 255
    if defect == "fused_shadow":
        src[65:87, 30:110] = 15
    if defect == "hole":
        a[30:40, 50:65] = 0
    if defect == "notch":
        a[20:38, 55:80] = 0
    if defect == "island":
        a[8:15, 55:70] = 255
    if defect == "fragmented":
        a[:] = 0
        a[20:40, 20:40] = 255
        a[60:80, 100:120] = 255
    q = grade_cut(src, a)
    assert q["grade"] == expected, q
    if code:
        assert code in q["codes"]


@pytest.mark.parametrize("name", ["mask-1369.png", "mask-1370.png"])
def test_hero_refuses_before_plate_or_storage(tmp_path, monkeypatch, name):
    mask = Image.open(FIXTURES / name).convert("L")
    assert grade_cut(None, mask)["grade"] == "RESHOOT"
    store = AssetStore(tmp_path)
    source = Image.new("RGBA", mask.size, (180, 180, 180, 255))
    sh = store.put_image(source, kind=KIND_SOURCE)
    fg = store.put_image(
        segmentation.extract_foreground(source, mask),
        kind=KIND_FOREGROUND,
        source_asset_id=store.parse_handle(sh),
    )

    def forbidden(*a, **kw):
        raise AssertionError("plate generation/storage reached")

    monkeypatch.setattr(composite, "generate_background_plate", forbidden)
    monkeypatch.setattr(store, "put_image", forbidden)
    with pytest.raises(composite.StageabilityError):
        composite.draw_product(store, fg, "room")


def test_review_contains_source_grade():
    src, a = scene()
    assert segmentation.review_mask(Image.fromarray(a), Image.fromarray(src))[
        "cut_quality"
    ] == grade_cut(src, a)


def test_server_refuses_before_billing(tmp_path, monkeypatch):
    from agenthand import server, meta_client

    src, a = scene()
    a[20:42, 50:80] = 0
    store = AssetStore(tmp_path)
    sh = store.put_image(Image.fromarray(src).convert("RGBA"), kind=KIND_SOURCE)
    fh = store.put_image(
        segmentation.extract_foreground(Image.fromarray(src), Image.fromarray(a)),
        kind=KIND_FOREGROUND,
        source_asset_id=store.parse_handle(sh),
    )
    monkeypatch.setattr(server, "_store", lambda: store)

    def forbidden(*args, **kw):
        raise AssertionError("billing reached")

    monkeypatch.setattr(meta_client, "billed_call", forbidden)
    with pytest.raises(composite.StageabilityError):
        server.draw(fh, "room")


def test_export_verifier_reports_grade(tmp_path):
    from agenthand.verify import verify_files

    src, a = scene()
    paths = [tmp_path / n for n in ("source.png", "final.png", "mask.png")]
    Image.fromarray(src).save(paths[0])
    Image.fromarray(src).save(paths[1])
    Image.fromarray(a).save(paths[2])
    report = verify_files(*paths)
    assert report["ok"] and report["changed_pixels"] == 0
    assert report["cut_quality"]["grade"] == "PASS"


def test_retained_proof_is_a_designated_pass_fixture():
    docs = Path(__file__).resolve().parents[1] / "docs"
    source = Image.open(docs / "proof-source-v1.png")
    mask = Image.open(docs / "proof-mask-v1.png")
    quality = grade_cut(source, mask)
    assert quality["grade"] == "PASS", quality


def test_outer_taper_and_unbounded_dark_background_are_not_top_or_support_defects():
    from PIL import ImageDraw

    alpha = Image.new("L", (200, 200))
    ImageDraw.Draw(alpha).polygon(
        [(30, 10), (120, 10), (170, 70), (170, 120), (30, 120)], fill=255
    )
    # Concave side taper, rather than a hole flanked by product.
    ImageDraw.Draw(alpha).polygon([(120, 10), (120, 65), (170, 70)], fill=0)
    source = np.full((200, 200, 3), 180, np.uint8)
    source[120:, 30:170] = 10
    q = grade_cut(source, alpha)
    assert (
        "TOP_OCCLUDED" not in q["codes"] and "SUPPORT_NOT_CAPTURED" not in q["codes"]
    ), q
