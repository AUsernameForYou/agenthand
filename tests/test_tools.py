"""Tools: stub shapes, receipts through the choke point, draw end to end.

AGENTHAND_DATA_DIR is pointed at a tmp dir per test; config reads the
environment at call time, so every receipt these tests trigger lands in
the tmp ledger, never the real one.
"""

import base64
import json
import os
from io import BytesIO

os.environ.setdefault("AGENTHAND_DATA_DIR", "/tmp/agenthand-test-placeholder")

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from agenthand import composite, config, ledger, server  # noqa: E402
from agenthand.assets import AssetStore  # noqa: E402


@pytest.fixture(autouse=True)
def scratch_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    yield tmp_path


def _ledger_records():
    path = config.get_ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _data_uri(size=(6, 4), color=(10, 120, 200, 255)) -> str:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _seed_foreground(store: AssetStore) -> str:
    photo = store.put_image(
        Image.new("RGBA", (6, 4), (90, 90, 90, 255)), kind="source_photo"
    )
    fg_img = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    px = fg_img.load()
    for y in range(4):
        for x in range(3):
            px[x, y] = (255, 0, 0, 255)
    return store.put_image(
        fg_img,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(photo),
    )


def test_identify_without_key_returns_not_configured_and_bills_nothing(monkeypatch):
    # No META_API_KEY anywhere: identify is wired now, so instead of a
    # stub result it returns a structured NOT_CONFIGURED error — and the
    # key check happens before billing, so the ledger stays empty.
    monkeypatch.delenv("META_API_KEY", raising=False)
    result = server.identify(_data_uri())
    assert result["status"] == "error"
    assert result["error"]["code"] == "NOT_CONFIGURED"
    assert result["concept"] is None
    assert result["alternates"] == []
    # The photo was still ingested: the result carries a real handle.
    assert result["image"].startswith("asset://")
    AssetStore(config.get_data_dir()).get_record(result["image"])
    assert _ledger_records() == []


def test_cut_out_with_concept_stub_shape_and_receipt():
    result = server.cut_out(_data_uri(), "brown leather wallet")
    assert result["status"] == "not_yet_wired"
    assert result["concept"] == "brown leather wallet"
    assert result["concept_source"] == "provided"
    assert result["output_asset"] is None

    records = _ledger_records()
    assert [r["phase"] for r in records] == ["pending", "settled"]
    assert records[0]["tool"] == "cut_out"
    assert records[0]["model"] == "sam-3.1"


def test_cut_out_without_concept_plans_internally():
    result = server.cut_out(_data_uri())
    assert result["concept_source"] == "identify_internal"
    # The internal planning step is the (stubbed) identify logic: no
    # concept yet, but the plumbing path is the one the wired tool uses.
    assert result["concept"] is None
    assert result["alternates"] == []


def test_draw_runs_the_real_pipeline_with_a_placeholder_plate():
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store)

    result, preview = server.draw(fg, "a regal, warmly lit reading room")
    assert result["status"] == "ok"
    assert result["output_asset"] is not None
    assert store.get_record(result["output_asset"]).kind == "composite"

    receipt = result["fidelity_receipt"]
    assert receipt["changed_opaque_pixels"] == 0
    assert receipt["foreground_asset"] == fg
    assert receipt["plate_source"] == "local_placeholder"

    # The result carries an image preview alongside the structured part.
    from mcp.server.mcpserver import Image as MCPImage

    assert isinstance(preview, MCPImage)

    records = _ledger_records()
    assert [r["phase"] for r in records] == ["pending", "settled"]
    assert records[0]["tool"] == "draw"
    assert records[0]["model"] == "muse-image-1.0"


def test_draw_rejects_a_non_foreground_before_billing():
    store = AssetStore(config.get_data_dir())
    photo = store.put_image(
        Image.new("RGBA", (6, 4), (90, 90, 90, 255)), kind="source_photo"
    )
    with pytest.raises(composite.FidelityLockError):
        server.draw(photo, "a regal room")
    assert _ledger_records() == []  # refused before the gate: no reservation


def test_stub_calls_refuse_when_ledger_already_over_cap():
    # Seed the ledger past the cap; even $0 stub calls must refuse, because
    # the choke point checks before the (future) billable call.
    path = config.get_ledger_path()
    ledger.append_receipt(
        path, ledger.Receipt(tool="identify", model="muse-wonder-1.0", usd_estimate=200.00)
    )
    with pytest.raises(ledger.SpendCapExceeded):
        server.cut_out(_data_uri(), "brown leather wallet")
