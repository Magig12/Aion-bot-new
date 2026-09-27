from __future__ import annotations
import os, time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
import requests


class CapitalAPIError(RuntimeError):
    pass


@dataclass
class Quote:
    epic: str
    bid: float
    ask: float
    market_status: str = "UNKNOWN"

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class CapitalClient:
    """Minimal Capital.com REST adapter designed for Android/Pydroid.

    Supports market discovery, historical bars, quotes, sentiment, positions,
    guarded order creation and position updates/closures. It intentionally avoids
    PC-only terminal dependencies.
    """

    LIVE_BASE = "https://api-capital.backend-capital.com"
    DEMO_BASE = "https://demo-api-capital.backend-capital.com"

    def __init__(self, api_key: str, identifier: str, password: str,
                 environment: str = "demo", timeout: float = 12.0):
        self.api_key = (api_key or "").strip()
        self.identifier = (identifier or "").strip()
        self.password = password or ""
        self.environment = environment.lower().strip()
        self.base = self.DEMO_BASE if self.environment != "live" else self.LIVE_BASE
        self.timeout = timeout
        self.session = requests.Session()
        self.cst = ""
        self.security_token = ""
        self.last_auth = 0.0
        self._last_request = 0.0

    @classmethod
    def from_env(cls) -> "CapitalClient":
        return cls(
            os.getenv("CAPITAL_API_KEY", ""),
            os.getenv("CAPITAL_IDENTIFIER", ""),
            os.getenv("CAPITAL_API_PASSWORD", ""),
            os.getenv("CAPITAL_ENV", "demo"),
        )

    def configured(self) -> bool:
        return bool(self.api_key and self.identifier and self.password)

    def _throttle(self, min_gap: float = 0.11) -> None:
        # Capital.com documents a 10 requests/sec/user ceiling. 0.11s keeps a
        # small safety margin and is still fast enough for our 20s scan cycle.
        gap = time.monotonic() - self._last_request
        if gap < min_gap:
            time.sleep(min_gap - gap)
        self._last_request = time.monotonic()

    def login(self) -> Dict[str, Any]:
        if not self.configured():
            raise CapitalAPIError("Faltan CAPITAL_API_KEY / CAPITAL_IDENTIFIER / CAPITAL_API_PASSWORD")
        self._throttle(1.05)  # /session is limited to 1 request/sec.
        headers = {"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"}
        body = {"identifier": self.identifier, "password": self.password, "encryptedPassword": False}
        r = self.session.post(self.base + "/api/v1/session", headers=headers, json=body, timeout=self.timeout)
        if not r.ok:
            raise CapitalAPIError(f"Login Capital.com HTTP {r.status_code}: {self._safe_error(r)}")
        self.cst = r.headers.get("CST", "")
        self.security_token = r.headers.get("X-SECURITY-TOKEN", "")
        if not self.cst or not self.security_token:
            raise CapitalAPIError("Capital.com no devolvió CST/X-SECURITY-TOKEN")
        self.last_auth = time.monotonic()
        return r.json() if r.content else {}

    @staticmethod
    def _safe_error(r: requests.Response) -> str:
        try:
            j = r.json()
            return str(j.get("errorCode") or j.get("error") or j.get("message") or "API error")[:300]
        except Exception:
            return (r.text or "API error")[:200]

    def _headers(self) -> Dict[str, str]:
        h = {"X-CAP-API-KEY": self.api_key, "Accept": "application/json"}
        if self.cst:
            h["CST"] = self.cst
        if self.security_token:
            h["X-SECURITY-TOKEN"] = self.security_token
        return h

    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 auth: bool = True, retry_auth: bool = True) -> Dict[str, Any]:
        if auth and (not self.cst or not self.security_token):
            self.login()
        self._throttle()
        headers = self._headers()
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        r = self.session.request(method, self.base + path, headers=headers,
                                 params=params, json=json_body, timeout=self.timeout)
        if r.status_code in (401, 403) and auth and retry_auth:
            self.cst = self.security_token = ""
            self.login()
            return self._request(method, path, params=params, json_body=json_body,
                                 auth=auth, retry_auth=False)
        if not r.ok:
            raise CapitalAPIError(f"{method} {path} HTTP {r.status_code}: {self._safe_error(r)}")
        return r.json() if r.content else {}

    def ping(self) -> bool:
        try:
            self._request("GET", "/api/v1/ping")
            return True
        except CapitalAPIError:
            return False

    def search_markets(self, term: str) -> List[Dict[str, Any]]:
        data = self._request("GET", "/api/v1/markets", params={"searchTerm": term})
        return list(data.get("markets") or [])

    @staticmethod
    def _norm(s: str) -> str:
        return "".join(ch for ch in str(s or "").upper() if ch.isalnum())

    def resolve_fx_epic(self, pair: str) -> Dict[str, Any]:
        pair = self._norm(pair)
        if len(pair) != 6:
            raise ValueError(f"Par inválido: {pair}")
        base, quote = pair[:3], pair[3:]
        terms = [f"{base}/{quote}", pair, f"{base}{quote}"]
        candidates: List[Dict[str, Any]] = []
        for term in terms:
            try:
                candidates.extend(self.search_markets(term))
            except CapitalAPIError:
                continue
        if not candidates:
            raise CapitalAPIError(f"No se encontró mercado para {base}/{quote}")

        def rank(m: Dict[str, Any]):
            epic = self._norm(m.get("epic"))
            name = self._norm(m.get("instrumentName") or m.get("name"))
            typ = str(m.get("instrumentType") or "").upper()
            status = str(m.get("marketStatus") or "").upper()
            exact = 1 if pair in epic or (base in name and quote in name) else 0
            fx = 1 if any(x in typ for x in ("CURRENC", "FOREX", "FX")) else 0
            openish = 1 if status in ("TRADEABLE", "OPEN", "EDITS_ONLY") else 0
            return (exact, fx, openish)

        best = max(candidates, key=rank)
        if rank(best)[0] == 0:
            raise CapitalAPIError(f"Resultados ambiguos para {base}/{quote}; revisa el epic manualmente")
        return best

    def market_details(self, epic: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/markets/{epic}")

    def quote(self, epic: str) -> Quote:
        data = self.market_details(epic)
        snap = data.get("snapshot") or data
        bid = float(snap.get("bid") or 0)
        ask = float(snap.get("offer") or snap.get("ask") or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            raise CapitalAPIError(f"Cotización inválida para {epic}")
        status = str(snap.get("marketStatus") or data.get("marketStatus") or "UNKNOWN")
        return Quote(epic=epic, bid=bid, ask=ask, market_status=status)

    @staticmethod
    def _mid(px: Any) -> float:
        if isinstance(px, dict):
            b = float(px.get("bid") or 0)
            a = float(px.get("ask") or 0)
            if b and a:
                return (b + a) / 2.0
            return b or a
        return float(px or 0)

    @classmethod
    def parse_prices(cls, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for p in response.get("prices") or []:
            try:
                bar = {
                    "ts": p.get("snapshotTimeUTC") or p.get("snapshotTime") or "",
                    "o": cls._mid(p.get("openPrice")),
                    "h": cls._mid(p.get("highPrice")),
                    "l": cls._mid(p.get("lowPrice")),
                    "c": cls._mid(p.get("closePrice")),
                    "v": float(p.get("lastTradedVolume") or 0),
                    "bid_c": float((p.get("closePrice") or {}).get("bid") or 0),
                    "ask_c": float((p.get("closePrice") or {}).get("ask") or 0),
                }
                if min(bar["o"], bar["h"], bar["l"], bar["c"]) > 0:
                    out.append(bar)
            except (TypeError, ValueError):
                continue
        return out

    def prices(self, epic: str, resolution: str = "MINUTE", max_bars: int = 250,
               from_iso: Optional[str] = None, to_iso: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"resolution": resolution, "max": max(10, min(1000, int(max_bars)))}
        if from_iso:
            params["from"] = from_iso
        if to_iso:
            params["to"] = to_iso
        data = self._request("GET", f"/api/v1/prices/{epic}", params=params)
        return self.parse_prices(data)

    def client_sentiments(self, market_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        ids = [str(x) for x in market_ids if str(x)]
        if not ids:
            return {}
        try:
            data = self._request("GET", "/api/v1/clientsentiment", params={"marketIds": ",".join(ids)})
        except CapitalAPIError:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for item in data.get("clientSentiments") or []:
            key = str(item.get("marketId") or "")
            if key:
                out[key] = item
        return out

    def positions(self) -> List[Dict[str, Any]]:
        return list(self._request("GET", "/api/v1/positions").get("positions") or [])

    def position(self, deal_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/positions/{deal_id}")

    def create_position(self, epic: str, direction: str, size: float,
                        stop_level: Optional[float] = None,
                        profit_level: Optional[float] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "epic": epic,
            "direction": direction.upper(),
            "size": float(size),
            "guaranteedStop": False,
        }
        if stop_level is not None:
            body["stopLevel"] = float(stop_level)
        if profit_level is not None:
            body["profitLevel"] = float(profit_level)
        created = self._request("POST", "/api/v1/positions", json_body=body)
        ref = created.get("dealReference")
        if not ref:
            raise CapitalAPIError("La API no devolvió dealReference")
        time.sleep(0.25)
        confirmation = self._request("GET", f"/api/v1/confirms/{ref}")
        if str(confirmation.get("dealStatus") or "").upper() not in ("ACCEPTED", ""):
            raise CapitalAPIError(f"Orden no aceptada: {confirmation.get('reason') or confirmation.get('dealStatus')}")
        return confirmation

    def update_position(self, deal_id: str, *, stop_level: Optional[float] = None,
                        profit_level: Optional[float] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if stop_level is not None:
            body["stopLevel"] = float(stop_level)
        if profit_level is not None:
            body["profitLevel"] = float(profit_level)
        if not body:
            return {}
        result = self._request("PUT", f"/api/v1/positions/{deal_id}", json_body=body)
        ref = result.get("dealReference")
        if ref:
            time.sleep(0.22)
            confirmation = self._request("GET", f"/api/v1/confirms/{ref}")
            status = str(confirmation.get("dealStatus") or "").upper()
            if status not in ("ACCEPTED", ""):
                raise CapitalAPIError(f"Modificación no aceptada: {confirmation.get('reason') or status}")
            return confirmation
        return result

    def close_position(self, deal_id: str) -> Dict[str, Any]:
        result = self._request("DELETE", f"/api/v1/positions/{deal_id}")
        ref = result.get("dealReference")
        if ref:
            time.sleep(0.25)
            return self._request("GET", f"/api/v1/confirms/{ref}")
        return result

    def activity(self, deal_id: str, last_period: int = 86400) -> List[Dict[str, Any]]:
        params = {"lastPeriod": max(60, min(86400, int(last_period))), "detailed": "true", "dealId": deal_id}
        try:
            data = self._request("GET", "/api/v1/history/activity", params=params)
            return list(data.get("activities") or [])
        except CapitalAPIError:
            return []

    def min_deal_size(self, epic: str, default: float = 1.0) -> float:
        data = self.market_details(epic)
        rules = data.get("dealingRules") or {}
        obj = rules.get("minDealSize") or {}
        try:
            return float(obj.get("value") or default)
        except Exception:
            return float(default)
