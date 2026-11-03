"""Scene guard (Dan's rule, 2026-11-03; tracker event 631): draw's
scene text describes the environment only. A scene that names the
product — the concept `cut_out` recorded in the mask chunk — is
refused BEFORE any spend, because naming the product makes the plate
model paint its own copy of it. The outcome is recorded on the
persisted receipt and the result as `scene_guard`.

Offline, on test_draw.py's seams: a fake client stands in for the
images transport (its call list proves the plate call was never
reached) and the ledger is read back directly.
"""

import base64
import json
import os
from io import BytesIO
from types import SimpleNamespace

os.environ.setdefault("AGENTHAND_DATA_DIR", "/tmp/agenthand-test-placeholder")

import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from PIL.PngImagePlugin import PngInfo  # noqa: E402

from agenthand import composite, config, meta_client, sceneguard, server  # noqa: E402
from agenthand.assets import MASK_PROVENANCE_PNG_KEY, AssetStore  # noqa: E402

FAKE_KEY = "placeholder-test-key-not-a-real-key"
CONCEPT = "blue kids BMX bike"


@pytest.fixture(autouse=True)
def scratch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    monkeypatch.delenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", raising=False)
    monkeypatch.setattr(config, "_DOTENV_LOADED", True)
    return tmp_path


def _records(tool=None):
    path = config.get_ledger_path()
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    return [r for r in records if tool is None or r["tool"] == tool]


def _png_b64(size=(16, 12), color=(30, 60, 160, 255)) -> str:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _image_response(**item_kwargs):
    return SimpleNamespace(data=[SimpleNamespace(**item_kwargs)])


class _FakeImages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self._scripted:
            raise AssertionError("fake transport ran out of scripted responses")
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, image_scripted=()):
        self.images = _FakeImages(image_scripted)


def _install_fake(monkeypatch, client: _FakeClient) -> _FakeClient:
    monkeypatch.setattr(meta_client, "build_client", lambda: client)
    return client


def _photo_and_foreground(store: AssetStore, parent_handle: str) -> str:
    """The test_draw seed shape: opaque red block, one soft edge
    column, transparent rest; the foreground's visible pixels ARE
    the photo's."""
    fg_img = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    px = fg_img.load()
    for y in range(4):
        for x in range(3):
            px[x, y] = (255, 0, 0, 255)
        px[3, y] = (0, 255, 0, 128)
    return store.put_image(
        fg_img,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(parent_handle),
    )


def _seed_foreground_with_concept(store: AssetStore, concept: str = CONCEPT) -> str:
    """source -> mask -> foreground, as cut_out leaves them: the
    mask's pixels equal the foreground's alpha (preflight checks
    exactly that) and its PNG chunk records the frame fields plus
    the concept."""
    photo_img = Image.new("RGBA", (6, 4), (90, 90, 90, 255))
    px = photo_img.load()
    for y in range(4):
        for x in range(3):
            px[x, y] = (255, 0, 0, 255)
        px[3, y] = (0, 255, 0, 255)
    photo = store.put_image(photo_img, kind="source_photo")

    mask_img = Image.new("L", (6, 4), 0)
    mpx = mask_img.load()
    for y in range(4):
        for x in range(3):
            mpx[x, y] = 255
        mpx[3, y] = 128
    pnginfo = PngInfo()
    chunk = {
        "concept": concept,
        "sam_height": 4,
        "sam_width": 6,
        "scale_method": "none",
        "source_height": 4,
        "source_width": 6,
    }
    pnginfo.add_text(
        MASK_PROVENANCE_PNG_KEY,
        json.dumps(chunk, separators=(",", ":"), sort_keys=True),
    )
    buffer = BytesIO()
    mask_img.save(buffer, format="PNG", pnginfo=pnginfo)
    mask = store.put_bytes(
        buffer.getvalue(),
        kind="mask_png",
        suffix=".png",
        source_asset_id=AssetStore.parse_handle(photo),
    )
    return _photo_and_foreground(store, mask)


def _seed_foreground_without_mask(store: AssetStore) -> str:
    photo_img = Image.new("RGBA", (6, 4), (90, 90, 90, 255))
    px = photo_img.load()
    for y in range(4):
        for x in range(3):
            px[x, y] = (255, 0, 0, 255)
        px[3, y] = (0, 255, 0, 255)
    photo = store.put_image(photo_img, kind="source_photo")
    return _photo_and_foreground(store, photo)


# -- the pure check (mirrors the Rust unit tests) -------------------------


def test_head_noun_in_the_scene_is_refused_and_named():
    guard = sceneguard.check_scene(CONCEPT, "a sunny porch with the bike leaning by the door")
    assert guard.is_refusal
    assert guard.offending_term == "bike"
    assert guard.concept == CONCEPT
    assert guard.checked_terms == ["blue kids bmx bike", "bike"]
    message = guard.refusal_message()
    assert '"bike"' in message
    assert "Describe the environment only" in message


def test_the_full_phrase_is_refused_and_named_whole():
    guard = sceneguard.check_scene(CONCEPT, "stage the blue kids BMX bike indoors, warm light")
    assert guard.is_refusal
    assert guard.offending_term == CONCEPT


def test_matching_is_case_insensitive_and_plural_aware():
    assert sceneguard.check_scene("leather satchel", "A SATCHEL on a table").is_refusal
    assert sceneguard.check_scene("leather satchel", "two satchels on a shelf").is_refusal


def test_a_head_noun_inside_a_longer_word_does_not_fire():
    assert sceneguard.check_scene(CONCEPT, "a biker bar, neon light").status == "pass"
    assert sceneguard.check_scene("kids bike", "the kids biker gang mural").status == "pass"


def test_environment_only_scenes_pass():
    for scene in [
        "a quiet room",
        "a bright, calm room with a plain warm-white wall and a light wooden table surface, soft daylight",
        "weathered wooden porch boards, a fence behind, golden hour",
        "an empty angled stone shelf, bare stone, mossy walls",
    ]:
        guard = sceneguard.check_scene(CONCEPT, scene)
        assert guard.status == "pass", scene
        record = guard.record()
        assert record["status"] == "pass"
        assert record["concept"] == CONCEPT


def test_no_concept_skips_with_a_reason():
    guard = sceneguard.check_scene(None, "a room with a bike in it")
    assert guard.status == "skipped"
    assert not guard.is_refusal
    record = guard.record()
    assert record["concept"] is None
    assert "no concept" in record["reason"]


def test_single_word_concepts_check_the_word():
    guard = sceneguard.check_scene("fedora", "a fedora on a hook")
    assert guard.is_refusal
    assert guard.offending_term == "fedora"
    assert guard.checked_terms == ["fedora"]


# -- the wiring ------------------------------------------------------------


def test_mask_concept_of_reads_the_chunk():
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground_with_concept(store)
    assert composite.mask_concept_of(store, fg) == CONCEPT
    bare = _seed_foreground_without_mask(store)
    assert composite.mask_concept_of(store, bare) is None


def test_draw_refuses_a_scene_naming_the_product_before_any_call(monkeypatch):
    client = _install_fake(
        monkeypatch, _FakeClient([_image_response(b64_json=_png_b64())])
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground_with_concept(store)

    result = server.draw(fg, "a sunny porch with the bike leaning by the door")
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["output_asset"] is None
    error = result["error"]
    assert error["code"] == "SCENE_NAMES_PRODUCT"
    assert error["cost_counted_usd"] == 0.0
    assert error["call_made"] is False
    assert '"bike"' in error["message"]
    guard = error["scene_guard"]
    assert guard["status"] == "refused"
    assert guard["concept"] == CONCEPT
    assert guard["offending_term"] == "bike"
    # The plate call was never reached and nothing was billed.
    assert client.images.calls == []
    assert _records() == []


def test_draw_clean_scene_records_the_guard_pass_on_result_and_receipt(monkeypatch):
    _install_fake(monkeypatch, _FakeClient([_image_response(b64_json=_png_b64())]))
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground_with_concept(store)

    result, _ = server.draw(fg, "a regal, warmly lit reading room")
    assert result["status"] == "ok"
    guard = result["scene_guard"]
    assert guard["status"] == "pass"
    assert guard["concept"] == CONCEPT
    assert guard["checked_terms"] == ["blue kids bmx bike", "bike"]

    persisted = store.load_receipt(result["output_asset"])
    assert persisted is not None
    assert persisted["scene_guard"] == guard
    # The additive field does not disturb the independent verifier.
    report = composite.verify_receipt_from_disk(store, result["output_asset"])
    assert report["ok"] is True, report["problems"]


def test_draw_without_a_concept_draws_with_the_guard_skipped(monkeypatch):
    _install_fake(monkeypatch, _FakeClient([_image_response(b64_json=_png_b64())]))
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground_without_mask(store)

    # Even a scene naming a product draws: with no concept recorded
    # there is nothing honest to check against, and the record says
    # so instead of claiming a pass.
    result, _ = server.draw(fg, "a room with a bike in it")
    assert result["status"] == "ok"
    guard = result["scene_guard"]
    assert guard["status"] == "skipped"
    assert guard["concept"] is None
    assert "no concept" in guard["reason"]
