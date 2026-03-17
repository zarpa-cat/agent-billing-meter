"""Tests for SpendPolicy, PolicyViolationError, and PolicyMeter."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_billing_meter import DebitResult, PolicyMeter
from agent_billing_meter.policy import PolicyViolationError, SpendPolicy

# ---------------------------------------------------------------------------
# SpendPolicy.check() unit tests (no meter, no RC)
# ---------------------------------------------------------------------------


class TestSpendPolicyBlockedOps:
    def test_blocked_op_raises(self) -> None:
        policy = SpendPolicy(blocked_ops=["purge_all"])
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("purge_all", 1)
        err = exc_info.value
        assert err.rule == "blocked_ops"
        assert err.operation == "purge_all"
        assert err.amount == 1

    def test_allowed_op_passes(self) -> None:
        policy = SpendPolicy(blocked_ops=["purge_all"])
        policy.check("llm_call", 10)  # must not raise

    def test_empty_blocked_list_passes(self) -> None:
        policy = SpendPolicy()
        policy.check("anything", 1)


class TestSpendPolicyAllowedOps:
    def test_op_not_in_allowlist_raises(self) -> None:
        policy = SpendPolicy(allowed_ops=["llm_call", "embed_chunk"])
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("send_email", 5)
        err = exc_info.value
        assert err.rule == "allowed_ops"

    def test_op_in_allowlist_passes(self) -> None:
        policy = SpendPolicy(allowed_ops=["llm_call"])
        policy.check("llm_call", 50)

    def test_none_allowlist_allows_everything(self) -> None:
        policy = SpendPolicy(allowed_ops=None)
        policy.check("anything_goes", 999)


class TestSpendPolicyOpMaxPerCall:
    def test_amount_over_cap_raises(self) -> None:
        policy = SpendPolicy(op_max_per_call={"llm_call": 50})
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("llm_call", 51)
        err = exc_info.value
        assert err.rule == "op_max_per_call"

    def test_amount_at_cap_passes(self) -> None:
        policy = SpendPolicy(op_max_per_call={"llm_call": 50})
        policy.check("llm_call", 50)

    def test_amount_under_cap_passes(self) -> None:
        policy = SpendPolicy(op_max_per_call={"llm_call": 50})
        policy.check("llm_call", 10)

    def test_uncapped_op_passes(self) -> None:
        policy = SpendPolicy(op_max_per_call={"llm_call": 50})
        policy.check("embed_chunk", 1000)  # no cap for this op


class TestSpendPolicyTimeWindowRules:
    """Time-window rules use a mock audit log."""

    def _mock_audit(self, spent: int) -> MagicMock:
        audit = MagicMock()
        audit.total_debited.return_value = spent
        return audit

    def test_op_max_per_hour_blocks_when_over(self) -> None:
        policy = SpendPolicy(op_max_per_hour={"llm_call": 100})
        audit = self._mock_audit(90)
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("llm_call", 20, audit_log=audit, app_user_id="u1")
        assert exc_info.value.rule == "op_max_per_hour"

    def test_op_max_per_hour_passes_when_under(self) -> None:
        policy = SpendPolicy(op_max_per_hour={"llm_call": 100})
        audit = self._mock_audit(50)
        policy.check("llm_call", 49, audit_log=audit, app_user_id="u1")

    def test_op_max_per_hour_exact_cap_passes(self) -> None:
        policy = SpendPolicy(op_max_per_hour={"llm_call": 100})
        audit = self._mock_audit(90)
        policy.check("llm_call", 10, audit_log=audit, app_user_id="u1")

    def test_max_per_hour_global_blocks(self) -> None:
        policy = SpendPolicy(max_per_hour=500)
        audit = self._mock_audit(490)
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("embed_chunk", 20, audit_log=audit, app_user_id="u1")
        assert exc_info.value.rule == "max_per_hour"

    def test_max_per_hour_global_passes(self) -> None:
        policy = SpendPolicy(max_per_hour=500)
        audit = self._mock_audit(400)
        policy.check("embed_chunk", 99, audit_log=audit, app_user_id="u1")

    def test_max_per_day_blocks(self) -> None:
        policy = SpendPolicy(max_per_day=1000)
        audit = self._mock_audit(990)
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("llm_call", 20, audit_log=audit, app_user_id="u1")
        assert exc_info.value.rule == "max_per_day"

    def test_max_per_day_passes(self) -> None:
        policy = SpendPolicy(max_per_day=1000)
        audit = self._mock_audit(500)
        policy.check("llm_call", 499, audit_log=audit, app_user_id="u1")

    def test_time_window_rules_skipped_without_audit(self) -> None:
        """Without an audit log, time-window rules are not evaluated."""
        policy = SpendPolicy(max_per_hour=1, max_per_day=1, op_max_per_hour={"op": 1})
        # Would fail all three time-window checks if audit log were present
        policy.check("op", 999, audit_log=None, app_user_id="u1")

    def test_time_window_rules_skipped_without_user(self) -> None:
        """Without app_user_id, time-window rules are not evaluated."""
        policy = SpendPolicy(max_per_hour=1)
        audit = self._mock_audit(999)
        # audit_log present but no user_id — should not raise
        policy.check("op", 999, audit_log=audit, app_user_id=None)

    def test_audit_called_with_correct_since_for_hour(self) -> None:
        policy = SpendPolicy(max_per_hour=1000)
        audit = self._mock_audit(0)
        before = time.time()
        policy.check("op", 1, audit_log=audit, app_user_id="u1")
        after = time.time()

        # Extract the `since` kwarg from the total_debited call
        since_vals = [
            call.kwargs.get("since")
            for call in audit.total_debited.call_args_list
            if call.kwargs.get("since") is not None
        ]
        assert any(before - 3601 <= s <= after - 3599 for s in since_vals)

    def test_audit_called_with_correct_since_for_day(self) -> None:
        policy = SpendPolicy(max_per_day=1000)
        audit = self._mock_audit(0)
        before = time.time()
        policy.check("op", 1, audit_log=audit, app_user_id="u1")
        after = time.time()

        # since should be approximately now - 86400
        since_vals = [
            call.kwargs.get("since")
            for call in audit.total_debited.call_args_list
            if call.kwargs.get("since") is not None
        ]
        assert any(before - 86401 <= s <= after - 86399 for s in since_vals)


class TestSpendPolicyRuleOrdering:
    """blocked_ops is checked before allowed_ops; op_max_per_call before time-windows."""

    def test_blocked_takes_priority_over_allowed(self) -> None:
        policy = SpendPolicy(
            blocked_ops=["op"],
            allowed_ops=["op"],  # contradictory: blocked wins
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("op", 1)
        assert exc_info.value.rule == "blocked_ops"

    def test_allowed_checked_before_per_call_cap(self) -> None:
        policy = SpendPolicy(
            allowed_ops=["other"],
            op_max_per_call={"op": 1000},
        )
        with pytest.raises(PolicyViolationError) as exc_info:
            policy.check("op", 1)
        assert exc_info.value.rule == "allowed_ops"


# ---------------------------------------------------------------------------
# PolicyMeter integration tests (mock RC client)
# ---------------------------------------------------------------------------


def _debit_response(balance: int = 90, currency: str = "credits") -> dict:
    return {"virtual_currencies": {currency: {"balance": balance}}}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_policy.db"


@pytest.fixture
def permissive_policy() -> SpendPolicy:
    return SpendPolicy()  # no rules — everything allowed


@pytest.fixture
def strict_policy() -> SpendPolicy:
    return SpendPolicy(
        blocked_ops=["purge_all"],
        allowed_ops=["llm_call", "embed_chunk"],
        op_max_per_call={"llm_call": 100},
        max_per_hour=5000,
    )


class TestPolicyMeterPermissive:
    @pytest.mark.asyncio
    async def test_debit_succeeds_with_no_rules(
        self, db_path: Path, permissive_policy: SpendPolicy
    ) -> None:
        with patch("agent_billing_meter.rc_client.httpx.AsyncClient") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = _debit_response()
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async with PolicyMeter(
                api_key="test",
                app_user_id="u1",
                policy=permissive_policy,
                db_path=str(db_path),
            ) as meter:
                result = await meter.debit(10, "anything")

        assert result.success is True
        assert result.amount_debited == 10

    @pytest.mark.asyncio
    async def test_policy_property(self, db_path: Path, permissive_policy: SpendPolicy) -> None:
        meter = PolicyMeter(
            api_key="test",
            app_user_id="u1",
            policy=permissive_policy,
            db_path=str(db_path),
        )
        assert meter.policy is permissive_policy


class TestPolicyMeterBlocking:
    @pytest.mark.asyncio
    async def test_blocked_op_raises_before_rc_call(
        self, db_path: Path, strict_policy: SpendPolicy
    ) -> None:
        with patch("agent_billing_meter.rc_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async with PolicyMeter(
                api_key="test",
                app_user_id="u1",
                policy=strict_policy,
                db_path=str(db_path),
            ) as meter:
                with pytest.raises(PolicyViolationError) as exc_info:
                    await meter.debit(1, "purge_all")

            # No RC call should have been made
            mock_client.post.assert_not_called()

        assert exc_info.value.rule == "blocked_ops"

    @pytest.mark.asyncio
    async def test_disallowed_op_raises_before_rc_call(
        self, db_path: Path, strict_policy: SpendPolicy
    ) -> None:
        with patch("agent_billing_meter.rc_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async with PolicyMeter(
                api_key="test",
                app_user_id="u1",
                policy=strict_policy,
                db_path=str(db_path),
            ) as meter:
                with pytest.raises(PolicyViolationError) as exc_info:
                    await meter.debit(1, "send_webhook")

            mock_client.post.assert_not_called()
        assert exc_info.value.rule == "allowed_ops"

    @pytest.mark.asyncio
    async def test_over_per_call_cap_raises(
        self, db_path: Path, strict_policy: SpendPolicy
    ) -> None:
        with patch("agent_billing_meter.rc_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async with PolicyMeter(
                api_key="test",
                app_user_id="u1",
                policy=strict_policy,
                db_path=str(db_path),
            ) as meter:
                with pytest.raises(PolicyViolationError) as exc_info:
                    await meter.debit(101, "llm_call")

            mock_client.post.assert_not_called()
        assert exc_info.value.rule == "op_max_per_call"

    @pytest.mark.asyncio
    async def test_allowed_op_at_cap_succeeds(
        self, db_path: Path, strict_policy: SpendPolicy
    ) -> None:
        with patch("agent_billing_meter.rc_client.httpx.AsyncClient") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = _debit_response()
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async with PolicyMeter(
                api_key="test",
                app_user_id="u1",
                policy=strict_policy,
                db_path=str(db_path),
            ) as meter:
                result = await meter.debit(100, "llm_call")

        assert result.success is True


class TestPolicyMeterHourlyWindow:
    """Hourly cap — uses real audit log to verify time-window integration."""

    @pytest.mark.asyncio
    async def test_hourly_cap_enforced_via_audit_log(self, db_path: Path) -> None:
        """Seed audit log, then verify PolicyMeter enforces the hourly cap."""
        from agent_billing_meter.audit_log import AuditLog

        # Seed the audit log with 490 credits already spent in the last hour
        audit = AuditLog(db_path=str(db_path))
        seed = DebitResult(
            success=True,
            app_user_id="u1",
            operation="llm_call",
            amount_debited=490,
            timestamp=time.time() - 100,  # 100s ago, within 1h window
        )
        audit.store(seed)

        policy = SpendPolicy(max_per_hour=500)

        with patch("agent_billing_meter.rc_client.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async with PolicyMeter(
                api_key="test",
                app_user_id="u1",
                policy=policy,
                db_path=str(db_path),
            ) as meter:
                # 11 would push to 501 > 500 — should be blocked
                with pytest.raises(PolicyViolationError) as exc_info:
                    await meter.debit(11, "llm_call")

            mock_client.post.assert_not_called()

        assert exc_info.value.rule == "max_per_hour"

    @pytest.mark.asyncio
    async def test_old_debits_not_counted_in_hourly_window(self, db_path: Path) -> None:
        """Debits older than 1h must not count toward the hourly cap."""
        from agent_billing_meter.audit_log import AuditLog

        audit = AuditLog(db_path=str(db_path))
        old = DebitResult(
            success=True,
            app_user_id="u1",
            operation="llm_call",
            amount_debited=490,
            timestamp=time.time() - 3700,  # >1h ago
        )
        audit.store(old)

        policy = SpendPolicy(max_per_hour=500)

        with patch("agent_billing_meter.rc_client.httpx.AsyncClient") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = _debit_response()
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            async with PolicyMeter(
                api_key="test",
                app_user_id="u1",
                policy=policy,
                db_path=str(db_path),
            ) as meter:
                # Old debits don't count — 499 should pass
                result = await meter.debit(499, "llm_call")

        assert result.success is True


class TestPolicyViolationErrorAttributes:
    def test_attributes_populated(self) -> None:
        err = PolicyViolationError(
            operation="do_thing",
            amount=42,
            rule="test_rule",
            reason="Exceeded limit.",
        )
        assert err.operation == "do_thing"
        assert err.amount == 42
        assert err.rule == "test_rule"
        assert err.reason == "Exceeded limit."
        assert "[test_rule]" in str(err)
        assert "Exceeded limit." in str(err)

    def test_is_exception(self) -> None:
        err = PolicyViolationError("op", 1, "r", "msg")
        assert isinstance(err, Exception)
