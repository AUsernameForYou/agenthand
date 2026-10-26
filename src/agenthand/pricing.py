"""The single pricing table for Meta Model API usage.

All USD estimates in the cost ledger come from here. When Meta changes a
price, edit the number and bump PRICING_TABLE_VERSION; old receipts keep the
version they were written with, so historical spend stays auditable.

Prices verified against the Meta Model API pricing page, 2026-10-26.
"""

from __future__ import annotations

PRICING_TABLE_VERSION = "2026-10-26.1"

# USD per 1M tokens.
TOKEN_PRICES_USD_PER_1M: dict[str, dict[str, float]] = {
    "muse-spark-1.3": {"input": 1.25, "output": 4.25, "cached_input": 0.15},
    "muse-wonder-1.0": {"input": 4.00, "output": 20.00, "cached_input": 0.20},
}

# USD per generated image.
PER_IMAGE_PRICES_USD: dict[str, float] = {
    "muse-image-1.0": 0.01,
}

# USD per 1,000 processed images.
PER_1000_IMAGES_PRICES_USD: dict[str, float] = {
    "sam-3.1": 2.50,
}


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    images: int = 0,
) -> float:
    """Estimate the USD cost of one model call.

    Token-priced models take token counts (cached input is billed at its own
    cheaper rate, so it is counted separately from fresh input). Per-image
    and per-1000-image models take an image count. A model present in more
    than one table would be charged under each; none currently is.
    """
    total = 0.0

    token_prices = TOKEN_PRICES_USD_PER_1M.get(model)
    if token_prices is not None:
        total += input_tokens * token_prices["input"] / 1_000_000
        total += output_tokens * token_prices["output"] / 1_000_000
        total += cached_input_tokens * token_prices["cached_input"] / 1_000_000

    if model in PER_IMAGE_PRICES_USD:
        total += images * PER_IMAGE_PRICES_USD[model]

    if model in PER_1000_IMAGES_PRICES_USD:
        total += images * PER_1000_IMAGES_PRICES_USD[model] / 1000

    return round(total, 6)
