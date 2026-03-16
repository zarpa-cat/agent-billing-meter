# agent-billing-meter

Meter agent operations and debit RevenueCat virtual currency credits.

Agents don't pay per-seat. They pay per unit of work. `agent-billing-meter` is the layer between your agent's function calls and your RevenueCat project's virtual currency balance — a context manager, a decorator, an audit log, and a CLI.

## Install

```bash
pip install agent-billing-meter
# or
uv add agent-billing-meter
```

## Usage

### Context manager

```python
from agent_billing_meter import BillingMeter

async with BillingMeter(
    api_key="rc_sk_...",
    app_user_id="agent_session_xyz",
    currency="credits",
) as meter:
    # Do your work
    result = await call_llm(prompt)

    # Debit 10 credits for the LLM call
    debit = await meter.debit(10, "llm_call", metadata={"model": "claude-3-7"})
    if not debit.success:
        logger.warning(f"Billing failed: {debit.error}")
```

### Decorator

```python
meter = BillingMeter(api_key="rc_sk_...", app_user_id="agent_123")

@meter.metered(cost=5, operation="generate_report")
async def generate_report(prompt: str) -> str:
    return await llm.complete(prompt)

# Automatically debits 5 credits when generate_report() completes successfully.
# No debit if it raises.
async with meter:
    report = await generate_report("Summarize Q1 metrics")
```

### Hard budget cap

```python
from agent_billing_meter import BudgetedMeter, BudgetExceededError

async with BudgetedMeter(
    api_key="rc_sk_...",
    app_user_id="agent_123",
    budget=100,  # session hard cap
) as meter:
    try:
        await meter.debit(50, "expensive_op")
        await meter.debit(60, "another_op")  # raises: would total 110 > 100
    except BudgetExceededError:
        logger.error("Agent session budget exhausted")

print(f"Session spent: {meter.session_spent}")
print(f"Remaining: {meter.remaining_budget}")
```

### CLI

```bash
# Debit manually
abm debit user_123 10 --operation test_run --api-key rc_sk_...

# Show history
abm history user_123

# Aggregate stats
abm stats
abm stats --user user_123
```

## How it works

1. **`BillingMeter.debit()`** opens an httpx async session and POSTs to RC's virtual currency debit endpoint
2. Every debit (success or failure) is recorded in a local SQLite audit log (`~/.agent-billing-meter.db`)
3. The `@metered` decorator wraps async functions — debit fires *after* success, never on exception
4. `BudgetedMeter` tracks a running session total and raises `BudgetExceededError` before touching the RC API

## Part of the agentic billing stack

- [rc-entitlement-gate](https://github.com/zarpa-cat/rc-entitlement-gate) — access control (can this agent use this feature?)
- **agent-billing-meter** — metering (how much did this agent consume?)
- [churnwall](https://github.com/zarpa-cat/churnwall) — retention (is this agent/user at risk of churning?)

## Development

```bash
uv sync --dev
uv run pytest tests/ -v
uv run ruff check .
```

## License

MIT
