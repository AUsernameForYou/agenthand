"""Pricing table math, pinned to the verified Meta Model API prices."""

from agenthand import pricing


def test_spark_token_prices():
    # 1M fresh input + 1M output on muse-spark-1.3 = $1.25 + $4.25.
    assert pricing.estimate_cost_usd(
        "muse-spark-1.3", input_tokens=1_000_000, output_tokens=1_000_000
    ) == 5.50


def test_spark_cached_input_is_cheaper():
    assert pricing.estimate_cost_usd(
        "muse-spark-1.3", cached_input_tokens=1_000_000
    ) == 0.15


def test_wonder_token_prices():
    assert pricing.estimate_cost_usd(
        "muse-wonder-1.0", input_tokens=1_000_000, output_tokens=1_000_000
    ) == 24.00
    assert pricing.estimate_cost_usd(
        "muse-wonder-1.0", cached_input_tokens=1_000_000
    ) == 0.20


def test_image_model_per_image():
    assert pricing.estimate_cost_usd("muse-image-1.0", images=3) == 0.03


def test_sam_per_thousand_images():
    assert pricing.estimate_cost_usd("sam-3.1", images=1000) == 2.50
    assert pricing.estimate_cost_usd("sam-3.1", images=1) == 0.0025


def test_unknown_model_costs_zero():
    assert pricing.estimate_cost_usd("no-such-model", input_tokens=10**9) == 0.0


def test_partial_token_counts():
    # 500k input tokens on Spark = half of $1.25.
    assert pricing.estimate_cost_usd("muse-spark-1.3", input_tokens=500_000) == 0.625
