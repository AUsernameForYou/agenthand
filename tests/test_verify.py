"""Independent verification (J2 Gauntlet must-fixes 2, 3, 5).

Two paths, both independent of the compositor's in-memory numbers:

- composite.verify_receipt_from_disk re-reads the stored files and
  re-derives every receipt number, so a receipt is no longer graded
  only by the code that produced it.
- agenthand.verify.verify_files (the agenthand-verify CLI) checks
  exported source/final/mask files with no store at all.

The adversarial fixtures are the lane's list: a tampered output file,
a 1-px shift, a JPEG re-encode, a plate swapped over the product, a
feathered mask, and a stripped colour profile whose pixels are
byte-identical (the profile attack: values match, colours change).
"""

import hashlib
from io import BytesIO

from PIL import Image, ImageCms

from agenthand import composite, segmentation, verify
from agenthand.assets import AssetStore


def _profile_bytes(kind: str = "XYZ") -> bytes:
    """A real, parseable ICC profile's bytes.

    Stands in for a camera's embedded wide-gamut profile (e.g. Display
    P3): what the pipeline must preserve is the byte identity, and
    what it must detect is any change to it.
    """
    return ImageCms.ImageCmsProfile(ImageCms.createProfile(kind)).tobytes()


def _pattern_photo(size=(8, 6)) -> Image.Image:
    """A photo with per-pixel varied colours (JPEG-hostile on purpose)."""
    img = Image.new("RGBA", size, (0, 0, 0, 255))
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = ((x * 37) % 256, (y * 53) % 256, ((x + y) * 29) % 256, 255)
    return img


def _block_mask(size=(8, 6)) -> Image.Image:
    """Binary mask over x in 1..4, y in 1..4 (16 product pixels)."""
    mask = Image.new("L", size, 0)
    px = mask.load()
    for y in range(1, 5):
        for x in range(1, 5):
            px[x, y] = 255
    return mask


def _export_run(tmp_path):
    """Build a real run and export source/final/mask as plain files.

    Returns (paths dict, expected checked-pixel count). The final is
    produced by the real extraction + compositing path, so a passing
    verify is evidence about the pipeline, not about a fixture faked
    to agree with itself.
    """
    photo = _pattern_photo()
    mask = _block_mask()
    foreground = segmentation.extract_foreground(photo, mask)
    plate = Image.new("RGBA", photo.size, (0, 0, 255, 255))
    final, _counts, _t = composite.composite_images(foreground, plate)
    paths = {}
    for name, img, mode in (
        ("source", photo, None),
        ("final", final, None),
        ("mask", mask, "L"),
    ):
        path = tmp_path / f"{name}.png"
        img.save(path, format="PNG")
        paths[name] = path
    return paths, 16


def _rewrite_png(path, mutate):
    with Image.open(path) as img:
        img.load()
        out = img.convert("RGBA")
    mutate(out)
    out.save(path, format="PNG")


# -- verify_files: the happy path and the pixel attacks -----------------


def test_verify_files_passes_on_a_real_run(tmp_path):
    paths, checked = _export_run(tmp_path)
    report = verify.verify_files(paths["source"], paths["final"], paths["mask"])
    assert report["ok"] is True
    assert report["checked_pixels"] == checked
    assert report["changed_pixels"] == 0
    assert report["color_profile_preserved"] is True
    assert report["problems"] == []


def test_verify_files_detects_a_one_pixel_shift(tmp_path):
    paths, _ = _export_run(tmp_path)
    final = Image.open(paths["final"]).convert("RGBA")
    shifted = Image.new("RGBA", final.size, (0, 0, 0, 255))
    shifted.paste(final.crop((1, 0) + final.size), (0, 0))
    shifted.save(paths["final"], format="PNG")

    report = verify.verify_files(paths["source"], paths["final"], paths["mask"])
    assert report["ok"] is False
    assert report["changed_pixels"] > 0


def test_verify_files_detects_a_jpeg_reencode(tmp_path):
    paths, _ = _export_run(tmp_path)
    final = Image.open(paths["final"]).convert("RGB")
    buffer = BytesIO()
    final.save(buffer, format="JPEG", quality=88)
    buffer.seek(0)
    Image.open(buffer).convert("RGBA").save(paths["final"], format="PNG")

    report = verify.verify_files(paths["source"], paths["final"], paths["mask"])
    assert report["ok"] is False
    assert report["changed_pixels"] > 0


def test_verify_files_detects_the_plate_swapped_over_the_product(tmp_path):
    paths, _ = _export_run(tmp_path)
    # The "final" is just the plate: every product pixel was replaced.
    Image.new("RGBA", (8, 6), (0, 0, 255, 255)).save(paths["final"], format="PNG")

    report = verify.verify_files(paths["source"], paths["final"], paths["mask"])
    assert report["ok"] is False
    assert report["changed_pixels"] == report["checked_pixels"] == 16


def test_verify_files_counts_the_edge_band_without_comparing_it(tmp_path):
    paths, _ = _export_run(tmp_path)
    # Feather the mask: one column at half strength is edge band —
    # counted separately, neither checked nor silently ignored.
    mask = Image.open(paths["mask"]).convert("L")
    px = mask.load()
    for y in range(1, 5):
        px[5, y] = 128
    mask.save(paths["mask"], format="PNG")

    report = verify.verify_files(paths["source"], paths["final"], paths["mask"])
    assert report["edge_band_pixels"] == 4
    assert report["checked_pixels"] == 16


def test_verify_cli_exit_codes(tmp_path):
    paths, _ = _export_run(tmp_path)
    args = [str(paths["source"]), str(paths["final"]), str(paths["mask"])]
    assert verify.main(args) == 0

    _rewrite_png(paths["final"], lambda img: img.putpixel((2, 2), (1, 2, 3, 255)))
    assert verify.main(args) == 1


# -- verify_files: the colour-profile attack ----------------------------


def test_verify_files_fails_on_a_stripped_profile_with_identical_pixels(tmp_path):
    # THE J2 attack: strip the ICC profile from the final. Every pixel
    # value stays byte-identical, so any pixel-only check passes —
    # verification must still fail, on the profile check alone.
    profile = _profile_bytes()
    photo = _pattern_photo()
    photo.info["icc_profile"] = profile
    mask = _block_mask()
    foreground = segmentation.extract_foreground(photo, mask)
    plate = Image.new("RGBA", photo.size, (0, 0, 255, 255))
    final, _counts, _t = composite.composite_images(foreground, plate)
    assert final.info.get("icc_profile") == profile  # the chain preserved it

    source_path = tmp_path / "source.png"
    photo.save(source_path, format="PNG", icc_profile=profile)
    final_path = tmp_path / "final.png"
    stripped = final.copy()
    stripped.info.pop("icc_profile", None)  # same pixels, NO profile written
    stripped.save(final_path, format="PNG")
    mask_path = tmp_path / "mask.png"
    mask.save(mask_path, format="PNG")

    report = verify.verify_files(source_path, final_path, mask_path)
    assert report["changed_pixels"] == 0  # pixels really are identical
    assert report["color_profile_preserved"] is False
    assert report["color_profile_source_sha256"] == hashlib.sha256(profile).hexdigest()
    assert report["color_profile_output_sha256"] is None
    assert report["ok"] is False


def test_verify_files_passes_when_the_profile_is_preserved(tmp_path):
    profile = _profile_bytes()
    photo = _pattern_photo()
    photo.info["icc_profile"] = profile
    mask = _block_mask()
    foreground = segmentation.extract_foreground(photo, mask)
    plate = Image.new("RGBA", photo.size, (0, 0, 255, 255))
    final, _counts, _t = composite.composite_images(foreground, plate)

    source_path = tmp_path / "source.png"
    photo.save(source_path, format="PNG", icc_profile=profile)
    final_path = tmp_path / "final.png"
    final.save(final_path, format="PNG", icc_profile=profile)
    mask_path = tmp_path / "mask.png"
    mask.save(mask_path, format="PNG")

    report = verify.verify_files(source_path, final_path, mask_path)
    assert report["ok"] is True
    assert report["color_profile_preserved"] is True


# -- verify_receipt_from_disk: the receipt, re-graded from the files ----


def _store_run(tmp_path, *, profiled=False):
    """A full draw through the store; returns (store, handle, receipt)."""
    store = AssetStore(tmp_path)
    photo = _pattern_photo()
    if profiled:
        photo.info["icc_profile"] = _profile_bytes()
    photo_handle = store.put_image(photo, kind="source_photo")
    foreground = segmentation.extract_foreground(photo, _block_mask())
    fg_handle = store.put_image(
        foreground,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(photo_handle),
    )
    handle, receipt = composite.draw_product(store, fg_handle, "a regal room")
    return store, handle, receipt


def test_verify_receipt_from_disk_passes_on_a_clean_run(tmp_path):
    store, handle, receipt = _store_run(tmp_path)
    report = composite.verify_receipt_from_disk(store, handle, receipt)
    assert report["ok"] is True, report["problems"]
    assert report["recomputed"]["changed_opaque_pixels"] == 0
    assert all(report["checks"].values())


def test_verify_receipt_from_disk_loads_the_persisted_receipt(tmp_path):
    # draw_product persists the receipt beside the composite, so the
    # verifier can be handed nothing but the composite handle.
    store, handle, receipt = _store_run(tmp_path)
    assert store.load_receipt(handle) == receipt
    report = composite.verify_receipt_from_disk(store, handle)
    assert report["ok"] is True, report["problems"]
    assert report["recomputed"]["changed_opaque_pixels"] == 0


def test_verify_receipt_from_disk_fails_on_a_tampered_output_file(tmp_path):
    store, handle, receipt = _store_run(tmp_path)
    # One pixel changed in the DELIVERED file, after the receipt was
    # issued. The receipt's own numbers still say 0 — the independent
    # recount from disk must refuse to agree.
    path = store.resolve(handle)
    _rewrite_png(path, lambda img: img.putpixel((2, 2), (9, 9, 9, 255)))

    report = composite.verify_receipt_from_disk(store, handle, receipt)
    assert report["ok"] is False
    assert report["checks"]["composite_file_integrity"] is False
    # The one-pixel NEGATIVE CONTROL (Grok): the recount itself must
    # see the tint — a counter that only ever reports 0 proves nothing
    # about whether it is wired to the delivered file.
    assert report["recomputed"]["changed_opaque_pixels"] == 1
    assert report["checks"]["recount_changed_opaque_pixels"] is False


def test_verify_files_refuses_an_empty_mask(tmp_path):
    # GPT's reproduction: two completely different images and a mask
    # that selects nothing verify "ok" today, because 0 of 0 checked
    # pixels changed. A pass that checked nothing proves nothing.
    paths, _ = _export_run(tmp_path)
    Image.new("RGBA", (8, 6), (250, 250, 250, 255)).save(
        paths["final"], format="PNG"
    )
    Image.new("L", (8, 6), 0).save(paths["mask"], format="PNG")

    report = verify.verify_files(paths["source"], paths["final"], paths["mask"])
    assert report["checked_pixels"] == 0
    assert report["ok"] is False
    assert any("no" in p and "checked" in p for p in report["problems"])


def test_verify_receipt_from_disk_fails_when_the_receipt_reports_a_change(tmp_path):
    # An HONEST violation receipt — the files really do differ at one
    # opaque pixel and the receipt says so (changed == 1, counts
    # matching the files) — must not verify ok. Before the hardening,
    # every recount check passed and ok came back True.
    store = AssetStore(tmp_path)
    photo = _pattern_photo()
    photo_handle = store.put_image(photo, kind="source_photo")
    foreground = segmentation.extract_foreground(photo, _block_mask())
    fg_handle = store.put_image(
        foreground,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(photo_handle),
    )
    handle, receipt = composite.draw_product(store, fg_handle, "a regal room")
    # Forge a violating composite + honest receipt by hand (draw never
    # produces one): flip one opaque pixel in a copy of the composite
    # and re-derive every count and hash from the tampered files.
    tampered = store.read_image(handle)
    tampered.putpixel((2, 2), (9, 9, 9, 255))
    bad_handle = store.put_image(
        tampered,
        kind="composite",
        source_asset_id=AssetStore.parse_handle(fg_handle),
    )
    bad_record = store.get_record(bad_handle)
    counts = composite._measure(store.read_image(fg_handle), tampered)
    assert counts["changed_opaque_pixels"] == 1
    bad_receipt = dict(receipt)
    bad_receipt.update(counts)
    bad_receipt["output_asset"] = bad_handle
    bad_receipt["output_sha256"] = bad_record.sha256
    bad_receipt["output_pixel_sha256"] = bad_record.pixel_sha256
    bad_receipt["color_profile_output_sha256"] = bad_record.color_profile_sha256
    store.save_receipt(bad_handle, bad_receipt)

    report = composite.verify_receipt_from_disk(store, bad_handle, bad_receipt)
    assert report["recomputed"]["changed_opaque_pixels"] == 1
    assert report["checks"]["zero_changed_pixels"] is False
    assert report["ok"] is False


def test_verify_receipt_from_disk_fails_for_receipt_a_against_composite_b(tmp_path):
    # Two clean runs, identical fixtures, ONE store: receipt A names
    # run A's assets, but the composite under test is run B's. The
    # numbers all reproduce (the fixtures are identical), so only
    # comparing the receipt's own output hash to the file catches
    # the swap.
    store = AssetStore(tmp_path)
    _h, receipt_a = None, None
    handles = []
    for _ in range(2):
        photo = _pattern_photo()
        photo_handle = store.put_image(photo, kind="source_photo")
        foreground = segmentation.extract_foreground(photo, _block_mask())
        fg_handle = store.put_image(
            foreground,
            kind="foreground_png",
            source_asset_id=AssetStore.parse_handle(photo_handle),
        )
        handle, receipt = composite.draw_product(store, fg_handle, "a regal room")
        handles.append((handle, receipt))
    (handle_a, receipt_a), (handle_b, _receipt_b) = handles
    assert handle_a != handle_b

    report = composite.verify_receipt_from_disk(store, handle_b, receipt_a)
    assert report["checks"]["output_hash_matches_receipt"] is False
    assert report["ok"] is False


def test_verify_receipt_from_disk_fails_on_an_edited_output_hash(tmp_path):
    store, handle, receipt = _store_run(tmp_path)
    edited = dict(receipt)
    edited["output_sha256"] = "0" * 64
    report = composite.verify_receipt_from_disk(store, handle, edited)
    assert report["checks"]["output_hash_matches_receipt"] is False
    assert report["ok"] is False

    edited = dict(receipt)
    edited["source_pixel_sha256"] = "0" * 64
    report = composite.verify_receipt_from_disk(store, handle, edited)
    assert report["checks"]["source_hash_matches_receipt"] is False
    assert report["ok"] is False


def test_verify_receipt_from_disk_fails_on_an_edited_mask_hash(tmp_path):
    # A run whose foreground is chained through its stored mask asset
    # (the production shape after fix 1): the receipt's mask pin must
    # be compared against the mask asset's record, not trusted.
    store = AssetStore(tmp_path)
    photo = _pattern_photo()
    photo_handle = store.put_image(photo, kind="source_photo")
    mask_handle = store.put_image(
        _block_mask(),
        kind="mask_png",
        source_asset_id=AssetStore.parse_handle(photo_handle),
    )
    foreground = segmentation.extract_foreground(photo, _block_mask())
    fg_handle = store.put_image(
        foreground,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(mask_handle),
    )
    handle, receipt = composite.draw_product(store, fg_handle, "a regal room")
    assert receipt["mask_source"] == "asset"

    clean = composite.verify_receipt_from_disk(store, handle, receipt)
    assert clean["ok"] is True, clean["problems"]

    edited = dict(receipt)
    edited["mask_pixel_sha256"] = "0" * 64
    report = composite.verify_receipt_from_disk(store, handle, edited)
    assert report["checks"]["mask_hash_matches_receipt"] is False
    assert report["ok"] is False


def test_verify_receipt_from_disk_fails_with_zero_opaque_coverage(tmp_path):
    # A fully transparent foreground: nothing is covered, nothing can
    # change, and a "pass" would vouch for zero product pixels.
    store = AssetStore(tmp_path)
    photo_handle = store.put_image(_pattern_photo(), kind="source_photo")
    empty_fg = Image.new("RGBA", (8, 6), (0, 0, 0, 0))
    fg_handle = store.put_image(
        empty_fg,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(photo_handle),
    )
    handle, receipt = composite.draw_product(store, fg_handle, "a regal room")
    assert receipt["opaque_pixels"] == 0

    report = composite.verify_receipt_from_disk(store, handle, receipt)
    assert report["checks"]["positive_coverage"] is False
    assert report["ok"] is False


def test_verify_receipt_from_disk_fails_on_a_stripped_output_profile(tmp_path):
    store, handle, receipt = _store_run(tmp_path, profiled=True)
    assert receipt["color_profile_preserved"] is True
    clean = composite.verify_receipt_from_disk(store, handle, receipt)
    assert clean["ok"] is True, clean["problems"]

    # Rewrite the delivered file with identical pixels but no ICC
    # profile. Pixel recount still finds 0 changed; the profile check
    # is what fails.
    path = store.resolve(handle)
    with Image.open(path) as img:
        img.load()
        stripped = img.convert("RGBA")
    stripped.info.pop("icc_profile", None)
    stripped.save(path, format="PNG")

    report = composite.verify_receipt_from_disk(store, handle, receipt)
    assert report["ok"] is False
    assert report["checks"]["color_profile_preserved"] is False
    assert report["recomputed"]["color_profile_preserved"] is False
