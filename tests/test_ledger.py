"""Ledger receipts and the fail-closed spend cap."""

import json

import pytest

from agenthand import ledger


def _seed_receipt(path, usd, tool="identify"):
    ledger.append_receipt(
        path, ledger.Receipt(tool=tool, model="muse-spark-1.3", usd_estimate=usd)
    )


def test_append_and_total(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seed_receipt(path, 1.25)
    _seed_receipt(path, 2.50, tool="cut_out")
    assert ledger.ledger_total_usd(path) == 3.75

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    record = json.loads(lines[0])
    assert record["tool"] == "identify"
    assert record["pricing_table_version"]  # version is always recorded


def test_missing_ledger_is_zero(tmp_path):
    assert ledger.ledger_total_usd(tmp_path / "nope.jsonl") == 0.0


def test_cap_allows_call_under_cap(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seed_receipt(path, 100.00)
    total = ledger.check_spend_cap(path, projected_usd=49.99, cap_usd=150.00)
    assert total == 100.00


def test_cap_allows_landing_exactly_on_cap(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seed_receipt(path, 100.00)
    ledger.check_spend_cap(path, projected_usd=50.00, cap_usd=150.00)


def test_cap_refuses_when_seeded_ledger_exceeds_cap(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seed_receipt(path, 149.00)
    _seed_receipt(path, 5.00)  # ledger now at $154, over the $150 cap
    with pytest.raises(ledger.SpendCapExceeded):
        ledger.check_spend_cap(path, projected_usd=0.01, cap_usd=150.00)


def test_cap_refuses_projected_overshoot(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _seed_receipt(path, 149.50)
    with pytest.raises(ledger.SpendCapExceeded):
        ledger.check_spend_cap(path, projected_usd=1.00, cap_usd=150.00)


def test_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ledger.SpendCapExceeded):
        ledger.check_spend_cap(path, projected_usd=0.01, cap_usd=150.00)
