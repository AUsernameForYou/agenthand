"""identify wired to the Meta Model API: offline tests, mocked transport.

A fake client stands in for the OpenAI-compatible transport; no network
is touched and no real key exists anywhere in this file. Each test
points the data dir at a tmp dir and sets a placeholder META_API_KEY so
the configured path runs end to end through the billing choke point.
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

from agenthand import config, ledger, meta_client, pricing, server  # noqa: E402

FAKE_KEY = "placeholder-test-key-not-a-real-key"

GOOD_PAYLOAD = {
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
    return tmp_path


def _data_uri(size=(6, 4), color=(120, 80, 40, 255)) -> str:
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def _records():
    path = config.get_ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _response(content, *, prompt_tokens=1500, completion_tokens=220, cached_tokens=0):
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
    )
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake transport ran out of scripted responses")
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.completions = _FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def _install_fake(monkeypatch, responses) -> _FakeClient:
    client = _FakeClient(responses)
    monkeypatch.setattr(meta_client, "build_client", lambda: client)
    return client


def _good_content(**overrides) -> str:
    payload = dict(GOOD_PAYLOAD)
    payload.update(overrides)
    return json.dumps(payload)


# -- the configured happy path ----------------------------------------------


def test_identify_success_parses_plan_and_settles_with_real_usage(monkeypatch):
    client = _install_fake(
        monkeypatch,
        [_response(_good_content(), prompt_tokens=1500, completion_tokens=220, cached_tokens=300)],
    )
    result = server.identify(_data_uri())

    assert result["status"] == "ok"
    assert result["description"] == GOOD_PAYLOAD["description"]
    assert result["concept"] == "brown leather wallet"
    assert result["alternates"] == GOOD_PAYLOAD["alternates"]
    assert result["suggested_title"] == GOOD_PAYLOAD["suggested_title"]
    assert result["listing_bullets"] == GOOD_PAYLOAD["listing_bullets"]
    assert result["condition_notes"] == GOOD_PAYLOAD["condition_notes"]

    # The request that went out: bounded output, strict schema, vision model.
    (request,) = client.completions.calls
    assert request["model"] == "muse-spark-1.3"
    assert request["max_tokens"] == meta_client.IDENTIFY_MAX_TOKENS
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True

    # Settled cost is priced from the response usage, counts included.
    expected_cost = pricing.estimate_cost_usd(
        "muse-spark-1.3",
        input_tokens=1500,
        output_tokens=220,
        cached_input_tokens=300,
    )
    assert expected_cost > 0
    assert result["cost_usd"] == expected_cost
    assert result["usage"]["input_tokens"] == 1500
    assert result["usage"]["output_tokens"] == 220
    assert result["usage"]["cached_input_tokens"] == 300

    records = _records()
    assert [r["phase"] for r in records] == ["pending", "settled"]
    pending, settled = records
    # The reservation is the projected worst case for a 6x4 image.
    assert pending["usd_estimate"] == meta_client.project_identify_cost_usd(
        "muse-spark-1.3", 6, 4
    )
    assert pending["tool"] == "identify"
    assert settled["call_id"] == pending["call_id"] == result["call_id"]
    assert settled["usd_estimate"] == expected_cost
    assert settled["input_tokens"] == 1500
    assert settled["output_tokens"] == 220
    assert settled["cached_input_tokens"] == 300


def test_cut_out_plans_internally_with_the_real_identify(monkeypatch):
    _install_fake(monkeypatch, [_response(_good_content())])
    result = server.cut_out(_data_uri())
    assert result["concept_source"] == "identify_internal"
    assert result["concept"] == "brown leather wallet"
    assert result["alternates"] == GOOD_PAYLOAD["alternates"]
    assert result["plan_error"] is None
    # Both the internal identify call and cut_out's own stub call billed.
    tools = [r["tool"] for r in _records() if r["phase"] == "pending"]
    assert tools == ["identify", "cut_out"]


# -- fail-closed paths --------------------------------------------------------


def test_identify_malformed_response_fails_closed_and_reservation_counts(monkeypatch):
    _install_fake(monkeypatch, [_response("this is not json at all")])
    result = server.identify(_data_uri())

    assert result["status"] == "error"
    assert result["error"]["code"] == "BAD_MODEL_RESPONSE"
    # The billed reservation stays counted as spend; nothing settled.
    records = _records()
    assert [r["phase"] for r in records] == ["pending"]
    projected = meta_client.project_identify_cost_usd("muse-spark-1.3", 6, 4)
    assert records[0]["usd_estimate"] == projected
    assert ledger.ledger_total_usd(config.get_ledger_path()) == projected
    assert result["error"]["cost_counted_usd"] == projected


def test_identify_truncated_response_saves_raw_to_debug_file(monkeypatch):
    # The first live failure mode: output cut off mid-string at the
    # token bound, so the body is a JSON prefix that never closes.
    full = _good_content()
    truncated = full[: len(full) // 2]
    assert truncated != full
    _install_fake(monkeypatch, [_response(truncated)])
    result = server.identify(_data_uri())

    assert result["status"] == "error"
    assert result["error"]["code"] == "BAD_MODEL_RESPONSE"

    # The full raw body was saved under data/debug/, named for the
    # ledger call, and the error result points at it.
    records = _records()
    assert [r["phase"] for r in records] == ["pending"]
    debug_path = result["error"].get("debug_path")
    assert debug_path
    path = Path(debug_path)
    assert path.parent == config.get_data_dir() / "debug"
    assert path.name == f"identify-{records[0]['call_id']}.txt"
    assert path.read_text(encoding="utf-8") == truncated

    # The raw model text never lands in the ledger...
    assert truncated not in config.get_ledger_path().read_text(encoding="utf-8")
    # ...and the reservation is counted exactly once: not dropped,
    # not double-counted.
    projected = meta_client.project_identify_cost_usd("muse-spark-1.3", 6, 4)
    assert records[0]["usd_estimate"] == projected
    assert ledger.ledger_total_usd(config.get_ledger_path()) == projected
    assert result["error"]["cost_counted_usd"] == projected


def test_identify_missing_field_fails_closed_without_repair(monkeypatch):
    payload = dict(GOOD_PAYLOAD)
    del payload["concept"]
    client = _install_fake(monkeypatch, [_response(json.dumps(payload))])
    result = server.identify(_data_uri())
    assert result["status"] == "error"
    assert result["error"]["code"] == "BAD_MODEL_RESPONSE"
    # A missing field is not the alternates defect: no repair call is made.
    assert len(client.completions.calls) == 1


def test_identify_missing_key_is_not_configured_with_no_ledger_write(monkeypatch):
    monkeypatch.delenv("META_API_KEY")
    # No fake installed: if the key check regressed and the call path ran,
    # build_client would raise NotConfiguredError AFTER a reservation was
    # written — the empty-ledger assertion below catches that ordering.
    result = server.identify(_data_uri())
    assert result["status"] == "error"
    assert result["error"]["code"] == "NOT_CONFIGURED"
    assert result["concept"] is None
    assert _records() == []
    assert not config.get_ledger_path().exists()


def test_identify_refuses_over_cap_before_any_api_call(monkeypatch):
    client = _install_fake(monkeypatch, [_response(_good_content())])
    ledger.append_receipt(
        config.get_ledger_path(),
        ledger.Receipt(tool="identify", model="muse-wonder-1.0", usd_estimate=200.00),
    )
    with pytest.raises(ledger.SpendCapExceeded):
        server.identify(_data_uri())
    assert client.completions.calls == []  # the gate refused before the call


# -- the alternates contract: repair, then error ------------------------------


def test_identify_alternates_shortfall_is_repaired_once(monkeypatch):
    first = _response(
        _good_content(alternates=["leather wallet"]),
        prompt_tokens=1000,
        completion_tokens=200,
    )
    repair = _response(
        json.dumps({"alternates": ["bifold wallet", "men's leather wallet"]}),
        prompt_tokens=100,
        completion_tokens=30,
    )
    client = _install_fake(monkeypatch, [first, repair])
    result = server.identify(_data_uri())

    assert result["status"] == "ok"
    assert len(client.completions.calls) == 2
    assert result["alternates"] == [
        "leather wallet",
        "bifold wallet",
        "men's leather wallet",
    ]
    # Usage from BOTH calls is summed onto the settled receipt.
    assert result["usage"]["input_tokens"] == 1100
    assert result["usage"]["output_tokens"] == 230


def test_identify_alternates_repair_failure_fails_closed(monkeypatch):
    first = _response(_good_content(alternates=[]))
    repair = _response(json.dumps({"alternates": []}))
    _install_fake(monkeypatch, [first, repair])
    result = server.identify(_data_uri())
    assert result["status"] == "error"
    assert result["error"]["code"] == "BAD_MODEL_RESPONSE"
    assert [r["phase"] for r in _records()] == ["pending"]


# -- key configuration ---------------------------------------------------------


def test_get_meta_api_key_reads_env_and_blank_is_none(monkeypatch):
    assert config.get_meta_api_key() == FAKE_KEY
    monkeypatch.setenv("META_API_KEY", "   ")
    assert config.get_meta_api_key() is None
    monkeypatch.delenv("META_API_KEY")
    assert config.get_meta_api_key() is None


def test_get_meta_api_key_loads_dotenv_without_overriding_env(tmp_path, monkeypatch):
    monkeypatch.delenv("META_API_KEY")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("META_API_KEY=from-dotenv-file\n", encoding="utf-8")
    monkeypatch.setattr(config, "_DOTENV_LOADED", False)
    try:
        assert config.get_meta_api_key() == "from-dotenv-file"
        # A real environment variable beats the file.
        monkeypatch.setenv("META_API_KEY", "from-env")
        assert config.get_meta_api_key() == "from-env"
    finally:
        os.environ.pop("META_API_KEY", None)
