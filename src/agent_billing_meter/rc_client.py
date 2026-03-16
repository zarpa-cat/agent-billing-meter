"""Async RevenueCat client for virtual currency operations."""

from __future__ import annotations

import httpx

RC_BASE = "https://api.revenuecat.com/v1"


class RCClient:
    """Thin async client for RevenueCat virtual currency API."""

    def __init__(self, api_key: str, base_url: str = RC_BASE) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "X-Platform": "stripe",
        }

    async def __aenter__(self) -> RCClient:
        self._client = httpx.AsyncClient(headers=self._headers(), timeout=10.0)
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("RCClient must be used as an async context manager")
        return self._client

    async def debit_currency(
        self,
        app_user_id: str,
        currency: str,
        amount: int,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """POST /v1/subscribers/{app_user_id}/virtual_currencies/{currency}/debit.

        Returns the parsed JSON response or raises httpx.HTTPStatusError.
        """
        client = self._ensure_client()
        url = f"{self._base_url}/subscribers/{app_user_id}/virtual_currencies/{currency}/debit"
        body: dict[str, object] = {"amount": amount}
        if metadata:
            body["metadata"] = metadata
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]

    async def get_subscriber(self, app_user_id: str) -> dict[str, object]:
        """GET /v1/subscribers/{app_user_id}."""
        client = self._ensure_client()
        url = f"{self._base_url}/subscribers/{app_user_id}"
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]

    async def get_balance(self, app_user_id: str, currency: str) -> int | None:
        """Return current virtual currency balance or None if not found."""
        data = await self.get_subscriber(app_user_id)
        subscriber = data.get("subscriber", {})
        virtual_currencies = subscriber.get("virtual_currencies", {})
        vc = virtual_currencies.get(currency, {})
        balance = vc.get("balance")
        if balance is None:
            return None
        return int(balance)
