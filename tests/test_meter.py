"""Tests for BillingMeter and BudgetedMeter."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_billing_meter.meter import BillingMeter, BudgetedMeter, BudgetExceededError


def make_meter(tmp_path: Path, raise_on_failure: bool = False) -> BillingMeter:
    return BillingMeter(
        api_key="test_key",
        app_user_id="user_test",
        currency="credits",
        db_path=str(tmp_path / "test.db"),
        raise_on_failure=raise_on_failure,
    )


def _mock_rc(balance_before: int = 100, balance_after: int = 90) -> MagicMock:
    """Return a mock RCClient."""
    rc = AsyncMock()
    rc.__aenter__ = AsyncMock(return_value=rc)
    rc.__aexit__ = AsyncMock(return_value=None)
    rc.get_balance = AsyncMock(return_value=balance_before)
    rc.debit_currency = AsyncMock(
        return_value={"virtual_currencies": {"credits": {"balance": balance_after}}}
    )
    return rc


@pytest.fixture()
def mock_rc() -> MagicMock:
    return _mock_rc()


async def test_debit_success(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            result = await meter.debit(10, "llm_call")

    assert result.success is True
    assert result.amount_debited == 10
    assert result.operation == "llm_call"
    assert result.balance_before == 100
    assert result.balance_after == 90


async def test_debit_logged(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.debit(5, "tool_use")

    history = meter.history()
    assert len(history) == 1
    assert history[0].operation == "tool_use"


async def test_debit_failure_no_raise(tmp_path: Path) -> None:
    rc = AsyncMock()
    rc.__aenter__ = AsyncMock(return_value=rc)
    rc.__aexit__ = AsyncMock(return_value=None)
    rc.get_balance = AsyncMock(side_effect=Exception("network error"))
    rc.debit_currency = AsyncMock(side_effect=Exception("RC 402"))

    meter = make_meter(tmp_path, raise_on_failure=False)
    with patch("agent_billing_meter.meter.RCClient", return_value=rc):
        async with meter:
            result = await meter.debit(10, "llm_call")

    assert result.success is False
    assert "RC 402" in (result.error or "")


async def test_debit_failure_raises(tmp_path: Path) -> None:
    rc = AsyncMock()
    rc.__aenter__ = AsyncMock(return_value=rc)
    rc.__aexit__ = AsyncMock(return_value=None)
    rc.get_balance = AsyncMock(return_value=None)
    rc.debit_currency = AsyncMock(side_effect=Exception("RC 402"))

    meter = make_meter(tmp_path, raise_on_failure=True)
    with patch("agent_billing_meter.meter.RCClient", return_value=rc):
        async with meter:
            with pytest.raises(Exception, match="RC 402"):
                await meter.debit(10, "llm_call")


async def test_metered_decorator(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)

    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:

            @meter.metered(cost=3, operation="summarize")
            async def summarize(text: str) -> str:
                return f"Summary of: {text}"

            result = await summarize("hello world")

    assert result == "Summary of: hello world"
    mock_rc.debit_currency.assert_called_once()
    call_args = mock_rc.debit_currency.call_args
    assert call_args.kwargs["amount"] == 3


async def test_metered_no_debit_on_exception(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)

    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:

            @meter.metered(cost=5, operation="failing_op")
            async def failing_fn() -> str:
                raise ValueError("Something broke")

            with pytest.raises(ValueError):
                await failing_fn()

    # Debit should NOT have been called
    mock_rc.debit_currency.assert_not_called()


async def test_metered_uses_fn_name_as_operation(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)

    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:

            @meter.metered(cost=1)
            async def my_tool() -> str:
                return "done"

            await my_tool()

    call_args = mock_rc.debit_currency.call_args
    assert call_args.kwargs["app_user_id"] == "user_test"


async def test_total_spent(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.debit(5, "op1")
            await meter.debit(10, "op2")

    assert meter.total_spent() == 15


async def test_requires_context_manager(tmp_path: Path) -> None:
    meter = make_meter(tmp_path)
    with pytest.raises(RuntimeError, match="async context manager"):
        await meter.debit(5, "op")


# ── BudgetedMeter tests ───────────────────────────────────────────────────────


async def test_budgeted_meter_within_budget(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = BudgetedMeter(
        api_key="test",
        app_user_id="user_b",
        budget=50,
        db_path=str(tmp_path / "b.db"),
    )
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            result = await meter.debit(20, "op")

    assert result.success is True
    assert meter.session_spent == 20
    assert meter.remaining_budget == 30


async def test_budgeted_meter_exceeds_budget(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = BudgetedMeter(
        api_key="test",
        app_user_id="user_b",
        budget=10,
        db_path=str(tmp_path / "b.db"),
    )
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            with pytest.raises(BudgetExceededError):
                await meter.debit(11, "op")

    # Should not have debited
    mock_rc.debit_currency.assert_not_called()
    assert meter.session_spent == 0


async def test_budgeted_meter_cumulative_tracking(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = BudgetedMeter(
        api_key="test",
        app_user_id="user_b",
        budget=25,
        db_path=str(tmp_path / "b.db"),
    )
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            await meter.debit(10, "op1")
            await meter.debit(10, "op2")
            with pytest.raises(BudgetExceededError):
                await meter.debit(10, "op3")  # would total 30, exceeds 25

    assert meter.session_spent == 20
    assert meter.remaining_budget == 5


async def test_context_manager_protocol(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter as m:
            assert m is meter
            result = await m.debit(1, "ping")
    assert result.success is True


async def test_debit_with_metadata(tmp_path: Path, mock_rc: MagicMock) -> None:
    meter = make_meter(tmp_path)
    with patch("agent_billing_meter.meter.RCClient", return_value=mock_rc):
        async with meter:
            result = await meter.debit(5, "llm_call", metadata={"model": "claude-3"})
    assert result.success is True
    call_args = mock_rc.debit_currency.call_args
    assert call_args.kwargs["metadata"] == {"model": "claude-3"}
