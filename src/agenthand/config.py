"""Runtime configuration for agenthand.

Everything is read from the environment at call time (no caching), so tests
and wrappers can point the server at a scratch data dir or a different spend
cap simply by setting variables before a call.
"""

from __future__ import annotations

import os
from pathlib import Path

# The hackathon credit amount. This is the ONLY authorized Meta Model API
# spend unless Daniel raises it explicitly; the ledger gate in ledger.py
# refuses any billable call that would push total spend past this value.
DEFAULT_TOTAL_SPEND_CAP_USD = 150.00


def get_data_dir() -> Path:
    """Local data dir holding the asset store and the cost ledger."""
    raw = os.environ.get("AGENTHAND_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "data"


def get_ledger_path() -> Path:
    return get_data_dir() / "ledger.jsonl"


def get_total_spend_cap_usd() -> float:
    raw = os.environ.get("AGENTHAND_TOTAL_SPEND_CAP_USD", "").strip()
    if raw:
        return float(raw)
    return DEFAULT_TOTAL_SPEND_CAP_USD
