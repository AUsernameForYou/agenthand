"""Runtime configuration for agenthand.

Everything is read from the environment at call time (no caching), so tests
and wrappers can point the server at a scratch data dir or a different spend
cap simply by setting variables before a call.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

# The hackathon credit amount, and the absolute ceiling for the spend cap.
# This is the ONLY authorized Meta Model API spend unless Daniel raises it
# explicitly; the environment variable below may only ever LOWER the cap
# toward zero, never raise it past this ceiling.
HARD_CEILING_USD = 150.00

# The working cap the server actually runs with by default: below the
# credit amount, so estimate drift cannot spend the last of the credits
# and reach the card on file.
DEFAULT_TOTAL_SPEND_CAP_USD = 120.00


def get_data_dir() -> Path:
    """Local data dir holding the asset store and the cost ledger."""
    _load_dotenv()
    raw = os.environ.get("AGENTHAND_DATA_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[2] / "data"


def get_ledger_path() -> Path:
    return get_data_dir() / "ledger.jsonl"


def get_total_spend_cap_usd() -> float:
    """The working spend cap in USD.

    AGENTHAND_TOTAL_SPEND_CAP_USD may lower the cap (down to 0). A value
    above the hard ceiling is clamped to the ceiling. A value that is
    unparseable, non-finite (NaN/inf), or negative raises ValueError:
    a cap we cannot interpret must stop spend, not wave it through.
    """
    _load_dotenv()
    raw = os.environ.get("AGENTHAND_TOTAL_SPEND_CAP_USD", "").strip()
    if not raw:
        return DEFAULT_TOTAL_SPEND_CAP_USD
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"AGENTHAND_TOTAL_SPEND_CAP_USD={raw!r} is not a number; "
            "refusing to run with an uninterpretable spend cap."
        ) from exc
    if not math.isfinite(value):
        raise ValueError(
            f"AGENTHAND_TOTAL_SPEND_CAP_USD={raw!r} is not finite; "
            "refusing to run with an unbounded spend cap."
        )
    if value < 0:
        raise ValueError(
            f"AGENTHAND_TOTAL_SPEND_CAP_USD={raw!r} is negative; "
            "a spend cap cannot be negative."
        )
    return min(value, HARD_CEILING_USD)


_DOTENV_LOADED = False


def _load_dotenv() -> None:
    """Load KEY=VALUE pairs from a local .env file into os.environ.

    The current directory and the project root are checked. A variable
    already present in the environment always wins: the file never
    overwrites it. Loaded at most once per process. The .env file is
    operator-local state (it is gitignored) and is where the Meta API key
    lives on a dev machine; committed code never carries key material.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name:
                os.environ.setdefault(name, value)


def get_meta_api_key() -> str | None:
    """The Meta Model API key, from META_API_KEY (environment or .env).

    Returns None when no key is configured. Callers that need to make a
    real API call must treat None as "not configured" and refuse before
    any money moves; the key value itself is never logged or stored.
    """
    _load_dotenv()
    raw = os.environ.get("META_API_KEY", "").strip()
    return raw or None
