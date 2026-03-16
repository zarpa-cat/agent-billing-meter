"""Tests for AuditLog."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_billing_meter.audit_log import AuditLog
from agent_billing_meter.models import DebitResult


@pytest.fixture()
def log(tmp_path: Path) -> AuditLog:
    return AuditLog(db_path=tmp_path / "test.db")


def make_result(
    success: bool = True,
    user: str = "user_1",
    op: str = "llm_call",
    amount: int = 5,
    balance_before: int | None = None,
    balance_after: int | None = None,
    error: str | None = None,
) -> DebitResult:
    return DebitResult(
        success=success,
        app_user_id=user,
        operation=op,
        amount_debited=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        error=error,
    )


def test_store_and_query_basic(log: AuditLog) -> None:
    r = make_result()
    row_id = log.store(r)
    assert isinstance(row_id, int)
    assert row_id > 0

    results = log.query()
    assert len(results) == 1
    assert results[0].app_user_id == "user_1"
    assert results[0].operation == "llm_call"
    assert results[0].amount_debited == 5
    assert results[0].success is True


def test_store_multiple(log: AuditLog) -> None:
    for i in range(5):
        log.store(make_result(amount=i + 1))
    results = log.query()
    assert len(results) == 5


def test_filter_by_user(log: AuditLog) -> None:
    log.store(make_result(user="alice"))
    log.store(make_result(user="bob"))
    log.store(make_result(user="alice"))
    assert len(log.query(app_user_id="alice")) == 2
    assert len(log.query(app_user_id="bob")) == 1


def test_filter_by_operation(log: AuditLog) -> None:
    log.store(make_result(op="llm_call"))
    log.store(make_result(op="tool_use"))
    log.store(make_result(op="llm_call"))
    assert len(log.query(operation="llm_call")) == 2
    assert len(log.query(operation="tool_use")) == 1


def test_filter_by_since(log: AuditLog) -> None:
    old = make_result()
    old.timestamp = time.time() - 7200  # 2h ago
    log.store(old)
    log.store(make_result())  # now

    since = time.time() - 3600  # 1h ago
    results = log.query(since=since)
    assert len(results) == 1


def test_filter_success_only(log: AuditLog) -> None:
    log.store(make_result(success=True))
    log.store(make_result(success=False, error="rc_error"))
    assert len(log.query(success_only=True)) == 1


def test_total_debited_all(log: AuditLog) -> None:
    log.store(make_result(amount=10))
    log.store(make_result(amount=5))
    log.store(make_result(success=False, amount=20))  # not counted
    assert log.total_debited() == 15


def test_total_debited_by_user(log: AuditLog) -> None:
    log.store(make_result(user="alice", amount=10))
    log.store(make_result(user="bob", amount=20))
    assert log.total_debited(app_user_id="alice") == 10
    assert log.total_debited(app_user_id="bob") == 20


def test_total_debited_by_operation(log: AuditLog) -> None:
    log.store(make_result(op="llm_call", amount=5))
    log.store(make_result(op="tool_use", amount=2))
    assert log.total_debited(operation="llm_call") == 5
    assert log.total_debited(operation="tool_use") == 2


def test_unique_users(log: AuditLog) -> None:
    log.store(make_result(user="alice"))
    log.store(make_result(user="bob"))
    log.store(make_result(user="alice"))
    users = log.unique_users()
    assert sorted(users) == ["alice", "bob"]


def test_store_with_balances(log: AuditLog) -> None:
    r = make_result(balance_before=100, balance_after=90)
    log.store(r)
    results = log.query()
    assert results[0].balance_before == 100
    assert results[0].balance_after == 90


def test_store_failure_with_error(log: AuditLog) -> None:
    r = make_result(success=False, error="HTTP 402")
    log.store(r)
    results = log.query()
    assert results[0].error == "HTTP 402"
    assert results[0].success is False


def test_limit(log: AuditLog) -> None:
    for _ in range(10):
        log.store(make_result())
    assert len(log.query(limit=3)) == 3


def test_empty_db(log: AuditLog) -> None:
    assert log.query() == []
    assert log.total_debited() == 0
    assert log.unique_users() == []


def test_store_metadata(log: AuditLog) -> None:
    r = make_result()
    row_id = log.store(r, metadata={"model": "gpt-4", "tokens": 1500})
    assert row_id > 0
    # Metadata stored; no field on DebitResult but row exists
    results = log.query()
    assert len(results) == 1
