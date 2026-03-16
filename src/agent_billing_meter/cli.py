"""CLI for agent-billing-meter (abm)."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from agent_billing_meter.audit_log import AuditLog

app = typer.Typer(help="Agent Billing Meter — track and debit RC virtual currency credits.")


def _get_audit(db_path: str | None) -> AuditLog:
    path = db_path or os.environ.get("ABM_DB_PATH")
    return AuditLog(db_path=path)


@app.command()
def history(
    user_id: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    db_path: Annotated[str | None, typer.Option("--db")] = None,
) -> None:
    """Show debit history for a user."""
    audit = _get_audit(db_path)
    results = audit.query(app_user_id=user_id, limit=limit)
    if not results:
        typer.echo(f"No history found for {user_id}")
        return
    for r in results:
        import datetime

        ts = datetime.datetime.fromtimestamp(r.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        status = "✓" if r.success else "✗"
        typer.echo(f"{ts}  {status}  -{r.amount_debited:>6}  {r.operation}")
        if not r.success and r.error:
            typer.echo(f"         error: {r.error}")


@app.command()
def stats(
    db_path: Annotated[str | None, typer.Option("--db")] = None,
    user_id: Annotated[str | None, typer.Option("--user")] = None,
) -> None:
    """Aggregate stats across all users (or a specific user)."""
    audit = _get_audit(db_path)

    if user_id:
        users = [user_id]
    else:
        users = audit.unique_users()

    if not users:
        typer.echo("No data in audit log.")
        return

    typer.echo(f"{'USER':<30}  {'TOTAL SPENT':>12}  {'OPERATIONS':>10}")
    typer.echo("-" * 58)
    grand_total = 0
    for uid in users:
        total = audit.total_debited(app_user_id=uid)
        ops = len(audit.query(app_user_id=uid, success_only=True, limit=10000))
        typer.echo(f"{uid:<30}  {total:>12,}  {ops:>10,}")
        grand_total += total
    typer.echo("-" * 58)
    typer.echo(f"{'TOTAL':<30}  {grand_total:>12,}")


@app.command()
def debit(
    user_id: str,
    amount: int,
    operation: Annotated[str, typer.Option("--operation", "-o")] = "manual",
    api_key: Annotated[str | None, typer.Option("--api-key")] = None,
    currency: Annotated[str, typer.Option("--currency")] = "credits",
    db_path: Annotated[str | None, typer.Option("--db")] = None,
) -> None:
    """Manually debit credits for a user (requires RC API key)."""
    import asyncio

    from agent_billing_meter.meter import BillingMeter

    key = api_key or os.environ.get("RC_API_KEY", "")
    if not key:
        typer.echo("Error: RC API key required. Use --api-key or set RC_API_KEY.", err=True)
        raise typer.Exit(1)

    async def _run() -> None:
        meter = BillingMeter(
            api_key=key,
            app_user_id=user_id,
            currency=currency,
            db_path=db_path,
        )
        async with meter:
            result = await meter.debit(amount, operation)
        if result.success:
            typer.echo(f"✓ Debited {amount} {currency} from {user_id} for {operation}")
            if result.balance_after is not None:
                typer.echo(f"  Balance after: {result.balance_after}")
        else:
            typer.echo(f"✗ Debit failed: {result.error}", err=True)
            raise typer.Exit(1)

    asyncio.run(_run())
