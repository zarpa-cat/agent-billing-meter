"""SpendPolicy: configurable spend rules enforced before any debit fires."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


class PolicyViolationError(Exception):
    """Raised when a SpendPolicy rule blocks a debit.

    Attributes:
        operation: The operation that was blocked.
        amount: The amount that was requested.
        rule: A short string identifying the violated rule.
        reason: Human-readable explanation.
    """

    def __init__(
        self,
        operation: str,
        amount: int,
        rule: str,
        reason: str,
    ) -> None:
        self.operation = operation
        self.amount = amount
        self.rule = rule
        self.reason = reason
        super().__init__(f"[{rule}] {reason}")


@dataclass
class SpendPolicy:
    """Declarative spend policy evaluated before each debit.

    Rules are checked in order; the first violation raises PolicyViolationError.

    Attributes:
        blocked_ops:
            Operations that are always denied, regardless of amount.
            Example: ``["delete_data", "send_email"]``

        allowed_ops:
            Allowlist of permitted operation names. If set, any operation *not*
            in this list is denied. ``None`` means all operations are allowed.
            Example: ``["llm_call", "embed_chunk", "search"]``

        op_max_per_call:
            Per-call credit cap by operation name.  A debit larger than the cap
            for that operation is denied.
            Example: ``{"llm_call": 50, "embed_chunk": 5}``

        op_max_per_hour:
            Rolling 1-hour credit cap per operation (checked against the audit
            log for the current user).  A debit that would push the hourly total
            over the cap is denied.
            Example: ``{"llm_call": 500}``

        max_per_hour:
            Rolling 1-hour credit cap across *all* operations for the current
            user. ``None`` disables this check.

        max_per_day:
            Rolling 24-hour credit cap across all operations for the current
            user. ``None`` disables this check.

    Example::

        policy = SpendPolicy(
            blocked_ops=["purge_all"],
            allowed_ops=["llm_call", "embed_chunk"],
            op_max_per_call={"llm_call": 100},
            op_max_per_hour={"llm_call": 2000},
            max_per_hour=5000,
            max_per_day=20000,
        )

        async with PolicyMeter(api_key="...", app_user_id="u1", policy=policy) as meter:
            await meter.debit(10, "llm_call")   # ok
            await meter.debit(200, "llm_call")  # raises — op_max_per_call
            await meter.debit(1, "purge_all")   # raises — blocked_ops
    """

    blocked_ops: list[str] = field(default_factory=list)
    allowed_ops: list[str] | None = None
    op_max_per_call: dict[str, int] = field(default_factory=dict)
    op_max_per_hour: dict[str, int] = field(default_factory=dict)
    max_per_hour: int | None = None
    max_per_day: int | None = None

    def check(
        self,
        operation: str,
        amount: int,
        *,
        audit_log: AuditLogProtocol | None = None,
        app_user_id: str | None = None,
    ) -> None:
        """Evaluate all policy rules.  Raises PolicyViolationError on first hit.

        Args:
            operation: The operation name being debited.
            amount: The credit amount being debited.
            audit_log: Optional audit log used for time-window checks.
            app_user_id: User whose history is checked (required when audit_log
                is provided and time-window rules are set).
        """
        # 1. Denylist
        if operation in self.blocked_ops:
            raise PolicyViolationError(
                operation=operation,
                amount=amount,
                rule="blocked_ops",
                reason=f"Operation '{operation}' is in the blocked list.",
            )

        # 2. Allowlist
        if self.allowed_ops is not None and operation not in self.allowed_ops:
            raise PolicyViolationError(
                operation=operation,
                amount=amount,
                rule="allowed_ops",
                reason=(
                    f"Operation '{operation}' is not in the allowed list. "
                    f"Allowed: {self.allowed_ops!r}"
                ),
            )

        # 3. Per-call cap
        if operation in self.op_max_per_call:
            cap = self.op_max_per_call[operation]
            if amount > cap:
                raise PolicyViolationError(
                    operation=operation,
                    amount=amount,
                    rule="op_max_per_call",
                    reason=(
                        f"Debit of {amount} exceeds per-call cap of {cap} "
                        f"for operation '{operation}'."
                    ),
                )

        # Time-window checks require an audit log
        if audit_log is None or app_user_id is None:
            return

        now = time.time()

        # 4. Per-operation hourly cap
        if operation in self.op_max_per_hour:
            cap = self.op_max_per_hour[operation]
            since = now - 3600.0
            spent = audit_log.total_debited(
                app_user_id=app_user_id,
                operation=operation,
                since=since,
            )
            if spent + amount > cap:
                raise PolicyViolationError(
                    operation=operation,
                    amount=amount,
                    rule="op_max_per_hour",
                    reason=(
                        f"Debit of {amount} for '{operation}' would push "
                        f"hourly total to {spent + amount}, exceeding cap of {cap}."
                    ),
                )

        # 5. Global hourly cap
        if self.max_per_hour is not None:
            since = now - 3600.0
            spent = audit_log.total_debited(app_user_id=app_user_id, since=since)
            if spent + amount > self.max_per_hour:
                raise PolicyViolationError(
                    operation=operation,
                    amount=amount,
                    rule="max_per_hour",
                    reason=(
                        f"Debit of {amount} would push hourly total to "
                        f"{spent + amount}, exceeding global cap of {self.max_per_hour}."
                    ),
                )

        # 6. Global daily cap
        if self.max_per_day is not None:
            since = now - 86400.0
            spent = audit_log.total_debited(app_user_id=app_user_id, since=since)
            if spent + amount > self.max_per_day:
                raise PolicyViolationError(
                    operation=operation,
                    amount=amount,
                    rule="max_per_day",
                    reason=(
                        f"Debit of {amount} would push daily total to "
                        f"{spent + amount}, exceeding global cap of {self.max_per_day}."
                    ),
                )


# ---------------------------------------------------------------------------
# Structural type alias (avoids circular import)
# ---------------------------------------------------------------------------

from typing import Protocol  # noqa: E402


class AuditLogProtocol(Protocol):
    """Subset of AuditLog used by SpendPolicy."""

    def total_debited(
        self,
        app_user_id: str | None = None,
        operation: str | None = None,
        since: float | None = None,
    ) -> int: ...
