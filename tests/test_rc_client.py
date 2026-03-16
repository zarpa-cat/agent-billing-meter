"""Tests for RCClient (mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from agent_billing_meter.rc_client import RC_BASE, RCClient


@pytest.fixture()
def client() -> RCClient:
    return RCClient(api_key="test_key_abc")


@respx.mock
async def test_debit_currency_success(client: RCClient) -> None:
    route = respx.post(f"{RC_BASE}/subscribers/user_1/virtual_currencies/credits/debit").mock(
        return_value=httpx.Response(
            200,
            json={"virtual_currencies": {"credits": {"balance": 90}}},
        )
    )
    async with client:
        resp = await client.debit_currency("user_1", "credits", 10)
    assert route.called
    assert resp["virtual_currencies"]["credits"]["balance"] == 90


@respx.mock
async def test_debit_currency_with_metadata(client: RCClient) -> None:
    route = respx.post(f"{RC_BASE}/subscribers/user_2/virtual_currencies/credits/debit").mock(
        return_value=httpx.Response(200, json={"virtual_currencies": {"credits": {"balance": 50}}})
    )
    async with client:
        resp = await client.debit_currency("user_2", "credits", 5, metadata={"op": "summarize"})
    assert route.called
    assert resp["virtual_currencies"]["credits"]["balance"] == 50


@respx.mock
async def test_debit_currency_raises_on_4xx(client: RCClient) -> None:
    respx.post(f"{RC_BASE}/subscribers/user_3/virtual_currencies/credits/debit").mock(
        return_value=httpx.Response(402, json={"error": "insufficient_credits"})
    )
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.debit_currency("user_3", "credits", 999)


@respx.mock
async def test_get_subscriber_success(client: RCClient) -> None:
    payload = {
        "subscriber": {
            "virtual_currencies": {"credits": {"balance": 200}},
            "subscriptions": {},
        }
    }
    respx.get(f"{RC_BASE}/subscribers/user_4").mock(return_value=httpx.Response(200, json=payload))
    async with client:
        data = await client.get_subscriber("user_4")
    assert data["subscriber"]["virtual_currencies"]["credits"]["balance"] == 200


@respx.mock
async def test_get_balance_returns_int(client: RCClient) -> None:
    payload = {"subscriber": {"virtual_currencies": {"credits": {"balance": 75}}}}
    respx.get(f"{RC_BASE}/subscribers/user_5").mock(return_value=httpx.Response(200, json=payload))
    async with client:
        balance = await client.get_balance("user_5", "credits")
    assert balance == 75


@respx.mock
async def test_get_balance_returns_none_if_missing(client: RCClient) -> None:
    payload = {"subscriber": {"virtual_currencies": {}}}
    respx.get(f"{RC_BASE}/subscribers/user_6").mock(return_value=httpx.Response(200, json=payload))
    async with client:
        balance = await client.get_balance("user_6", "credits")
    assert balance is None


def test_client_requires_context_manager(client: RCClient) -> None:
    with pytest.raises(RuntimeError, match="async context manager"):
        import asyncio

        asyncio.get_event_loop().run_until_complete(client.debit_currency("u", "credits", 1))


@respx.mock
async def test_debit_sends_correct_auth_header(client: RCClient) -> None:
    route = respx.post(f"{RC_BASE}/subscribers/user_7/virtual_currencies/credits/debit").mock(
        return_value=httpx.Response(200, json={})
    )
    async with client:
        await client.debit_currency("user_7", "credits", 1)
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer test_key_abc"


@respx.mock
async def test_debit_sends_correct_amount(client: RCClient) -> None:
    import json

    route = respx.post(f"{RC_BASE}/subscribers/user_8/virtual_currencies/credits/debit").mock(
        return_value=httpx.Response(200, json={})
    )
    async with client:
        await client.debit_currency("user_8", "credits", 42)
    body = json.loads(route.calls[0].request.content)
    assert body["amount"] == 42
