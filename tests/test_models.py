"""Tests for DebitResult model."""

from __future__ import annotations

import time

from agent_billing_meter.models import DebitResult


def test_debit_result_defaults() -> None:
    r = DebitResult(
        success=True,
        app_user_id="user_1",
        operation="llm_call",
        amount_debited=5,
    )
    assert r.success is True
    assert r.app_user_id == "user_1"
    assert r.operation == "llm_call"
    assert r.amount_debited == 5
    assert r.balance_before is None
    assert r.balance_after is None
    assert r.error is None
    assert abs(r.timestamp - time.time()) < 2


def test_debit_result_failed_property() -> None:
    r = DebitResult(success=False, app_user_id="u", operation="op", amount_debited=1)
    assert r.failed is True


def test_debit_result_success_not_failed() -> None:
    r = DebitResult(success=True, app_user_id="u", operation="op", amount_debited=1)
    assert r.failed is False


def test_debit_result_with_balances() -> None:
    r = DebitResult(
        success=True,
        app_user_id="u",
        operation="op",
        amount_debited=10,
        balance_before=100,
        balance_after=90,
    )
    assert r.balance_before == 100
    assert r.balance_after == 90


def test_debit_result_with_error() -> None:
    r = DebitResult(
        success=False,
        app_user_id="u",
        operation="op",
        amount_debited=5,
        error="HTTP 402: Insufficient credits",
    )
    assert r.error == "HTTP 402: Insufficient credits"
    assert r.failed is True
