"""The billing choke point: the ONLY place a billable call may originate.

Every external model call agenthand ever makes goes through billed_call().
Tools never touch the ledger or the gate directly. billed_call enforces
reserve-then-settle accounting against the spend cap:

1. Under a file lock (so two processes or threads cannot both pass the
   gate on the same stale total), the ledger total is checked against the
   cap and a "pending" reservation receipt is appended at the projected
   worst-case cost.
2. The call runs (the lock is NOT held across the network call; the
   reservation on disk is what makes the spend visible to other callers).
3. A "settled" receipt is appended with the actual cost, priced from the
   usage the provider returned, usage counts included.
4. If the call raises, no settlement is written: the reservation stays
   counted at its projected cost. A call that may have billed us is
   treated as having billed us.

When the live Meta client lands here, it MUST be constructed with SDK
retries disabled (max_retries=0). An SDK-level retry would re-run a
billable call behind the gate's back; every attempt, including retries,
has to pass through billed_call so it is reserved and counted.

Nothing in this module performs network I/O yet: fn is supplied by the
caller, and until API wiring is unblocked (hackathon credits landed) the
tools pass local stand-ins that make no external call at all.
"""

from __future__ import annotations

import fcntl
import uuid
from collections.abc import Callable
from pathlib import Path

from . import config, ledger, pricing

# A usage dict as returned by a billable call: any of these keys.
UsageDict = dict[str, int]

_USAGE_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens", "images")


def _lock_file(data_dir: Path):
    """Open (creating if needed) the ledger lock file. Caller closes it."""
    data_dir.mkdir(parents=True, exist_ok=True)
    return (data_dir / "ledger.lock").open("a+")


def billed_call(
    tool: str,
    model: str | None,
    projected_usd: float,
    fn: Callable[[], UsageDict | None],
    *,
    status: str = "ok",
    note: str = "",
) -> dict:
    """Run one billable call under the spend cap, reserve-then-settle.

    Args:
        tool: Tool name recorded on the receipts ("identify", ...).
        model: Priced model name, or None for a call with no model cost.
            An unknown (unpriced) model raises before anything is written:
            unpriced spend must never flow.
        projected_usd: Worst-case cost reserved before the call runs.
        fn: Zero-argument callable performing the call. Returns a usage
            dict (any of input_tokens, output_tokens, cached_input_tokens,
            images); the settled cost is priced from it. May return None
            for zero usage.
        status: Receipt status ("ok", or "stub" while a tool is unwired).
        note: Receipt note (stub explanations etc.).

    Returns a dict with call_id, the projected and actual USD amounts, and
    the usage fn reported.

    Raises ledger.SpendCapExceeded if the reservation would exceed the
    cap, pricing.UnknownModelError for an unpriced model, and whatever fn
    raises (with the reservation left counted).
    """
    if model is not None and not pricing.is_known_model(model):
        raise pricing.UnknownModelError(
            f"Model {model!r} is not in the pricing table; refusing to run "
            "an unpriced billable call."
        )
    # Validate the projection before anything is written.
    ledger._validate_amount(projected_usd, "projected cost")

    data_dir = config.get_data_dir()
    ledger_path = config.get_ledger_path()
    cap_usd = config.get_total_spend_cap_usd()
    call_id = uuid.uuid4().hex

    # Reserve: check + append pending, atomically under the file lock.
    with _lock_file(data_dir) as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        ledger.check_spend_cap(ledger_path, projected_usd, cap_usd)
        ledger.append_receipt(
            ledger_path,
            ledger.Receipt(
                tool=tool,
                model=model,
                usd_estimate=float(projected_usd),
                status=status,
                phase=ledger.PHASE_PENDING,
                call_id=call_id,
                note=note,
            ),
        )

    # Call. If this raises, the reservation stays counted; do not settle.
    usage = fn() or {}
    usage = {key: int(usage.get(key, 0)) for key in _USAGE_KEYS}

    actual_usd = pricing.estimate_cost_usd(model, **usage) if model else 0.0

    # Settle: append the actual, with the usage counts stored on the receipt.
    with _lock_file(data_dir) as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        ledger.append_receipt(
            ledger_path,
            ledger.Receipt(
                tool=tool,
                model=model,
                usd_estimate=actual_usd,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cached_input_tokens=usage["cached_input_tokens"],
                images=usage["images"],
                status=status,
                phase=ledger.PHASE_SETTLED,
                call_id=call_id,
                note=note,
            ),
        )

    return {
        "call_id": call_id,
        "projected_usd": float(projected_usd),
        "actual_usd": actual_usd,
        "usage": usage,
    }
