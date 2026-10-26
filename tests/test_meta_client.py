"""The billing choke point: reserve-then-settle, races, failure modes."""

import json
import threading

import pytest

from agenthand import config, ledger, meta_client, pricing


@pytest.fixture(autouse=True)
def scratch_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTHAND_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTHAND_TOTAL_SPEND_CAP_USD", "120")
    return tmp_path


def _records():
    path = config.get_ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_settle_records_actual_cost_and_usage():
    result = meta_client.billed_call(
        "identify",
        "muse-spark-1.3",
        1.00,
        lambda: {"input_tokens": 1_000_000, "cached_input_tokens": 1_000_000},
    )
    # Actual: 1M input tokens, all cached, at $0.15/1M.
    assert result["actual_usd"] == 0.15
    assert result["projected_usd"] == 1.00
    assert ledger.ledger_total_usd(config.get_ledger_path()) == 0.15

    records = _records()
    assert [r["phase"] for r in records] == ["pending", "settled"]
    settled = records[1]
    assert settled["call_id"] == records[0]["call_id"] == result["call_id"]
    # The usage the provider reported is stored on the settled receipt.
    assert settled["input_tokens"] == 1_000_000
    assert settled["cached_input_tokens"] == 1_000_000
    assert settled["output_tokens"] == 0
    assert settled["usd_estimate"] == 0.15


def test_exception_after_send_leaves_reservation_counted():
    def boom():
        raise RuntimeError("provider timed out; the call may have billed")

    with pytest.raises(RuntimeError):
        meta_client.billed_call("draw", "muse-image-1.0", 0.50, boom)

    # No settlement was written, so the reservation still counts in full:
    # a call that may have billed is treated as having billed.
    assert ledger.ledger_total_usd(config.get_ledger_path()) == 0.50
    records = _records()
    assert len(records) == 1
    assert records[0]["phase"] == "pending"


def test_unknown_model_raises_and_writes_nothing():
    with pytest.raises(pricing.UnknownModelError):
        meta_client.billed_call("identify", "muse-spark-9.9", 1.00, lambda: {})
    assert _records() == []


def test_projection_over_cap_refused_before_call_runs():
    ran = []
    with pytest.raises(ledger.SpendCapExceeded):
        meta_client.billed_call(
            "identify", "muse-spark-1.3", 121.00, lambda: ran.append(True)
        )
    assert ran == []  # fn never ran
    assert _records() == []  # and no reservation was left behind


def test_two_threads_racing_the_gate_cannot_both_fit():
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def slow_call():
        entered.set()  # reservation is already on disk when fn runs
        release.wait(timeout=10)
        return {}

    def run_a():
        try:
            meta_client.billed_call("identify", "muse-spark-1.3", 100.00, slow_call)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert entered.wait(timeout=10)

    # A holds a $100 reservation against the $120 cap; B's $30 cannot fit.
    with pytest.raises(ledger.SpendCapExceeded):
        meta_client.billed_call("identify", "muse-spark-1.3", 30.00, lambda: {})

    release.set()
    thread_a.join(timeout=10)
    assert errors == []
