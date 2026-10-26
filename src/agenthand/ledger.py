"""Cost receipts and the fail-closed spend cap.

Every tool call appends one JSON receipt to a local JSONL ledger. Before any
billable model call, check_spend_cap() compares the ledger total plus the
projected cost of the next call against the cap and refuses if the call
would exceed it.

Fail-closed means exactly that: a missing ledger reads as $0.00 (nothing has
been spent yet), but a ledger that exists and cannot be parsed raises. When
spend state is unknown, the server does not spend.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import pricing


class SpendCapExceeded(RuntimeError):
    """Raised when a billable call would push total spend past the cap."""


@dataclass
class Receipt:
    tool: str
    model: str | None
    usd_estimate: float
    pricing_table_version: str = pricing.PRICING_TABLE_VERSION
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    images: int = 0
    status: str = "ok"  # "ok" for real calls, "stub" while tools are unwired
    note: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def append_receipt(ledger_path: Path, receipt: Receipt) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(receipt.to_json_line() + "\n")


def ledger_total_usd(ledger_path: Path) -> float:
    """Total recorded spend. Missing ledger = 0.0; corrupt ledger = raise."""
    if not ledger_path.exists():
        return 0.0
    total = 0.0
    with ledger_path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                total += float(record["usd_estimate"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise SpendCapExceeded(
                    f"Ledger {ledger_path} line {lineno} is unreadable ({exc}); "
                    "refusing to spend with unknown spend state."
                ) from exc
    return round(total, 6)


def check_spend_cap(ledger_path: Path, projected_usd: float, cap_usd: float) -> float:
    """Refuse a billable call that would exceed the cap.

    Returns the current ledger total when the call is allowed. Raises
    SpendCapExceeded when ledger total + projected cost would exceed the cap
    (landing exactly on the cap is allowed).
    """
    total = ledger_total_usd(ledger_path)
    if total + projected_usd > cap_usd:
        raise SpendCapExceeded(
            f"Spend cap: ledger total ${total:.2f} + projected ${projected_usd:.4f} "
            f"would exceed the ${cap_usd:.2f} cap. Call refused before it ran."
        )
    return total
