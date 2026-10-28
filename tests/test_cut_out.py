"""cut_out wired to SAM 3.1: offline tests, mocked transport.

A fake client stands in for the OpenAI-compatible transport (chat
completions for the internal identify plan, responses for SAM); no
network is touched. The fixture pins the dotenv loader off and sets a
placeholder key, so the suite is hermetic in both directions: it never
reads the repo .env and never sees the real key. Canned SAM wire text
is shared with test_segmentation (real payloads, round-tripped through
the official parser during development).
"""

import base64
import json
import os
from io import BytesIO
from types import SimpleNamespace

os.environ.setdefault("AGENTHAND_DATA_DIR", "/tmp/agenthand-test-placeholder")

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from agenthand import config, ledger, meta_client, server  # noqa: E402
from agenthand.assets import AssetStore  # noqa: E402
from test_segmentation import FRAG_TEXT, FULL_TEXT, GOOD_TEXT, TWO_RECORDS_TEXT  # noqa: E402

FAKE_KEY = "placeholder-test-key-not-a-real-key"

IDENTIFY_PAYLOAD = {
    "description": "A brown leather bifold wallet on a wooden table.",
    "concept": "brown leather wallet",
    "alternates": ["leather bifold wallet", "brown wallet"],
    "suggested_title": "Brown Leather Bifold Wallet - Genuine Leather",
    "listing_bullets": ["Genuine leather", "Six card slots", "Slim profile"],
    "condition_notes": "Light corner wear visible; no tears or stains.",
}


@pytest.fixture(autouse=True)
def scratch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    # Pin the dotenv loader off: the repo .env (real key, for manual
    # smokes) must never leak into the suite, in either direction.
    monkeypatch.setattr(config, "_DOTENV_LOADED", True)
    return tmp_path


def _photo_uri(size=(8, 6)) -> str:
    """An 8x6 photo: red product on the left half, gray background right."""
    img = Image.new("RGBA", size, (120, 120, 120, 255))
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0] // 2):
            px[x, y] = (200, 30, 30, 255)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _records(tool=None):
    path = config.get_ledger_path()
    if not path.exists():
        return []
    records = [
        json.loads(line) for line in path.read_text().splitlines() if line.strip()
    ]
    return [r for r in records if tool is None or r["tool"] == tool]


def _chat_response(payload: dict):
    usage = SimpleNamespace(
        prompt_tokens=1500,
        completion_tokens=220,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
    )
    message = SimpleNamespace(content=json.dumps(payload))
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _sam_response(output_text: str):
    return SimpleNamespace(
        output_text=output_text,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class _FakeCalls:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake transport ran out of scripted responses")
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, chat_responses=(), sam_responses=()):
        self.completions = _FakeCalls(chat_responses)
        self.chat = SimpleNamespace(completions=self.completions)
        self.responses = _FakeCalls(sam_responses)


def _install_fake(monkeypatch, client: _FakeClient) -> _FakeClient:
    monkeypatch.setattr(meta_client, "build_client", lambda: client)
    return client


# -- success paths --------------------------------------------------------------


def test_cut_out_success_with_provided_concept(monkeypatch):
    client = _install_fake(monkeypatch, _FakeClient(sam_responses=[_sam_response(GOOD_TEXT)]))
    result = server.cut_out(_photo_uri(), "brown leather wallet")

    assert result["status"] == "ok"
    assert result["concept"] == "brown leather wallet"
    assert result["requested_concept"] == "brown leather wallet"
    assert result["concept_source"] == "provided"
    assert result["model"] == "sam-3.1"
    assert result["usage"]["images"] == 1
    assert result["cost_usd"] == 0.0025

    # The request that went out: Responses transport, the phrase, the image.
    (request,) = client.responses.calls
    assert request["model"] == "sam-3.1"
    content = request["input"][0]["content"]
    assert {"type": "input_text", "text": "brown leather wallet"} in content
    assert any(part.get("type") == "input_image" for part in content)

    # Attempt trace: one accepted attempt at 50% coverage.
    (attempt,) = result["attempts"]
    assert attempt["phrase"] == "brown leather wallet"
    assert attempt["outcome"] == "accepted"
    assert attempt["coverage"] == 0.5
    assert attempt["records"] == 1

    # Stored assets: kinds, lineage to the source photo, hashes reported.
    store = AssetStore(config.get_data_dir())
    source = store.get_record(result["image"])
    foreground = store.get_record(result["output_asset"])
    mask = store.get_record(result["mask_asset"])
    assert foreground.kind == "foreground_png"
    assert mask.kind == "mask_png"
    # The lineage is a chain, not siblings: the mask hangs off the
    # source photo and the foreground hangs off the mask, so draw's
    # receipt can pin the exact mask asset this cut used.
    assert mask.source_asset_id == source.asset_id
    assert foreground.source_asset_id == mask.asset_id
    assert result["hashes"]["source"]["sha256"] == source.sha256
    assert result["hashes"]["foreground"]["sha256"] == foreground.sha256
    assert result["hashes"]["foreground"]["pixel_sha256"] == foreground.pixel_sha256
    assert result["hashes"]["mask"]["sha256"] == mask.sha256
    store.verify_integrity(result["output_asset"])
    store.verify_integrity(result["mask_asset"])

    # The foreground IS the source pixels through the mask: red left
    # half byte-for-byte, transparent right half.
    fg = store.read_image(result["output_asset"]).load()
    for y in range(6):
        for x in range(4):
            assert fg[x, y] == (200, 30, 30, 255)
        for x in range(4, 8):
            assert fg[x, y] == (0, 0, 0, 0)

    # Ledger: one reservation, settled at one image.
    records = _records("cut_out")
    assert [r["phase"] for r in records] == ["pending", "settled"]
    assert records[0]["usd_estimate"] == 0.0025
    assert records[1]["usd_estimate"] == 0.0025
    assert records[1]["images"] == 1
    assert records[1]["call_id"] == result["call_id"]


def test_cut_out_ships_the_mask_overlay_and_review(monkeypatch):
    # J2 item 6: the selection itself gets a review surface — an
    # overlay asset a human/agent can look at, plus the mask's sanity
    # signals — because the receipt can only vouch for selected pixels.
    _install_fake(
        monkeypatch, _FakeClient(sam_responses=[_sam_response(GOOD_TEXT)])
    )
    result = server.cut_out(_photo_uri(), "brown leather wallet")
    assert result["status"] == "ok"

    store = AssetStore(config.get_data_dir())
    overlay_record = store.get_record(result["mask_overlay_asset"])
    assert overlay_record.kind == "mask_overlay"
    assert overlay_record.source_asset_id == store.get_record(result["image"]).asset_id
    store.verify_integrity(result["mask_overlay_asset"])

    from agenthand import segmentation

    overlay = store.read_image(result["mask_overlay_asset"]).load()
    outline = segmentation.OVERLAY_OUTLINE_RGBA
    assert overlay[3, 3] == outline  # boundary of the left-half mask
    assert overlay[1, 1] == (200, 30, 30, 255)  # interior source pixel intact
    assert overlay[6, 3] == (120, 120, 120, 255)  # outside: photo untouched

    review = result["mask_review"]
    assert review["bbox"] == [0, 0, 4, 6]
    assert review["bbox_fill"] == 1.0
    assert review["coverage"] == 0.5
    assert review["touches_frame_edges"] == ["left", "top", "bottom"]
    assert review["warnings"] == []


def test_cut_out_picks_the_largest_record(monkeypatch):
    _install_fake(
        monkeypatch, _FakeClient(sam_responses=[_sam_response(TWO_RECORDS_TEXT)])
    )
    result = server.cut_out(_photo_uri(), "brown leather wallet")
    assert result["status"] == "ok"
    (attempt,) = result["attempts"]
    assert attempt["records"] == 2
    assert attempt["object_id"] == "1"  # the 24-pixel block, not the 4-pixel blob
    assert attempt["coverage"] == 0.5


def test_cut_out_recovers_with_first_alternate_after_zero_match(monkeypatch):
    client = _install_fake(
        monkeypatch,
        _FakeClient(
            chat_responses=[_chat_response(IDENTIFY_PAYLOAD)],
            sam_responses=[_sam_response(""), _sam_response(GOOD_TEXT)],
        ),
    )
    result = server.cut_out(_photo_uri())

    assert result["status"] == "ok"
    assert result["concept_source"] == "identify_internal"
    # The winning phrase is the first alternate; the trace shows both tries.
    assert result["concept"] == "leather bifold wallet"
    assert [a["outcome"] for a in result["attempts"]] == ["no_match", "accepted"]
    assert [a["phrase"] for a in result["attempts"]] == [
        "brown leather wallet",
        "leather bifold wallet",
    ]
    phrases_sent = [
        call["input"][0]["content"][0]["text"] for call in client.responses.calls
    ]
    assert phrases_sent == ["brown leather wallet", "leather bifold wallet"]

    # Both attempts billed: settled at two images; identify billed on its own.
    assert result["usage"]["images"] == 2
    assert result["cost_usd"] == 0.005
    cut_records = _records("cut_out")
    assert [r["phase"] for r in cut_records] == ["pending", "settled"]
    assert cut_records[0]["usd_estimate"] == 0.005  # reservation priced both attempts
    assert cut_records[1]["images"] == 2
    tools = [r["tool"] for r in _records() if r["phase"] == "pending"]
    assert tools == ["identify", "cut_out"]


# -- failure paths: spend counted, structured errors -----------------------------


def test_cut_out_no_match_with_provided_concept(monkeypatch):
    _install_fake(monkeypatch, _FakeClient(sam_responses=[_sam_response("")]))
    result = server.cut_out(_photo_uri(), "brown leather wallet")

    assert result["status"] == "error"
    assert result["error"]["code"] == "NO_MATCH"
    assert result["output_asset"] is None
    (attempt,) = result["error"]["attempts"]
    assert attempt["outcome"] == "no_match"
    # The single attempt's reservation stays counted; nothing settled.
    records = _records("cut_out")
    assert [r["phase"] for r in records] == ["pending"]
    assert records[0]["usd_estimate"] == 0.0025
    assert result["error"]["cost_counted_usd"] == 0.0025
    assert ledger.ledger_total_usd(config.get_ledger_path()) == 0.0025


def test_cut_out_poor_mask_full_frame(monkeypatch):
    _install_fake(monkeypatch, _FakeClient(sam_responses=[_sam_response(FULL_TEXT)]))
    result = server.cut_out(_photo_uri(), "brown leather wallet")

    assert result["status"] == "error"
    assert result["error"]["code"] == "POOR_MASK"
    (attempt,) = result["error"]["attempts"]
    assert attempt["outcome"] == "poor_mask"
    assert attempt["rejection"] == "full_frame"
    assert attempt["coverage"] == 1.0
    assert [r["phase"] for r in _records("cut_out")] == ["pending"]


def test_cut_out_recovery_exhausted_counts_spend_once_per_attempt(monkeypatch):
    _install_fake(
        monkeypatch,
        _FakeClient(
            chat_responses=[_chat_response(IDENTIFY_PAYLOAD)],
            sam_responses=[_sam_response(""), _sam_response(FRAG_TEXT)],
        ),
    )
    result = server.cut_out(_photo_uri())

    assert result["status"] == "error"
    # Final attempt produced a mask that failed QA -> POOR_MASK.
    assert result["error"]["code"] == "POOR_MASK"
    assert [a["outcome"] for a in result["error"]["attempts"]] == [
        "no_match",
        "poor_mask",
    ]
    assert result["error"]["attempts"][1]["rejection"] == "fragmented"
    # One reservation priced at both attempts, counted exactly once.
    records = _records("cut_out")
    assert [r["phase"] for r in records] == ["pending"]
    assert records[0]["usd_estimate"] == 0.005
    assert result["error"]["cost_counted_usd"] == 0.005


def test_cut_out_missing_key_is_not_configured_with_no_ledger_write(monkeypatch):
    monkeypatch.delenv("META_API_KEY")
    result = server.cut_out(_photo_uri(), "brown leather wallet")
    assert result["status"] == "error"
    assert result["error"]["code"] == "NOT_CONFIGURED"
    assert _records() == []
    assert not config.get_ledger_path().exists()


def test_cut_out_without_concept_or_key_reports_the_plan_failure(monkeypatch):
    monkeypatch.delenv("META_API_KEY")
    result = server.cut_out(_photo_uri())
    assert result["status"] == "error"
    assert result["error"]["code"] == "NOT_CONFIGURED"
    assert result["plan_error"]["code"] == "NOT_CONFIGURED"
    assert _records() == []


# -- the production chain, end to end (architecture Gauntlet fix 1) -------


def test_production_chain_draw_receipt_pins_the_stored_mask(monkeypatch):
    # The real server tools, in the order a listing is produced:
    # cut_out (fake SAM) then draw (placeholder plate, the explicit
    # offline flag). The draw receipt must pin the SAME mask asset
    # cut_out stored — production receipts said mask_asset: null
    # before this fix because mask and foreground were siblings.
    from agenthand import composite

    monkeypatch.setenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", "1")
    _install_fake(
        monkeypatch, _FakeClient(sam_responses=[_sam_response(GOOD_TEXT)])
    )
    cut = server.cut_out(_photo_uri(), "brown leather wallet")
    assert cut["status"] == "ok"

    out = server.draw(cut["output_asset"], "a regal, warmly lit reading room")
    result, _preview = out
    assert result["status"] == "ok"
    receipt = result["fidelity_receipt"]

    store = AssetStore(config.get_data_dir())
    mask_record = store.get_record(cut["mask_asset"])
    assert receipt["mask_source"] == "asset"
    assert receipt["mask_asset"] == cut["mask_asset"]
    assert receipt["mask_sha256"] == mask_record.sha256
    assert receipt["mask_pixel_sha256"] == mask_record.pixel_sha256
    assert receipt["source_asset"] == cut["image"]
    assert receipt["changed_opaque_pixels"] == 0

    report = composite.verify_receipt_from_disk(store, result["output_asset"])
    assert report["ok"] is True, report["problems"]


def test_cut_out_refuses_a_composite_handle(monkeypatch):
    # draw -> cut_out(composite) must refuse before any billing: the
    # composite's pixels are partly generated, and cutting them out
    # would launder them into a "foreground" chained to the photo.
    monkeypatch.setenv("AGENTHAND_DRAW_PLACEHOLDER_PLATE", "1")
    client = _install_fake(
        monkeypatch, _FakeClient(sam_responses=[_sam_response(GOOD_TEXT)])
    )
    cut = server.cut_out(_photo_uri(), "brown leather wallet")
    assert cut["status"] == "ok"
    drawn, _preview = server.draw(cut["output_asset"], "a regal room")
    composite_handle = drawn["output_asset"]

    result = server.cut_out(composite_handle, "brown leather wallet")
    assert result["status"] == "error"
    assert result["error"]["code"] == "NOT_A_SOURCE_PHOTO"
    assert result["output_asset"] is None
    # No second SAM call, no second cut_out reservation.
    assert len(client.responses.calls) == 1
    assert [r["phase"] for r in _records("cut_out")] == ["pending", "settled"]


def test_cut_out_refuses_over_cap_before_any_api_call(monkeypatch):
    client = _install_fake(
        monkeypatch, _FakeClient(sam_responses=[_sam_response(GOOD_TEXT)])
    )
    ledger.append_receipt(
        config.get_ledger_path(),
        ledger.Receipt(tool="identify", model="muse-wonder-1.0", usd_estimate=200.00),
    )
    with pytest.raises(ledger.SpendCapExceeded):
        server.cut_out(_photo_uri(), "brown leather wallet")
    assert client.responses.calls == []
