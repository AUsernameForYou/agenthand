"""The Fidelity Lock invariant, tested the way the J1 Gauntlet demanded:
with a solid-color fake plate and a known foreground, zero API calls.
"""

import inspect

import pytest
from PIL import Image

from agenthand import composite
from agenthand.assets import AssetIntegrityError, AssetStore


def _foreground() -> Image.Image:
    """6x4: opaque red block, one edge column (alpha 128), transparent rest."""
    img = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    px = img.load()
    for y in range(4):
        for x in range(3):
            px[x, y] = (255, 0, 0, 255)
        px[3, y] = (0, 255, 0, 128)
    return img


def _photo() -> Image.Image:
    return Image.new("RGBA", (6, 4), (90, 90, 90, 255))


def _store_with_foreground(tmp_path, with_mask=False):
    store = AssetStore(tmp_path)
    photo = store.put_image(_photo(), kind="source_photo")
    parent = AssetStore.parse_handle(photo)
    if with_mask:
        mask_img = Image.new("RGBA", (6, 4), (255, 255, 255, 255))
        mask = store.put_image(mask_img, kind="mask_png", source_asset_id=parent)
        parent = AssetStore.parse_handle(mask)
    fg = store.put_image(_foreground(), kind="foreground_png", source_asset_id=parent)
    return store, photo, fg


def test_invariant_opaque_pixels_unchanged_edge_pixels_counted(tmp_path):
    plate = Image.new("RGBA", (6, 4), (0, 0, 255, 255))
    out, counts, transformations = composite.composite_images(_foreground(), plate)

    assert transformations == []
    assert counts["opaque_pixels"] == 12
    assert counts["edge_band_pixels"] == 4
    assert counts["transparent_pixels"] == 8
    # THE invariant: not one fully opaque foreground pixel changed.
    assert counts["changed_opaque_pixels"] == 0

    px = out.load()
    assert px[0, 0] == (255, 0, 0, 255)  # opaque foreground, untouched
    assert px[5, 0] == (0, 0, 255, 255)  # transparent: plate shows through
    edge = px[3, 0]
    assert edge[3] == 255 and 100 < edge[1] < 160 and 100 < edge[2] < 160  # blended


def test_plate_is_fitted_to_the_product_never_the_reverse(tmp_path):
    big_plate = Image.new("RGBA", (24, 16), (0, 0, 255, 255))
    out, counts, transformations = composite.composite_images(_foreground(), big_plate)
    assert out.size == (6, 4)  # product dimensions win
    assert transformations  # and the fitting is on the record


def test_plate_signature_takes_no_product_pixels():
    params = list(inspect.signature(composite.generate_background_plate).parameters)
    assert params == ["prompt", "width", "height"]
    a = composite.generate_background_plate("a regal room", 6, 4)
    b = composite.generate_background_plate("a regal room", 6, 4)
    assert a.tobytes() == b.tobytes()  # deterministic placeholder


def test_draw_product_receipt_and_chain(tmp_path):
    store, photo, fg = _store_with_foreground(tmp_path)
    handle, receipt = composite.draw_product(store, fg, "a regal room")

    assert store.get_record(handle).kind == "composite"
    assert receipt["changed_opaque_pixels"] == 0
    assert receipt["edge_band_pixels"] == 4
    assert receipt["product_layer_scaled"] is False
    assert receipt["plate_source"] == "local_placeholder"
    assert receipt["source_asset"] == photo
    assert receipt["foreground_asset"] == fg
    assert receipt["foreground_sha256"] == store.get_record(fg).sha256
    assert receipt["foreground_pixel_sha256"] == store.get_record(fg).pixel_sha256
    assert receipt["mask_asset"] is None
    assert receipt["mask_source"] == "foreground_alpha"
    assert receipt["mask_sha256"]  # derived mask is still pinned by hash


def test_draw_product_uses_mask_asset_when_chained(tmp_path):
    store, photo, fg = _store_with_foreground(tmp_path, with_mask=True)
    _, receipt = composite.draw_product(store, fg, "a regal room")
    assert receipt["mask_source"] == "asset"
    assert receipt["mask_asset"] is not None


def test_draw_rejects_a_non_foreground_asset(tmp_path):
    store, photo, fg = _store_with_foreground(tmp_path)
    with pytest.raises(composite.FidelityLockError):
        composite.draw_product(store, photo, "a regal room")


def test_draw_rejects_foreground_without_source_photo_root(tmp_path):
    store = AssetStore(tmp_path)
    plate = store.put_image(_photo(), kind="background_plate")
    fg = store.put_image(
        _foreground(),
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(plate),
    )
    with pytest.raises(composite.FidelityLockError):
        composite.draw_product(store, fg, "a regal room")


def test_draw_rejects_a_tampered_foreground(tmp_path):
    store, photo, fg = _store_with_foreground(tmp_path)
    store.resolve(fg).write_bytes(b"tampered bytes, not the stored PNG")
    with pytest.raises(AssetIntegrityError):
        composite.draw_product(store, fg, "a regal room")
