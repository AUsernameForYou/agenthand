"""draw wired to muse-image-1.0: offline tests, mocked transport.

A fake client stands in for the OpenAI-compatible images transport; no
network is touched. The fixture sets a placeholder key and pins the
dotenv loader off, so the suite is hermetic in both directions: it
never reads the repo .env, never sees the real key, and — because
AGENTHAND_DATA_DIR points at a tmp dir — never writes the real ledger
(one test asserts exactly that against the real ledger file).

The placeholder plate is exercised too, but only through its explicit
dev flag (AGENTHAND_DRAW_PLACEHOLDER_PLATE): live mode never falls
back to it.
"""

import base64
import json
import os
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("AGENTHAND_DATA_DIR", "/tmp/agenthand-test-placeholder")

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from agenthand import composite, config, ledger, meta_client, server  # noqa: E402
from agenthand.assets import AssetStore  # noqa: E402

FAKE_KEY = "placeholder-test-key-not-a-real-key"
REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_LEDGER = REPO_ROOT / "data" / "ledger.jsonl"


@pytest.fixture(autouse=True)
def scratch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    # Live mode is this file's default: the placeholder flag must never
    # leak in from the ambient environment.
    monkeypatch.delenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", raising=False)
    # Pin the dotenv loader off: the repo .env (real key, for manual
    # smokes) must never leak into the suite, in either direction.
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


def _seed_foreground(store: AssetStore) -> str:
    """A 6x4 foreground: opaque red block, one edge column, transparent rest."""
    photo = store.put_image(
        Image.new("RGBA", (6, 4), (90, 90, 90, 255)), kind="source_photo"
    )
    fg_img = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    px = fg_img.load()
    for y in range(4):
        for x in range(3):
            px[x, y] = (255, 0, 0, 255)
        px[3, y] = (0, 255, 0, 128)
    return store.put_image(
        fg_img,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(photo),
    )


# -- live plate path ---------------------------------------------------


def test_draw_live_plate_is_priced_settled_and_pinned_in_the_receipt(monkeypatch):
    client = _install_fake(
        monkeypatch, _FakeClient([_image_response(b64_json=_png_b64())])
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    out = server.draw(fg, "a regal, warmly lit reading room")
    assert isinstance(out, list)
    result, preview = out
    assert result["status"] == "ok"

    # The plate was generated, stored as its own asset kind, and the
    # composite as composite chained to the foreground.
    plate_handle = result["plate_asset"]
    assert store.get_record(plate_handle).kind == "background_plate"
    composite_record = store.get_record(result["output_asset"])
    assert composite_record.kind == "composite"
    assert composite_record.source_asset_id == AssetStore.parse_handle(fg)

    # The fidelity receipt is computed from the REAL composite and pins
    # the plate alongside the foreground.
    receipt = result["fidelity_receipt"]
    assert receipt["changed_opaque_pixels"] == 0
    assert receipt["plate_source"] == "asset"
    assert receipt["plate_asset"] == plate_handle
    assert receipt["plate_sha256"] == store.get_record(plate_handle).sha256

    # The caller's scene words ride verbatim inside the fixed wrapper,
    # and the size requested is the closest supported to the product's
    # 6x4 (3:2) dimensions.
    (call,) = client.images.calls
    assert call["model"] == "muse-image-1.0"
    assert call["n"] == 1
    assert call["size"] == "1536x1024"
    assert call["prompt"].endswith("a regal, warmly lit reading room")
    assert "EMPTY" in call["prompt"] and "no product" in call["prompt"]

    # Priced at the per-image rate and settled with images=1.
    assert result["cost_usd"] == 0.01
    assert result["usage"]["images"] == 1
    records = _records("draw")
    assert [r["phase"] for r in records] == ["pending", "settled"]
    assert records[0]["usd_estimate"] == 0.01
    assert records[1]["usd_estimate"] == 0.01
    assert records[1]["images"] == 1
    assert records[1]["call_id"] == result["call_id"]

    from mcp.server.mcpserver import Image as MCPImage

    assert isinstance(preview, MCPImage)


def test_draw_live_composite_keeps_opaque_product_pixels_identical(monkeypatch):
    _install_fake(
        monkeypatch,
        _FakeClient([_image_response(b64_json=_png_b64(color=(30, 60, 160, 255)))]),
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    result, _ = server.draw(fg, "a blue studio backdrop")
    composite_img = store.read_image(result["output_asset"])
    px = composite_img.load()
    assert px[0, 0] == (255, 0, 0, 255)  # opaque product pixel, untouched
    assert px[5, 0] == (30, 60, 160, 255)  # transparent area: the plate
    assert composite_img.size == (6, 4)  # product dimensions, not the plate's


def test_generate_background_plate_stores_a_plate_asset(monkeypatch):
    _install_fake(
        monkeypatch, _FakeClient([_image_response(b64_json=_png_b64())])
    )
    store = AssetStore(config.get_data_dir())
    handle, billing = meta_client.generate_background_plate(
        "an empty warehouse", 800, 600, store=store
    )
    record = store.get_record(handle)
    assert record.kind == "background_plate"
    assert (record.width, record.height) == (16, 12)  # the returned plate
    assert billing["actual_usd"] == 0.01
    assert billing["usage"]["images"] == 1


def test_closest_image_size_picks_by_aspect_then_area():
    assert meta_client.closest_image_size(6, 4) == "1536x1024"
    assert meta_client.closest_image_size(4, 6) == "1024x1536"
    assert meta_client.closest_image_size(100, 100) == "1024x1024"
    assert meta_client.closest_image_size(None, None) == "1024x1024"
    assert meta_client.closest_image_size(4000, 1000) == "1792x1024"


# -- failure behavior ----------------------------------------------------


def test_draw_empty_image_response_is_a_structured_error_with_spend_counted(
    monkeypatch,
):
    _install_fake(monkeypatch, _FakeClient([SimpleNamespace(data=[])]))
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    result = server.draw(fg, "a regal room")
    assert isinstance(result, dict)  # error shape, not the [result, preview] list
    assert result["status"] == "error"
    assert result["error"]["code"] == "BAD_IMAGE_RESPONSE"
    assert result["error"]["cost_counted_usd"] == 0.01
    assert result["output_asset"] is None

    records = _records("draw")
    assert [r["phase"] for r in records] == ["pending"]  # reservation stands
    assert ledger.ledger_total_usd(config.get_ledger_path()) == 0.01


def test_draw_safety_refusal_response_shape(monkeypatch):
    refusal = _image_response(b64_json=None, url=None, refusal="content policy")
    _install_fake(monkeypatch, _FakeClient([refusal]))
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    result = server.draw(fg, "a scene the filter dislikes")
    assert result["status"] == "error"
    assert result["error"]["code"] == "IMAGE_REFUSED"
    assert "content policy" in result["error"]["message"]
    assert ledger.ledger_total_usd(config.get_ledger_path()) == 0.01


def test_draw_transport_failure_is_a_structured_error(monkeypatch):
    _install_fake(
        monkeypatch,
        _FakeClient([RuntimeError("HTTP 500 from the image endpoint")]),
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    result = server.draw(fg, "a regal room")
    assert result["status"] == "error"
    assert result["error"]["code"] == "IMAGE_CALL_FAILED"
    assert result["error"]["cost_counted_usd"] == 0.01
    assert ledger.ledger_total_usd(config.get_ledger_path()) == 0.01


def test_draw_missing_key_is_not_configured_and_bills_nothing(monkeypatch):
    monkeypatch.delenv("META_API_KEY")
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    result = server.draw(fg, "a regal room")
    assert result["status"] == "error"
    assert result["error"]["code"] == "NOT_CONFIGURED"
    assert _records() == []  # key check happens before billing


# -- the placeholder exists only behind the flag -------------------------


def test_draw_placeholder_plate_only_behind_the_dev_flag(monkeypatch):
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    # Flag on, key gone: the full local pipeline runs, no API call, $0.
    monkeypatch.delenv("META_API_KEY")
    monkeypatch.setenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", "1")
    result, _ = server.draw(fg, "a regal, warmly lit reading room")
    assert result["status"] == "ok"
    assert result["fidelity_receipt"]["plate_source"] == "local_placeholder"
    assert result["plate_asset"] is None
    assert result["cost_usd"] == 0.0
    assert _records("draw")[1]["status"] == "stub"


def test_draw_refuses_a_non_foreground_before_any_reservation_live(monkeypatch):
    client = _install_fake(
        monkeypatch, _FakeClient([_image_response(b64_json=_png_b64())])
    )
    store = AssetStore(config.get_data_dir())
    photo = store.put_image(
        Image.new("RGBA", (6, 4), (90, 90, 90, 255)), kind="source_photo"
    )
    with pytest.raises(composite.FidelityLockError):
        server.draw(photo, "a regal room")
    assert _records() == []  # refused before the gate: no reservation
    assert client.images.calls == []  # and the image model was never called


# -- hermeticity ---------------------------------------------------------


def test_mocked_draw_never_touches_the_real_ledger(monkeypatch):
    before = REAL_LEDGER.read_bytes() if REAL_LEDGER.exists() else None
    _install_fake(
        monkeypatch, _FakeClient([_image_response(b64_json=_png_b64())])
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    result, _ = server.draw(fg, "a regal room")
    assert result["status"] == "ok"

    after = REAL_LEDGER.read_bytes() if REAL_LEDGER.exists() else None
    assert after == before  # no sam/image (or any) entries leaked in
