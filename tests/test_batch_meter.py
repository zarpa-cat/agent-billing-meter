"""Tests for BatchMeter (batched debit + debounce) and balance cache integration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agent_billing_meter.meter import BatchMeter, BillingMeter


def make_batch_meter(
    tmp_path: Path,
    debounce_ms: float = 0.0,
    auto_flush: bool = True,
) -> BatchMeter:
    return BatchMeter(
        api_key="test_key",
        app_user_id="user_batch",
        currency="credits",
        db_path=str(tmp_path / "batch.db"),
        debounce_ms=debounce_ms,
        auto_flush=auto_flush,
    )


def _mock_rc(balance_before: int = 100, balance_after: int = 90) -> MagicMock:
    rc = AsyncMock()
    rc.__aenter__ = AsyncMock(return_value=rc)
    rc.__aexit__ = AsyncMock(return_value=None)
    rc.get_balance = AsyncMock(return_value=balance_before)
    rc.debit_currency = AsyncMock(
        return_value={"virtual_currencies": {"credits": {"balance": balance_after}}}
    )
    return rc


# ── queue_debit + flush ────────────────────────────────────────────────────────


async def test_queue_and_flush(tmp_path: Path) -> None:
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.queue_debit(5, "embed")
            await meter.queue_debit(3, "embed")
            await meter.queue_debit(10, "llm_call")
            results = await meter.flush()

    # Two unique operations: embed (5+3=8) and llm_call (10)
    assert len(results) == 2
    ops = {r.operation: r.amount_debited for r in results}
    assert ops["embed"] == 8
    assert ops["llm_call"] == 10
    # RC debit should be called exactly twice
    assert mock_rc.debit_currency.call_count == 2


async def test_flush_empty_returns_empty(tmp_path: Path) -> None:
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            results = await meter.flush()
    assert results == []
    mock_rc.debit_currency.assert_not_called()


async def test_auto_flush_on_exit(tmp_path: Path) -> None:
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path, auto_flush=True)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.queue_debit(7, "summarize")
            await meter.queue_debit(7, "summarize")
    # Auto-flush fired: one call, amount=14
    assert mock_rc.debit_currency.call_count == 1
    call_kwargs = mock_rc.debit_currency.call_args.kwargs
    assert call_kwargs["amount"] == 14
    assert call_kwargs["app_user_id"] == "user_batch"


async def test_no_auto_flush(tmp_path: Path) -> None:
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path, auto_flush=False)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.queue_debit(5, "op")
    # No flush → no RC calls
    mock_rc.debit_currency.assert_not_called()


async def test_pending_count(tmp_path: Path) -> None:
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path, auto_flush=False)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            assert meter.pending_count == 0
            await meter.queue_debit(1, "op1")
            await meter.queue_debit(2, "op2")
            assert meter.pending_count == 2
            await meter.flush()
            assert meter.pending_count == 0


# ── debounce ──────────────────────────────────────────────────────────────────


async def test_debounce_accumulates_within_window(tmp_path: Path) -> None:
    """Calls within the debounce window accumulate; only fire on flush."""
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path, debounce_ms=500.0)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            # All 5 calls within 500ms window
            r1 = await meter.debit(1, "search")
            r2 = await meter.debit(1, "search")
            r3 = await meter.debit(1, "search")
            r4 = await meter.debit(1, "search")
            r5 = await meter.debit(1, "search")

            # None should have fired RC yet (all accumulated)
            assert mock_rc.debit_currency.call_count == 0

            # Synthetic results are all success=True
            for r in (r1, r2, r3, r4, r5):
                assert r.success is True

    # Auto-flush on exit fires accumulated amount = 5
    assert mock_rc.debit_currency.call_count == 1
    call_kwargs = mock_rc.debit_currency.call_args.kwargs
    assert call_kwargs["amount"] == 5


async def test_debounce_fires_on_window_expiry(tmp_path: Path) -> None:
    """When window expires, the accumulated batch fires on the NEXT call."""
    mock_rc = _mock_rc(balance_before=100, balance_after=97)
    meter = make_batch_meter(tmp_path, debounce_ms=10.0, auto_flush=False)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            # First call: opens window (synthetic, no RC)
            await meter.debit(1, "search")
            await meter.debit(2, "search")
            # No RC calls yet
            assert mock_rc.debit_currency.call_count == 0

            # Wait for window to expire
            await asyncio.sleep(0.02)

            # Next call: window expired → fires accumulated (1+2=3), opens new window
            await meter.debit(1, "search")
            assert mock_rc.debit_currency.call_count == 1
            assert mock_rc.debit_currency.call_args.kwargs["amount"] == 3

            # Flush remaining (the last call's amount=1)
            await meter.flush()
            assert mock_rc.debit_currency.call_count == 2
            assert mock_rc.debit_currency.call_args.kwargs["amount"] == 1


async def test_debounce_zero_fires_immediately(tmp_path: Path) -> None:
    """debounce_ms=0 means immediate firing (normal BillingMeter behavior)."""
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path, debounce_ms=0.0)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.debit(5, "llm_call")
            await meter.debit(3, "llm_call")

    assert mock_rc.debit_currency.call_count == 2


async def test_debounce_different_ops_independent(tmp_path: Path) -> None:
    """Different operation names have independent debounce windows."""
    mock_rc = _mock_rc()
    meter = make_batch_meter(tmp_path, debounce_ms=500.0, auto_flush=False)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.debit(1, "op_a")
            await meter.debit(2, "op_b")
            await meter.debit(3, "op_a")
            # No RC yet — all within windows
            assert mock_rc.debit_currency.call_count == 0
            await meter.flush()

    # After flush: op_a=4, op_b=2 → 2 RC calls
    assert mock_rc.debit_currency.call_count == 2
    amounts = sorted(c.kwargs["amount"] for c in mock_rc.debit_currency.call_args_list)
    assert amounts == [2, 4]


# ── balance cache integration ─────────────────────────────────────────────────


async def test_balance_cache_avoids_extra_get(tmp_path: Path) -> None:
    """With balance cache, the second debit should NOT call get_balance."""
    mock_rc = _mock_rc(balance_before=200, balance_after=190)
    meter = BillingMeter(
        api_key="test",
        app_user_id="user_c",
        currency="credits",
        db_path=str(tmp_path / "c.db"),
        balance_cache_ttl_s=60.0,
    )
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            r1 = await meter.debit(10, "op1")
            r2 = await meter.debit(10, "op2")

    # get_balance called only once (first debit; second uses cache)
    assert mock_rc.get_balance.call_count == 1
    assert r1.balance_before == 200
    # r2 reads balance_before from cache (populated by r1's balance_after=190)
    assert r2.balance_before == 190


async def test_balance_cache_disabled_by_default(tmp_path: Path) -> None:
    """Without cache, get_balance is called before every debit."""
    mock_rc = _mock_rc(balance_before=100, balance_after=95)
    meter = BillingMeter(
        api_key="test",
        app_user_id="user_d",
        currency="credits",
        db_path=str(tmp_path / "d.db"),
        # No balance_cache_ttl_s → default 0.0 (disabled)
    )
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.debit(5, "op1")
            await meter.debit(5, "op2")

    assert mock_rc.get_balance.call_count == 2


async def test_balance_cache_invalidated_on_failure(tmp_path: Path) -> None:
    """Cache is invalidated when a debit fails — avoids stale reads."""
    call_count = 0

    async def flaky_debit(**kwargs: object) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise Exception("RC 500")
        return {"virtual_currencies": {"credits": {"balance": 90}}}

    mock_rc = AsyncMock()
    mock_rc.__aenter__ = AsyncMock(return_value=mock_rc)
    mock_rc.__aexit__ = AsyncMock(return_value=None)
    mock_rc.get_balance = AsyncMock(return_value=100)
    mock_rc.debit_currency = flaky_debit  # type: ignore[method-assign]

    meter = BillingMeter(
        api_key="test",
        app_user_id="user_e",
        currency="credits",
        db_path=str(tmp_path / "e.db"),
        balance_cache_ttl_s=60.0,
    )
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            # First debit succeeds, populates cache
            r1 = await meter.debit(10, "op1")
            assert r1.success is True

            # Second debit fails, cache should be invalidated
            r2 = await meter.debit(10, "op2")
            assert r2.success is False

            # Third debit: cache is empty, should re-fetch balance from RC
            r3 = await meter.debit(10, "op3")
            assert r3.success is True

    # get_balance called for 1st debit and 3rd (after cache invalidated by 2nd failure)
    assert mock_rc.get_balance.call_count == 2


async def test_balance_method_uses_cache(tmp_path: Path) -> None:
    """meter.balance() returns cached value without RC call when warm."""
    mock_rc = _mock_rc(balance_before=500, balance_after=490)
    meter = BillingMeter(
        api_key="test",
        app_user_id="user_f",
        currency="credits",
        db_path=str(tmp_path / "f.db"),
        balance_cache_ttl_s=60.0,
    )
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.debit(10, "op")  # populates cache with balance_after=490
            bal = await meter.balance()

    assert bal == 490
    # get_balance was called once (during debit, before cache warm)
    assert mock_rc.get_balance.call_count == 1
