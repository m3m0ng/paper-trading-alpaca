from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bot.config import Settings


class AlpacaError(RuntimeError):
    pass


class AlpacaNotFoundError(AlpacaError):
    """The resource does not exist (HTTP 404) — distinct from a network failure."""


class AlpacaClient:
    """Small REST client so V1 has no hidden broker SDK dependency."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._headers = {
            "APCA-API-KEY-ID": settings.api_key,
            "APCA-API-SECRET-KEY": settings.secret_key,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        data_api: bool = False,
    ) -> Any:
        base_url = "https://data.alpaca.markets" if data_api else self._settings.api_base_url
        url = f"{base_url}{path}"
        if params:
            url = f"{url}?{urlencode(params, doseq=True)}"
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(url, data=payload, headers=self._headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            if error.code == 404:
                raise AlpacaNotFoundError(f"Alpaca HTTP 404: {message}") from error
            raise AlpacaError(f"Alpaca HTTP {error.code}: {message}") from error
        except URLError as error:
            raise AlpacaError(f"Could not reach Alpaca: {error.reason}") from error

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def clock(self) -> dict[str, Any]:
        return self._request("GET", "/v2/clock")

    def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/v2/calendar",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )

    def positions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/positions")

    def open_orders(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/orders", params={"status": "open", "direction": "asc"})

    def closed_orders(self, after: date, limit: int = 100) -> list[dict[str, Any]]:
        # Read-only order history used by end-of-day reconciliation.
        return self._request(
            "GET",
            "/v2/orders",
            params={"status": "closed", "after": after.isoformat(), "limit": str(limit), "direction": "desc"},
        )

    def portfolio_history(self, period: str = "1D", timeframe: str = "5Min") -> dict[str, Any]:
        # Read-only broker-reported P/L series for the account (paper, simulated fills).
        return self._request(
            "GET",
            "/v2/account/portfolio/history",
            params={"period": period, "timeframe": timeframe},
        )

    def daily_bars(self, symbols: list[str], start: date, end: date) -> dict[str, list[dict[str, Any]]]:
        # IEX is intentional: it works with Alpaca paper/basic accounts. It is not full-market data.
        payload = self._request(
            "GET",
            "/v2/stocks/bars",
            params={
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": "all",
                "feed": "iex",
                "limit": "10000",
            },
            data_api=True,
        )
        return payload.get("bars", {})

    def order_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        """Look up one order by its client_order_id (restart reconciliation).

        Returns None when the broker has no order with this ID (HTTP 404),
        i.e. the POST never created an order. Network/HTTP failures still
        raise, so callers halt instead of guessing.
        """
        try:
            return self._request(
                "GET", "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
            )
        except AlpacaNotFoundError:
            return None

    def submit_market_order(
        self,
        *,
        symbol: str,
        side: str,
        client_order_id: str,
        qty: float | None = None,
        notional: float | None = None,
    ) -> dict[str, Any]:
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if (qty is None) == (notional is None):
            raise ValueError("Provide exactly one of qty or notional")

        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "extended_hours": False,
            "client_order_id": client_order_id,
        }
        if qty is not None:
            body["qty"] = f"{qty:.6f}".rstrip("0").rstrip(".")
        else:
            body["notional"] = f"{notional:.2f}"
        return self._request("POST", "/v2/orders", body=body)
