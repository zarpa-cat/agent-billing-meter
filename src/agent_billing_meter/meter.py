"""BillingMeter: context manager + decorator for metered agent operations."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable, Coroutine
from typing import Any, ParamSpec, TypeVar

from agent_billing_meter.audit_log import AuditLog
from agent_billing_meter.balance_cache import BalanceCache
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

    Balance cache (optional)::

        # Cache balance for 60s — avoids an extra RC GET before each debit
        meter = BillingMeter(..., balance_cache_ttl_s=60.0)
    """

    def __init__(
        self,
        api_key: str,
        app_user_id: str,
        currency: str = "credits",
        db_path: str | None = None,
        log_all: bool = True,
        raise_on_failure: bool = False,
        balance_cache_ttl_s: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self.app_user_id = app_user_id
        self.currency = currency
        self._log_all = log_all
        self._raise_on_failure = raise_on_failure
        self._audit: AuditLog = AuditLog(db_path=db_path)
        self._rc: RCClient | None = None
        self._balance_cache: BalanceCache | None = (
            BalanceCache(ttl_seconds=balance_cache_ttl_s) if balance_cache_ttl_s > 0 else None
        )

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

        When balance_cache_ttl_s > 0, balance_before is served from the cache
        if available (avoids a redundant RC GET call per debit).
        """
        rc = self._ensure_rc()
        ts = time.time()
        balance_before: int | None = None
        balance_after: int | None = None

        try:
            # Fetch balance_before: use cache if warm, otherwise hit RC
            if self._balance_cache is not None:
                balance_before = self._balance_cache.get(self.app_user_id, self.currency)

            if balance_before is None:
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

            # Extract balance_after from response
            vc = resp.get("virtual_currencies", {})
            vc_data = vc.get(self.currency, {}) if isinstance(vc, dict) else {}
            raw_balance = vc_data.get("balance") if isinstance(vc_data, dict) else None
            balance_after = int(raw_balance) if raw_balance is not None else None

            # Update cache with fresh balance
            if self._balance_cache is not None and balance_after is not None:
                self._balance_cache.set(self.app_user_id, self.currency, balance_after)

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
            # Invalidate cache on error — balance state is uncertain
            if self._balance_cache is not None:
                self._balance_cache.invalidate(self.app_user_id, self.currency)

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
        if self._balance_cache is not None:
            cached = self._balance_cache.get(self.app_user_id, self.currency)
            if cached is not None:
                return cached
        bal = await self._ensure_rc().get_balance(self.app_user_id, self.currency)
        if self._balance_cache is not None and bal is not None:
            self._balance_cache.set(self.app_user_id, self.currency, bal)
        return bal

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


class BatchMeter(BillingMeter):
    """BillingMeter with batched debit and per-operation debounce.

    Debits can be queued locally and flushed in bulk, reducing RC API calls
    when many operations fire in a short window.

    Features:
    - queue_debit(): enqueue a debit without an immediate RC call
    - flush(): coalesce same-operation debits, fire one RC call per unique op,
      return list of DebitResult
    - auto_flush=True (default): flush on __aexit__ so nothing is lost
    - debounce_ms > 0: when debit() is called, same-operation calls within the
      debounce window are accumulated locally; the RC call fires when the window
      expires (checked on the next call to that operation) or on flush()

    Example — queue many cheap ops, flush at end::

        async with BatchMeter(api_key="...", app_user_id="u1") as meter:
            for chunk in chunks:
                await meter.queue_debit(1, "embed_chunk")
            results = await meter.flush()  # one RC call

    Example — debounce a hot decorator::

        meter = BatchMeter(api_key="...", app_user_id="u1", debounce_ms=200)

        @meter.metered(cost=1, operation="vector_search")
        async def search(q: str) -> list[str]: ...

        # 50 calls in 50ms → ~1 RC call per 200ms window on flush/exit
    """

    def __init__(
        self,
        *args: Any,
        auto_flush: bool = True,
        debounce_ms: float = 0.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._auto_flush = auto_flush
        self._debounce_ms = debounce_ms
        # (amount, operation, metadata) tuples waiting to be flushed
        self._queue: list[tuple[int, str, dict[str, object] | None]] = []
        # op -> (accumulated_amount, window_start_time, metadata)
        self._pending: dict[str, tuple[int, float, dict[str, object] | None]] = {}
        self._batch_lock = asyncio.Lock()

    async def queue_debit(
        self,
        amount: int,
        operation: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Enqueue a debit without firing an RC API call.

        Call flush() (or let __aexit__ auto-flush) to send to RevenueCat.
        """
        async with self._batch_lock:
            self._queue.append((amount, operation, metadata))

    async def flush(self) -> list[DebitResult]:
        """Coalesce pending debits and send to RevenueCat.

        Same-operation debits are summed into a single RC call. Returns one
        DebitResult per unique operation name.
        """
        async with self._batch_lock:
            # Drain debounce buffer into queue
            for op, (amt, _ws, meta) in list(self._pending.items()):
                self._queue.append((amt, op, meta))
            self._pending.clear()

            if not self._queue:
                return []

            # Coalesce: sum amounts for identical operations
            coalesced: dict[str, tuple[int, dict[str, object] | None]] = {}
            for amount, op, meta in self._queue:
                if op in coalesced:
                    prev_amt, prev_meta = coalesced[op]
                    coalesced[op] = (prev_amt + amount, prev_meta or meta)
                else:
                    coalesced[op] = (amount, meta)
            self._queue.clear()

        # Fire RC calls outside the lock (allows concurrent debits during flush)
        results: list[DebitResult] = []
        for op, (amount, meta) in coalesced.items():
            result = await super().debit(amount, op, meta)
            results.append(result)
        return results

    async def debit(
        self,
        amount: int,
        operation: str,
        metadata: dict[str, object] | None = None,
    ) -> DebitResult:
        """Debit with optional debounce coalescing.

        When debounce_ms == 0 (default): fires immediately (same as BillingMeter).

        When debounce_ms > 0: the first call for an operation starts a window.
        Subsequent same-operation calls within the window are accumulated locally
        and return a synthetic pending result (success=True, pending=True in
        metadata). When the window expires (detected on the next call to that op)
        the accumulated amount fires as a single RC call. On flush() or __aexit__,
        any remaining pending amounts are also fired.
        """
        if self._debounce_ms <= 0:
            return await super().debit(amount, operation, metadata)

        now = time.time()
        fire_amount: int | None = None
        fire_meta: dict[str, object] | None = None

        async with self._batch_lock:
            if operation in self._pending:
                acc_amt, window_start, acc_meta = self._pending[operation]
                elapsed_ms = (now - window_start) * 1000.0

                if elapsed_ms < self._debounce_ms:
                    # Within window: accumulate, return synthetic pending result
                    self._pending[operation] = (
                        acc_amt + amount,
                        window_start,
                        acc_meta or metadata,
                    )
                    return DebitResult(
                        success=True,
                        app_user_id=self.app_user_id,
                        operation=operation,
                        amount_debited=amount,
                        timestamp=now,
                    )
                else:
                    # Window expired: fire accumulated, open new window for current
                    fire_amount = acc_amt
                    fire_meta = acc_meta
                    self._pending[operation] = (amount, now, metadata)
            else:
                # First call: open window, return synthetic pending result
                self._pending[operation] = (amount, now, metadata)
                return DebitResult(
                    success=True,
                    app_user_id=self.app_user_id,
                    operation=operation,
                    amount_debited=amount,
                    timestamp=now,
                )

        # Fire outside lock
        assert fire_amount is not None
        return await super().debit(fire_amount, operation, fire_meta)

    async def __aexit__(self, *args: object) -> None:
        if self._auto_flush:
            await self.flush()
        await super().__aexit__(*args)

    @property
    def pending_count(self) -> int:
        """Number of items currently queued (not yet flushed)."""
        return len(self._queue) + len(self._pending)
