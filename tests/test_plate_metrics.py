"""Plate metrics (decision 152) and placement (decision 153),
mirroring the Rust build: cut_out measures how the source photo
was taken and stores the measurement in the mask chunk; draw
composes the plate with system-owned metric clauses, translates
the product layer to the measured anchor, and gates the finished
composite against the measurement — one strengthened retry, then
an honest PLATE_GEOMETRY_MISMATCH refusal.

Offline, on test_draw.py's seams: a fake client stands in for both
transports (images for plates, chat for the metrics reads), so the
full gate loop runs with no network. The pure decisions
(parse / clauses / conflicts / compare / placement / grounding)
are covered directly, mirroring the Rust module's unit tests.
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

from agenthand import composite, config, meta_client, platemetrics, server  # noqa: E402
from agenthand.assets import MASK_PROVENANCE_PNG_KEY, AssetStore  # noqa: E402

FAKE_KEY = "placeholder-test-key-not-a-real-key"

MEASURED = {
    "camera_elevation": "high",
    "camera_azimuth": "front-left",
    "light_direction": "left",
    "ground_line": 0.82,
    "product_frame_share": 0.5,
    "surface_texture_period": 0.1,
    "surface_quality": "everyday",
}

MATCHING = {
    "camera_elevation": "high",
    "camera_azimuth": "front",
    "light_direction": "left",
    "ground_line": 0.8,
    "product_frame_share": 0.5,
    "surface_texture_period": 0.1,
    "surface_quality": "everyday",
}

MISMATCHING = {
    "camera_elevation": "eye-level",
    "camera_azimuth": "front",
    "light_direction": "left",
    "ground_line": 0.55,
    "product_frame_share": 0.5,
    "surface_texture_period": 0.1,
    "surface_quality": "everyday",
}


@pytest.fixture(autouse=True)
def scratch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    monkeypatch.delenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", raising=False)
    monkeypatch.setattr(config, "_DOTENV_LOADED", True)
    return tmp_path


def _chat_response(payload: dict):
    usage = SimpleNamespace(
        prompt_tokens=900,
        completion_tokens=60,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    message = SimpleNamespace(content=json.dumps(payload))
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _png_b64(size=(16, 12), color=(30, 60, 160, 255)) -> str:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class _FakeCalls:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake transport ran out of scripted responses")
        return self._responses.pop(0)


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
    def __init__(self, image_scripted=(), chat_responses=()):
        self.images = _FakeImages(image_scripted)
        self.completions = _FakeCalls(chat_responses)
        self.chat = SimpleNamespace(completions=self.completions)


def _install_fake(monkeypatch, client: _FakeClient) -> _FakeClient:
    monkeypatch.setattr(meta_client, "build_client", lambda: client)
    return client


def _plate_response():
    return SimpleNamespace(data=[SimpleNamespace(b64_json=_png_b64())])


def _seed_foreground(store: AssetStore, plate_metrics_chunk: dict | None) -> str:
    """source -> mask -> foreground, as cut_out leaves them: the
    mask's pixels equal the foreground's alpha and its PNG chunk
    records the frame fields, the concept, and (optionally) the
    plate_metrics record. The product block sits at the frame's
    left edge, full height — so placement must centre it."""
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
        "concept": "orange suitcase",
        "sam_height": 4,
        "sam_width": 6,
        "scale_method": "none",
        "source_height": 4,
        "source_width": 6,
    }
    if plate_metrics_chunk is not None:
        chunk["plate_metrics"] = plate_metrics_chunk
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
    fg_img = Image.new("RGBA", (6, 4), (0, 0, 0, 0))
    px = fg_img.load()
    for y in range(4):
        for x in range(3):
            px[x, y] = (255, 0, 0, 255)
        px[3, y] = (0, 255, 0, 128)
    return store.put_image(
        fg_img,
        kind="foreground_png",
        source_asset_id=AssetStore.parse_handle(mask),
    )


def _measured_chunk() -> dict:
    return {"status": "measured", **MEASURED}


# -- the pure module (mirrors the Rust unit tests) -------------------------


def _metrics(elevation=None, light=None, ground=None, share=None):
    return platemetrics.PlateMetrics(
        camera_elevation=elevation,
        camera_azimuth="front",
        light_direction=light,
        ground_line=ground,
        product_frame_share=share,
    )


def test_a_full_answer_parses_field_by_field():
    parsed = platemetrics.parse_metrics_payload(
        '{"camera_elevation":"high","camera_azimuth":"front-left",'
        '"light_direction":"left","ground_line":0.78,'
        '"product_frame_share":0.31,"surface_texture_period":0.12,'
        '"surface_quality":"premium"}'
    )
    assert parsed.camera_elevation == "high"
    assert parsed.camera_azimuth == "front-left"
    assert parsed.light_direction == "left"
    assert parsed.ground_line == 0.78
    assert parsed.product_frame_share == 0.31
    assert parsed.surface_texture_period == 0.12
    assert parsed.surface_quality == "premium"
    # A pre-texture record (five fields) still parses, with the new
    # fields unknown rather than invented.
    older = platemetrics.parse_metrics_payload(
        '{"camera_elevation":"high","camera_azimuth":"front",'
        '"light_direction":"left","ground_line":0.78,'
        '"product_frame_share":0.31}'
    )
    assert older.surface_texture_period is None
    assert older.surface_quality is None


def test_unknowns_and_bad_values_become_explicit_unknowns():
    parsed = platemetrics.parse_metrics_payload(
        '{"camera_elevation":"unknown","camera_azimuth":"somewhere",'
        '"light_direction":"LEFT","ground_line":1.4,'
        '"product_frame_share":null}'
    )
    assert parsed.camera_elevation is None
    assert parsed.camera_azimuth is None
    # Case is normalized; the value itself is a measurement.
    assert parsed.light_direction == "left"
    # Out-of-range fractions are not measurements.
    assert parsed.ground_line is None
    assert parsed.product_frame_share is None
    record = parsed.to_json()
    assert record["camera_elevation"] is None
    assert record["ground_line"] is None


def test_a_non_object_answer_is_an_error_not_a_measurement():
    with pytest.raises(ValueError):
        platemetrics.parse_metrics_payload("the photo looks fine")
    with pytest.raises(ValueError):
        platemetrics.parse_metrics_payload("[1,2,3]")


def test_clauses_carry_only_what_was_measured():
    clauses = platemetrics.clauses_for(_metrics("high", "left", 0.8, None))
    text = platemetrics.clause_text(clauses, False)
    assert "standing height looking steeply down" in text
    assert "key light from the left side of the frame" in text
    assert "about 80% of the frame height" in text
    assert "overrides any other viewpoint" in text
    empty = platemetrics.clauses_for(platemetrics.PlateMetrics.unknown())
    assert platemetrics.clause_text(empty, False) is None


def test_the_retry_clause_is_the_same_measurement_worded_mandatory():
    clauses = platemetrics.clauses_for(_metrics("top-down", None, None, None))
    normal = platemetrics.clause_text(clauses, False)
    strong = platemetrics.clause_text(clauses, True)
    assert "mandatory" in strong
    assert "directly overhead" in strong
    assert "mandatory" not in normal


def test_scene_conflicts_name_only_contradictions():
    m = _metrics("high", "left", None, None)
    assert platemetrics.scene_conflicts(m, "an eye-level shot of a calm room") == [
        "eye-level"
    ]
    assert platemetrics.scene_conflicts(
        m, "a room seen from below, lit from the right"
    ) == ["from below", "light from the right"]
    assert (
        platemetrics.scene_conflicts(m, "a porch at golden hour, looking down the boards")
        == []
    )


def test_the_gate_compares_by_family_and_contradiction():
    measured = _metrics("high", "left", 0.82, 0.5)
    same_family = _metrics("top-down", "left", 0.8, 0.5)
    assert platemetrics.compare(measured, same_family).is_pass()
    cross_family = _metrics("eye-level", "left", 0.8, 0.5)
    comparison = platemetrics.compare(measured, cross_family)
    assert not comparison.is_pass()
    assert any(m.startswith("camera_elevation") for m in comparison.mismatches)
    # An unreadable plate angle is a mismatch, never a silent pass.
    unreadable = _metrics(None, "left", 0.8, 0.5)
    assert not platemetrics.compare(measured, unreadable).is_pass()
    # Front vs overhead light washes a still the same way: a note.
    soft = _metrics("high", "overhead", 0.8, 0.5)
    measured_front = _metrics("high", "front", 0.82, 0.5)
    comparison = platemetrics.compare(measured_front, soft)
    assert comparison.is_pass()
    assert any(n.startswith("light_direction") for n in comparison.notes)


def test_the_gate_fails_oversized_texture_and_notes_a_downgrade():
    measured = _metrics("high", "left", 0.82, 0.3)
    measured.surface_quality = "premium"
    observed = _metrics("high", "left", 0.8, 0.3)
    observed.surface_texture_period = 0.35
    observed.surface_quality = "worn"
    comparison = platemetrics.compare(measured, observed)
    assert not comparison.is_pass()
    assert any(
        m.startswith("surface_texture_scale") for m in comparison.mismatches
    )
    assert any(n.startswith("surface_quality") for n in comparison.notes)


def test_placement_centres_the_product_on_the_measured_ground_line():
    m = _metrics("high", None, 0.8, None)
    plan = platemetrics.plan_placement(1000, 800, (100, 500, 400, 600), m)
    assert plan.dx == 250  # bbox centre 250 -> frame centre 500
    assert plan.dy == 40  # base 600 -> 0.8 * 800 = 640
    assert plan.applied
    assert plan.anchor == "centered_on_measured_ground_line"
    m.ground_line = None
    plan = platemetrics.plan_placement(1000, 800, (100, 500, 400, 600), m)
    assert (plan.dx, plan.dy) == (250, 0)
    assert plan.anchor == "centered"
    # A frame-filling product cannot move at all.
    plan = platemetrics.plan_placement(1000, 800, (0, 0, 1000, 800), m)
    assert (plan.dx, plan.dy) == (0, 0)
    assert not plan.applied


def test_grounding_follows_the_source_box_height():
    high = platemetrics.grounding_assessment(0.33)
    assert high["status"] == "at_risk"
    assert high["anchor"] == "high"
    assert platemetrics.grounding_assessment(0.85)["anchor"] == "low"
    assert platemetrics.grounding_assessment(0.52)["anchor"] == "central"
    assert platemetrics.grounding_assessment(None)["status"] == "unknown"


def test_the_refusal_names_the_fix():
    measured = _metrics("high", "left", 0.82, 0.5)
    message = platemetrics.refusal_message(
        measured, ['camera_elevation: measured "high", plate "eye-level"']
    )
    assert "already retried once" in message
    assert "from standing height, looking down" in message
    assert "call draw again" in message


# -- wiring: the placeholder seam ------------------------------------------


def test_a_placeholder_draw_republishes_the_measurement_and_marks_the_gate_skipped(
    monkeypatch, scratch_env
):
    monkeypatch.setenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", "1")
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store, _measured_chunk())
    drawn, _preview = server.draw(fg, "a calm room")
    assert drawn["status"] == "ok"
    record = drawn["plate_metrics"]
    assert record["status"] == "skipped"
    assert record["measured"]["camera_elevation"] == "high"
    assert record["measured"]["ground_line"] == 0.82
    assert record["measured"]["surface_texture_period"] == 0.1
    assert record["gate"]["status"] == "skipped"
    assert "placeholder" in record["gate"]["reason"]
    # The fixture's resting line sits at the frame's bottom edge:
    # a low anchor, groundable.
    assert record["grounding"]["anchor"] == "low"
    # The persisted receipt carries the same record.
    persisted = store.load_receipt(drawn["output_asset"])
    assert persisted["plate_metrics"] == record


def test_a_mask_without_metrics_draws_with_the_record_unmeasured(
    monkeypatch, scratch_env
):
    monkeypatch.setenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", "1")
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store, None)
    drawn, _preview = server.draw(fg, "a calm room")
    assert drawn["status"] == "ok"
    assert drawn["plate_metrics"]["measured"] is None


# -- wiring: the live gate loop on the fake transport -----------------------


def test_the_gate_passes_a_matching_plate_and_the_prompt_carries_the_clauses(
    monkeypatch, scratch_env
):
    client = _install_fake(
        monkeypatch,
        _FakeClient(
            image_scripted=[_plate_response()],
            chat_responses=[_chat_response(MATCHING)],
        ),
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store, _measured_chunk())
    drawn, _preview = server.draw(fg, "a calm room")
    assert drawn["status"] == "ok", drawn
    record = drawn["plate_metrics"]
    assert record["status"] == "enforced"
    assert record["gate"]["status"] == "pass"
    assert len(record["gate"]["attempts"]) == 1
    # The plate prompt carried the system-owned clauses, last.
    assert len(client.images.calls) == 1
    prompt = client.images.calls[0]["prompt"]
    assert "Measured plate geometry" in prompt
    assert "standing height looking steeply down" in prompt
    # Placement: the product's opaque box (x 0..3 on a 6-wide
    # frame) is centred by +2; the receipt pins the offset and the
    # placed layer, and independent verification recounts there
    # and passes.
    receipt = drawn["fidelity_receipt"]
    assert receipt["placement"]["dx"] == 2
    assert receipt["placement"]["dy"] == 0
    assert receipt["changed_opaque_pixels"] == 0
    report = composite.verify_receipt_from_disk(
        store, drawn["output_asset"], receipt
    )
    assert report["ok"], report


def test_the_gate_retries_once_with_mandatory_clauses_then_passes(
    monkeypatch, scratch_env
):
    client = _install_fake(
        monkeypatch,
        _FakeClient(
            image_scripted=[_plate_response(), _plate_response()],
            chat_responses=[_chat_response(MISMATCHING), _chat_response(MATCHING)],
        ),
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store, _measured_chunk())
    drawn, _preview = server.draw(fg, "a calm room")
    assert drawn["status"] == "ok", drawn
    record = drawn["plate_metrics"]
    assert record["gate"]["status"] == "pass_after_retry"
    assert len(record["gate"]["attempts"]) == 2
    assert len(client.images.calls) == 2
    # The retry's prompt is the same measurement, worded mandatory.
    assert "mandatory" in client.images.calls[1]["prompt"]
    # Both plates' spend is on the result.
    assert drawn["cost_usd"] >= 0.02


def test_the_gate_refuses_a_second_mismatch_honestly(monkeypatch, scratch_env):
    client = _install_fake(
        monkeypatch,
        _FakeClient(
            image_scripted=[_plate_response(), _plate_response()],
            chat_responses=[
                _chat_response(MISMATCHING),
                _chat_response(MISMATCHING),
            ],
        ),
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store, _measured_chunk())
    result = server.draw(fg, "a calm room")
    assert result["status"] == "error"
    error = result["error"]
    assert error["code"] == "PLATE_GEOMETRY_MISMATCH"
    assert error["call_made"] is True
    assert error["cost_counted_usd"] > 0.02  # two plates + two gate reads
    assert "already retried once" in error["message"]
    assert error["plate_metrics"]["gate"]["status"] == "refused"
    assert len(error["plate_metrics"]["gate"]["attempts"]) == 2
    assert len(client.images.calls) == 2


def test_a_conflicting_scene_is_not_a_refusal_the_measurement_wins_by_position(
    monkeypatch, scratch_env
):
    _install_fake(
        monkeypatch,
        _FakeClient(
            image_scripted=[_plate_response()],
            chat_responses=[_chat_response(MATCHING)],
        ),
    )
    store = AssetStore(config.get_data_dir())
    fg = _seed_foreground(store, _measured_chunk())
    drawn, _preview = server.draw(fg, "an eye-level shot of a calm room")
    assert drawn["status"] == "ok", drawn
    record = drawn["plate_metrics"]
    assert record["overridden_scene_phrases"] == ["eye-level"]
    assert record["gate"]["status"] == "pass"
