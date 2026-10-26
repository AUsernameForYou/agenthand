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

The live Meta client below IS constructed with SDK retries disabled
(max_retries=0): an SDK-level retry would re-run a billable call behind
the gate's back. Every attempt, including our own explicit repair
attempt, passes through billed_call so it is reserved and counted.

The key check happens BEFORE billed_call is invoked: a call attempted
with no META_API_KEY configured raises NotConfiguredError before any
reservation is written, so a missing key can never leave phantom spend
in the ledger.
"""

from __future__ import annotations

import base64
import fcntl
import json
import math
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


# ---------------------------------------------------------------------------
# The live Meta Model API client (OpenAI-compatible transport).
# ---------------------------------------------------------------------------

META_BASE_URL = "https://api.meta.ai/v1"

# Bounded output for the identify call: the structured result is a short
# description, a few phrases, and listing copy — well under this. The
# bound is set explicitly on every request because the reservation is
# priced from it.
IDENTIFY_MAX_TOKENS = 1024

# Allowance for the text side of the identify request (instructions plus
# schema overhead) when projecting the worst-case input size.
_IDENTIFY_PROMPT_TOKENS = 300


class NotConfiguredError(RuntimeError):
    """Raised when a real Meta API call is attempted with no key configured.

    Raised BEFORE any reservation is written: a missing key is an
    operator setup problem, not spend, and must never touch the ledger.
    """


class IdentifyResultError(RuntimeError):
    """Raised when the model's identify response cannot be used.

    Covers malformed JSON, missing or mistyped fields, and a concept /
    alternates contract the repair attempt could not fix. When this
    propagates out of a billed_call fn, the reservation stays counted:
    the call may have billed, so the spend is treated as spent.
    """


def require_meta_api_key() -> str:
    """Return the configured Meta API key or raise NotConfiguredError."""
    key = config.get_meta_api_key()
    if not key:
        raise NotConfiguredError(
            "META_API_KEY is not set (environment or .env); the Meta "
            "Model API is not configured, so no billable call was made."
        )
    return key


def build_client():
    """Construct the OpenAI-compatible client for the Meta Model API.

    max_retries=0 is load-bearing: SDK retries would re-run a billable
    call behind the spend gate's back. Raises NotConfiguredError when no
    key is configured.
    """
    from openai import OpenAI

    return OpenAI(
        base_url=META_BASE_URL,
        api_key=require_meta_api_key(),
        max_retries=0,
    )


def estimate_image_input_tokens(width: int | None, height: int | None) -> int:
    """Conservative upper bound on vision input tokens for one image.

    Vision encoders tile an image; one token per 32x32 patch (rounded
    up) plus slack is a safe over-estimate for reservation pricing.
    Unknown dimensions fall back to a mid-size bound.
    """
    if not width or not height:
        return 4096
    patches = math.ceil(width / 32) * math.ceil(height / 32)
    return patches + 256


def project_identify_cost_usd(
    model: str, width: int | None, height: int | None
) -> float:
    """Worst-case USD projection for one identify invocation.

    Covers the initial image call AND the one possible alternates-repair
    call at the same worst case (the repair call is text-only and
    cheaper in practice; projecting it at the full image-call price
    keeps the reservation a true upper bound).
    """
    input_tokens = estimate_image_input_tokens(width, height) + _IDENTIFY_PROMPT_TOKENS
    one_call = pricing.estimate_cost_usd(
        model, input_tokens=input_tokens, output_tokens=IDENTIFY_MAX_TOKENS
    )
    return round(2 * one_call, 6)


_IDENTIFY_SCHEMA: dict = {
    "name": "identify_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "description": {"type": "string"},
            "concept": {"type": "string"},
            "alternates": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
            },
            "suggested_title": {"type": "string"},
            "listing_bullets": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 5,
            },
            "condition_notes": {"type": "string"},
        },
        "required": [
            "description",
            "concept",
            "alternates",
            "suggested_title",
            "listing_bullets",
            "condition_notes",
        ],
    },
}

_IDENTIFY_INSTRUCTIONS = (
    "You are the planning step of a listing-production pipeline. Look at "
    "the product photo and return the structured result. description: a "
    "short factual description of the product. concept: ONE short noun "
    "phrase naming the product, suitable for text-prompted image "
    "segmentation (e.g. 'brown leather wallet') — the product only, no "
    "background, no adjectives about the scene. alternates: at least 2 "
    "different fallback noun phrases for the same product, for use if "
    "the first phrase segments poorly. suggested_title: a marketplace "
    "listing title. listing_bullets: 3 to 5 short selling points a buyer "
    "can verify from the photo. condition_notes: visible wear, damage, "
    "or defects; say so plainly, or state that none are visible."
)


def _usage_of(response) -> UsageDict:
    """Map an OpenAI-compatible usage object to our usage dict.

    prompt_tokens is the total input; its cached subset rides in
    prompt_tokens_details.cached_tokens (the convention pricing.py
    documents).
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    return {
        "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "cached_input_tokens": int(cached),
        "images": 0,
    }


def _merge_usage(a: UsageDict, b: UsageDict) -> UsageDict:
    return {key: int(a.get(key, 0)) + int(b.get(key, 0)) for key in _USAGE_KEYS}


def _parse_identify_payload(content: str) -> dict:
    """Parse and contract-check one identify response body.

    Raises IdentifyResultError on malformed JSON or any field that
    violates the contract (types, listing_bullets 3-5, alternates >= 2
    after cleaning). The alternates shortfall is the one defect the
    caller may repair with a follow-up call, so it is raised with a
    distinguishable message prefix.
    """
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise IdentifyResultError(
            f"Identify response is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise IdentifyResultError("Identify response is not a JSON object.")

    for field in ("description", "concept", "suggested_title", "condition_notes"):
        if not isinstance(payload.get(field), str) or not payload[field].strip():
            raise IdentifyResultError(
                f"Identify response field {field!r} is missing or not a "
                "non-empty string."
            )
    bullets = payload.get("listing_bullets")
    if (
        not isinstance(bullets, list)
        or not 3 <= len(bullets) <= 5
        or not all(isinstance(b, str) and b.strip() for b in bullets)
    ):
        raise IdentifyResultError(
            "Identify response field 'listing_bullets' must be 3 to 5 "
            "non-empty strings."
        )

    concept = payload["concept"].strip()
    raw_alternates = payload.get("alternates")
    if not isinstance(raw_alternates, list):
        raise IdentifyResultError(
            "ALTERNATES_SHORTFALL: 'alternates' is not a list."
        )
    alternates: list[str] = []
    for item in raw_alternates:
        if isinstance(item, str):
            phrase = item.strip()
            if phrase and phrase.lower() != concept.lower() and phrase not in alternates:
                alternates.append(phrase)
    if len(alternates) < 2:
        raise IdentifyResultError(
            "ALTERNATES_SHORTFALL: fewer than 2 usable alternates after "
            f"cleaning (got {len(alternates)})."
        )

    return {
        "description": payload["description"].strip(),
        "concept": concept,
        "alternates": alternates,
        "suggested_title": payload["suggested_title"].strip(),
        "listing_bullets": [b.strip() for b in bullets],
        "condition_notes": payload["condition_notes"].strip(),
    }


def vision_identify(
    png_bytes: bytes,
    *,
    model: str,
    client=None,
) -> tuple[dict, UsageDict]:
    """One real identify call against the Meta Model API.

    Sends the normalized RGBA PNG to the vision model with a strict
    structured-output request and returns (plan, usage). If the first
    response's only defect is too few usable alternates, ONE text-only
    repair call asks for more phrases (its usage is added to the total);
    a response that is still unusable raises IdentifyResultError.

    This function performs network I/O and must only be invoked as the
    fn of billed_call, so the spend is reserved before it runs and
    settled (or left counted) afterwards. `client` is injectable for
    offline tests; when omitted, the real client is built (which raises
    NotConfiguredError if no key is configured).
    """
    if client is None:
        client = build_client()
    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
    response = client.chat.completions.create(
        model=model,
        max_tokens=IDENTIFY_MAX_TOKENS,
        response_format={"type": "json_schema", "json_schema": _IDENTIFY_SCHEMA},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _IDENTIFY_INSTRUCTIONS},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }
        ],
    )
    usage = _usage_of(response)
    content = response.choices[0].message.content or ""
    try:
        return _parse_identify_payload(content), usage
    except IdentifyResultError as exc:
        if not str(exc).startswith("ALTERNATES_SHORTFALL"):
            raise

    # Repair path: the response was usable except for its alternates.
    # One text-only follow-up asks for replacement phrases; the concept
    # and the rest of the plan are kept from the first response.
    partial = json.loads(content)
    concept = str(partial.get("concept", "")).strip()
    repair = client.chat.completions.create(
        model=model,
        max_tokens=256,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "identify_alternates_repair",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "alternates": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                        }
                    },
                    "required": ["alternates"],
                },
            },
        },
        messages=[
            {
                "role": "user",
                "content": (
                    "A segmentation plan for a product photo uses the "
                    f"noun phrase {concept!r}. Give at least 2 different "
                    "fallback noun phrases for the same product, each "
                    "short and naming the product only."
                ),
            }
        ],
    )
    usage = _merge_usage(usage, _usage_of(repair))
    repair_content = repair.choices[0].message.content or ""
    try:
        repair_payload = json.loads(repair_content)
        extra = repair_payload["alternates"]
        if not isinstance(extra, list):
            raise TypeError("'alternates' is not a list")
    except (json.JSONDecodeError, TypeError, KeyError) as exc:
        raise IdentifyResultError(
            f"Alternates repair response is unusable: {exc}"
        ) from exc
    merged = dict(partial)
    merged["alternates"] = list(partial.get("alternates") or []) + list(extra)
    # Final contract check; raises if the repair did not fix the shortfall.
    return _parse_identify_payload(json.dumps(merged)), usage
