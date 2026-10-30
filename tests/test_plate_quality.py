import base64
from pathlib import Path
import numpy as np
import pytest
from PIL import Image, ImageDraw
from agenthand import meta_client, composite, server, config, ledger
from agenthand.plate_quality import assess_plate
from agenthand.assets import AssetStore, KIND_SOURCE, KIND_FOREGROUND
from test_draw import _FakeClient, _image_response, _install_fake

FIXTURES = Path(__file__).parent / "fixtures" / "wonder"


@pytest.mark.parametrize(
    "name,grade", [("plate-v1-podium.png", "FAIL"), ("plate-v3.png", "PASS")]
)
def test_supplied_plate_fixtures(name, grade):
    quality = assess_plate(
        Image.open(FIXTURES / name), [1563, 863, 4419, 3009], (5712, 4284)
    )
    assert quality["grade"] == grade, quality


def test_stage_spec_is_constrained_and_versioned():
    spec = meta_client.StageSpec.from_scene("a warm room with concrete floor")
    assert spec.floor == "matte concrete"
    assert meta_client.wrap_plate_prompt(spec) == meta_client.wrap_plate_prompt(
        "warm concrete room"
    )
    assert "where an object will be placed" not in meta_client.wrap_plate_prompt(spec)
    with pytest.raises(ValueError):
        meta_client.StageSpec(floor="podium")


def test_central_object_interrupts_floor():
    image = Image.new("RGB", (640, 480), "white")
    d = ImageDraw.Draw(image)
    d.rectangle((0, 260, 640, 480), fill=(180, 165, 140))
    d.rectangle((260, 290, 380, 400), fill=(30, 30, 30))
    q = assess_plate(image, [180, 80, 470, 350], image.size)
    assert q["grade"] == "FAIL", q
    assert (
        "CENTRAL_OBJECT" in q["codes"]
        or "RAISED_SLAB_OR_INTERRUPTED_FLOOR" in q["codes"]
    )


def seed(store):
    source = Image.new("RGBA", (600, 450), (180, 180, 180, 255))
    sh = store.put_image(source, kind=KIND_SOURCE)
    fg = Image.new("RGBA", source.size)
    fg.paste(
        source,
        (0, 0),
        Image.fromarray(
            np.pad(np.full((230, 320), 255, np.uint8), ((85, 135), (150, 130)))
        ),
    )
    return store.put_image(
        fg, kind=KIND_FOREGROUND, source_asset_id=store.parse_handle(sh)
    )


@pytest.mark.parametrize(
    "second,fallback", [("plate-v3.png", False), ("plate-v1-podium.png", True)]
)
def test_two_billed_attempts_then_accepted_plate_or_disclosed_fallback(
    tmp_path, monkeypatch, second, fallback
):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("META_API_KEY", "test-not-real")
    monkeypatch.delenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", raising=False)
    monkeypatch.setattr(config, "_DOTENV_LOADED", True)
    responses = [
        _image_response(b64_json=base64.b64encode((FIXTURES / n).read_bytes()).decode())
        for n in ("plate-v1-podium.png", second)
    ]
    client = _install_fake(monkeypatch, _FakeClient(responses))
    store = AssetStore(tmp_path)
    fh = seed(store)
    result, _ = server.draw(fh, "soft daylight wood room")
    assert result["status"] == "ok"
    assert len(client.images.calls) == 2
    q = result["fidelity_receipt"]["plate_quality"]
    assert q["fallback"] is fallback and len(q["attempts"]) == 2
    assert q["assessment"]["grade"] == "PASS"
    assert result["usage"]["images"] == 2
    assert result["cost_usd"] == 2 * meta_client.project_draw_cost_usd("muse-image-1.0")
    assert ledger.ledger_total_usd(config.get_ledger_path()) == result["cost_usd"]
    if fallback:
        assert result["fidelity_receipt"]["plate_kind"] == "code_rendered"
        assert q["fallback_reason"] == "two_rejected_plates"
    report = composite.verify_receipt_from_disk(store, result["output_asset"])
    assert report["ok"], report["problems"]
