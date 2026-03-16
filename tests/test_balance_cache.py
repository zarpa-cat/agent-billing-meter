"""Tests for BalanceCache."""

from __future__ import annotations

import time

import pytest

from agent_billing_meter.balance_cache import BalanceCache


def test_get_miss() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    assert cache.get("user_1", "credits") is None


def test_set_and_get() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    cache.set("user_1", "credits", 500)
    assert cache.get("user_1", "credits") == 500


def test_different_users_isolated() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    cache.set("user_a", "credits", 100)
    cache.set("user_b", "credits", 200)
    assert cache.get("user_a", "credits") == 100
    assert cache.get("user_b", "credits") == 200


def test_different_currencies_isolated() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    cache.set("user_1", "credits", 100)
    cache.set("user_1", "tokens", 999)
    assert cache.get("user_1", "credits") == 100
    assert cache.get("user_1", "tokens") == 999


def test_expired_returns_none() -> None:
    cache = BalanceCache(ttl_seconds=0.001)
    cache.set("user_1", "credits", 300)
    time.sleep(0.002)
    assert cache.get("user_1", "credits") is None


def test_expired_entry_removed() -> None:
    cache = BalanceCache(ttl_seconds=0.001)
    cache.set("user_1", "credits", 300)
    assert cache.size() == 1
    time.sleep(0.002)
    cache.get("user_1", "credits")  # triggers eviction
    assert cache.size() == 0


def test_invalidate() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    cache.set("user_1", "credits", 100)
    cache.invalidate("user_1", "credits")
    assert cache.get("user_1", "credits") is None


def test_invalidate_nonexistent_is_noop() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    cache.invalidate("nobody", "credits")  # should not raise


def test_clear() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    cache.set("user_1", "credits", 100)
    cache.set("user_2", "tokens", 200)
    cache.clear()
    assert cache.size() == 0
    assert cache.get("user_1", "credits") is None


def test_overwrite() -> None:
    cache = BalanceCache(ttl_seconds=60.0)
    cache.set("user_1", "credits", 100)
    cache.set("user_1", "credits", 250)
    assert cache.get("user_1", "credits") == 250


def test_invalid_ttl() -> None:
    with pytest.raises(ValueError):
        BalanceCache(ttl_seconds=0.0)
    with pytest.raises(ValueError):
        BalanceCache(ttl_seconds=-1.0)
