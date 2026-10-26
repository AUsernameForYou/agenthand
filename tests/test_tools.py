"""Stub tools: structured not-yet-wired results, receipts, cap wiring.

AGENTHAND_DATA_DIR is pointed at a tmp dir before the server module is
imported; config reads the environment at call time, so every receipt these
tests trigger lands in the tmp ledger, never the real one.
"""

import json
import os

os.environ.setdefault("AGENTHAND_DATA_DIR", "/tmp/agenthand-test-placeholder")

import pytest  # noqa: E402

from agenthand import config, ledger, server  # noqa: E402


@pytest.fixture(autouse=True)
def scratch_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    yield tmp_path


def _ledger_records():
    path = config.get_ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_identify_stub_shape_and_receipt():
    result = server.identify("asset://abc123")
    assert result["status"] == "not_yet_wired"
    assert result["concept"] is None
    assert result["alternates"] == []

    records = _ledger_records()
    assert len(records) == 1
    assert records[0]["tool"] == "identify"
    assert records[0]["model"] == "muse-spark-1.3"
    assert records[0]["status"] == "stub"
    assert records[0]["usd_estimate"] == 0.0


def test_cut_out_stub_shape_and_receipt():
    result = server.cut_out("asset://abc123", "brown leather wallet")
    assert result["status"] == "not_yet_wired"
    assert result["output_asset"] is None

    records = _ledger_records()
    assert len(records) == 1
    assert records[0]["tool"] == "cut_out"
    assert records[0]["model"] == "sam-3.1"


def test_draw_stub_shape_and_receipt():
    result = server.draw("asset://fg1", "a regal, warmly lit reading room")
    assert result["status"] == "not_yet_wired"
    assert result["output_asset"] is None
    assert result["fidelity_receipt"] is None

    records = _ledger_records()
    assert len(records) == 1
    assert records[0]["tool"] == "draw"
    assert records[0]["model"] == "muse-image-1.0"


def test_stub_calls_refuse_when_ledger_already_over_cap():
    # Seed the ledger past the $150 cap; even $0 stub calls must refuse,
    # because the gate runs before the (future) billable call.
    path = config.get_ledger_path()
    ledger.append_receipt(
        path, ledger.Receipt(tool="identify", model="muse-wonder-1.0", usd_estimate=200.00)
    )
    with pytest.raises(ledger.SpendCapExceeded):
        server.identify("asset://abc123")
