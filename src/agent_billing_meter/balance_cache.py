"""In-memory balance cache with TTL for BillingMeter."""

from __future__ import annotations

import time


class BalanceCache:
    """TTL-based in-memory cache for virtual currency balances.

    Keys on (app_user_id, currency). Thread-safe for async use (no shared state
    mutation during async yields).

    Usage::

        cache = BalanceCache(ttl_seconds=60.0)
        cache.set("user_123", "credits", 500)
        balance = cache.get("user_123", "credits")  # 500 or None if expired
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        # (app_user_id, currency) -> (balance, expires_at)
        self._store: dict[tuple[str, str], tuple[int, float]] = {}

    def get(self, app_user_id: str, currency: str) -> int | None:
        """Return cached balance, or None if missing/expired."""
        key = (app_user_id, currency)
        entry = self._store.get(key)
        if entry is None:
            return None
        balance, expires_at = entry
        if time.time() > expires_at:
            del self._store[key]
            return None
        return balance

    def set(self, app_user_id: str, currency: str, balance: int) -> None:
        """Store balance with TTL starting now."""
        self._store[(app_user_id, currency)] = (balance, time.time() + self._ttl)

    def invalidate(self, app_user_id: str, currency: str) -> None:
        """Remove cached entry for a specific user/currency."""
        self._store.pop((app_user_id, currency), None)

    def clear(self) -> None:
        """Evict all cached entries."""
        self._store.clear()

    def size(self) -> int:
        """Number of cached entries (including potentially expired ones)."""
        return len(self._store)
