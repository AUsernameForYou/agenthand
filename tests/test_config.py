"""Spend-cap configuration: ceiling, default, fail-closed parsing."""

import pytest

from agenthand import config


def test_default_cap_is_the_working_cap(monkeypatch):
    monkeypatch.delenv("AGENTHAND_TOTAL_SPEND_CAP_USD", raising=False)
    assert config.get_total_spend_cap_usd() == 120.00
    assert config.DEFAULT_TOTAL_SPEND_CAP_USD == 120.00
    assert config.HARD_CEILING_USD == 150.00


def test_env_can_lower_the_cap(monkeypatch):
    monkeypatch.setenv("AGENTHAND_TOTAL_SPEND_CAP_USD", "25.50")
    assert config.get_total_spend_cap_usd() == 25.50


def test_env_cannot_raise_the_cap_past_the_ceiling(monkeypatch):
    monkeypatch.setenv("AGENTHAND_TOTAL_SPEND_CAP_USD", "99999")
    assert config.get_total_spend_cap_usd() == 150.00


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "abc", "1.2.3"])
def test_uninterpretable_cap_fails_closed(monkeypatch, bad):
    monkeypatch.setenv("AGENTHAND_TOTAL_SPEND_CAP_USD", bad)
    with pytest.raises(ValueError):
        config.get_total_spend_cap_usd()


def test_negative_cap_fails_closed(monkeypatch):
    monkeypatch.setenv("AGENTHAND_TOTAL_SPEND_CAP_USD", "-5")
    with pytest.raises(ValueError):
        config.get_total_spend_cap_usd()
