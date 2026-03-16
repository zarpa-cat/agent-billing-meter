"""BillingMeter: context manager + decorator for metered agent operations."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

from agent_billing_meter.audit_log import AuditLog
from agent_billing_meter.models import DebitResult
from agent_billing_meter.rc_client import RCClient

P = ParamSpec("P")
R = TypeVar("R")


class BillingMeter:
    """Meter agent operations against RevenueCat virtual currency credits.

    Usage as context manager::

        async with BillingMeter(api_key="...", app_user_id="user_123") as meter:
            result = await meter.debit(10, "llm_call")

    Usage as decorator factory::

        meter = BillingMeter(api_key="...", app_user_id="user_123")

        @meter.metered(cost=5, operation="generate_report")
        async def generate_report(prompt: str) -> str:
            ...
    """

    def __init__(
        self,
        api_key: str,
        app_user_id: str,
        currency: str = "credits",
        db_path: str | None = None,
        log_all: bool = True,
        raise_on_failure: bool = False,
    ) -> None:
        self._api_key = api_key
        self.app_user_id = app_user_id
        self.currency = currency
        self._log_all = log_all
        self._raise_on_failure = raise_on_failure
        self._audit: AuditLog = AuditLog(db_path=db_path)
        self._rc: RCClient | None = None

    async def __aenter__(self) -> BillingMeter:
        self._rc = RCClient(api_key=self._api_key)
        await self._rc.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._rc:
            await self._rc.__aexit__(*args)
            self._rc = None

    def _ensure_rc(self) -> RCClient:
        if self._rc is None:
            raise RuntimeError(
                "BillingMeter must be used as an async context manager before calling debit(). "
                "Use: async with BillingMeter(...) as meter: ..."
            )
        return self._rc

    async def debit(
        self,
        amount: int,
        operation: str,
        metadata: dict[str, object] | None = None,
    ) -> DebitResult:
        """Debit `amount` credits for `operation`.

        Returns DebitResult. If raise_on_failure=True, raises on RC API errors.
        Always logs to the audit log when log_all=True.
        """
        rc = self._ensure_rc()
        ts = time.time()
        balance_before: int | None = None
        balance_after: int | None = None

        try:
            # Attempt to get balance before (best-effort, don't fail if unavailable)
            try:
                balance_before = await rc.get_balance(self.app_user_id, self.currency)
            except Exception:
                pass

            # Perform debit
            resp = await rc.debit_currency(
                app_user_id=self.app_user_id,
                currency=self.currency,
                amount=amount,
                metadata=metadata,
            )

            # Extract balance_after from response if available
            vc = resp.get("virtual_currencies", {})
            vc_data = vc.get(self.currency, {}) if isinstance(vc, dict) else {}
            raw_balance = vc_data.get("balance") if isinstance(vc_data, dict) else None
            balance_after = int(raw_balance) if raw_balance is not None else None

            result = DebitResult(
                success=True,
                app_user_id=self.app_user_id,
                operation=operation,
                amount_debited=amount,
                timestamp=ts,
                balance_before=balance_before,
                balance_after=balance_after,
            )

        except Exception as exc:
            result = DebitResult(
                success=False,
                app_user_id=self.app_user_id,
                operation=operation,
                amount_debited=amount,
                timestamp=ts,
                balance_before=balance_before,
                balance_after=None,
                error=str(exc),
            )
            if self._raise_on_failure:
                if self._log_all:
                    self._audit.store(result, metadata)
                raise

        if self._log_all:
            self._audit.store(result, metadata)

        return result

    def metered(
        self,
        cost: int = 1,
        operation: str | None = None,
    ) -> Callable[[Callable[P, Coroutine[Any, Any, R]]], Callable[P, Coroutine[Any, Any, R]]]:
        """Decorator factory. Debits `cost` credits after wrapped function succeeds.

        The debit fires only when the function completes without raising.
        If the BillingMeter is not entered as a context manager when the wrapped
        function is called, the debit is skipped with a warning.

        Example::

            @meter.metered(cost=3, operation="summarize")
            async def summarize(text: str) -> str:
                ...
        """

        def decorator(
            fn: Callable[P, Coroutine[Any, Any, R]],
        ) -> Callable[P, Coroutine[Any, Any, R]]:
            op_name = operation or fn.__name__

            @functools.wraps(fn)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                result = await fn(*args, **kwargs)
                # Debit after success
                if self._rc is not None:
                    await self.debit(cost, op_name)
                return result

            return wrapper

        return decorator

    # ── Convenience helpers ──────────────────────────────────────────────────

    async def balance(self) -> int | None:
        """Return current balance for this user / currency."""
        return await self._ensure_rc().get_balance(self.app_user_id, self.currency)

    def history(self, limit: int = 50) -> list[DebitResult]:
        """Return recent debit history for this user from the audit log."""
        return self._audit.query(app_user_id=self.app_user_id, limit=limit)

    def total_spent(self) -> int:
        """Total credits debited successfully for this user."""
        return self._audit.total_debited(app_user_id=self.app_user_id)

    @property
    def audit(self) -> AuditLog:
        return self._audit


class BudgetExceededError(Exception):
    """Raised when a budget cap would be exceeded."""


class BudgetedMeter(BillingMeter):
    """BillingMeter with a hard session budget cap.

    Raises BudgetExceededError (before the debit) when the running total
    for this session would exceed `budget`.
    """

    def __init__(self, *args: Any, budget: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._budget = budget
        self._session_spent: int = 0
        self._lock = asyncio.Lock()

    async def debit(
        self,
        amount: int,
        operation: str,
        metadata: dict[str, object] | None = None,
    ) -> DebitResult:
        async with self._lock:
            if self._session_spent + amount > self._budget:
                raise BudgetExceededError(
                    f"Budget of {self._budget} would be exceeded: "
                    f"spent={self._session_spent}, requested={amount}"
                )
            result = await super().debit(amount, operation, metadata)
            if result.success:
                self._session_spent += amount
            return result

    @property
    def session_spent(self) -> int:
        return self._session_spent

    @property
    def remaining_budget(self) -> int:
        return max(0, self._budget - self._session_spent)
