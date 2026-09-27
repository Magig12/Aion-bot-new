#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import time
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from capital_client import CapitalClient, CapitalAPIError, Quote

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

VERSION = "AION-CORE-V1-INDEX-PRECISION-R1H"
MARKETS: Dict[str, str] = {
    "US100": "US Tech 100",
    "US500": "US 500",
    "US30": "US Wall Street 30",
    "DE40": "Germany 40",
    "FR40": "France 40",
}


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def ema(values: Sequence[float], period: int) -> List[float]:
    xs = [fnum(x) for x in values]
    if not xs:
        return []
    a = 2.0 / (period + 1.0)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(a * x + (1.0 - a) * out[-1])
    return out


def atr(bars: Sequence[dict], period: int = 14) -> float:
    xs = list(bars)
    if len(xs) < period + 1:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(xs)):
        h, l = fnum(xs[i]["h"]), fnum(xs[i]["l"])
        pc = fnum(xs[i - 1]["c"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return statistics.fmean(trs[-period:]) if len(trs) >= period else 0.0


def parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def floor_bucket(dt: datetime, minutes: int) -> datetime:
    minute = (dt.minute // minutes) * minutes
    return dt.replace(minute=minute, second=0, microsecond=0)


def aggregate(bars: Sequence[dict], minutes: int, now: Optional[datetime] = None) -> List[dict]:
    """Aggregate only CLOSED M1 bars into CLOSED M5/M15 bars."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_minute = now.replace(second=0, microsecond=0)
    groups: Dict[datetime, List[dict]] = defaultdict(list)
    for b in bars:
        ts = parse_ts(str(b.get("ts") or ""))
        if ts is None or ts >= current_minute:
            continue
        groups[floor_bucket(ts, minutes)].append(b)
    out: List[dict] = []
    for start in sorted(groups):
        end = start.timestamp() + minutes * 60
        if end > current_minute.timestamp():
            continue
        g = sorted(groups[start], key=lambda x: str(x.get("ts") or ""))
        if not g:
            continue
        out.append({
            "ts": start.isoformat(),
            "o": fnum(g[0]["o"]),
            "h": max(fnum(x["h"]) for x in g),
            "l": min(fnum(x["l"]) for x in g),
            "c": fnum(g[-1]["c"]),
            "v": sum(fnum(x.get("v")) for x in g),
            "n": len(g),
        })
    return out


def closed_m1(bars: Sequence[dict], now: Optional[datetime] = None) -> List[dict]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cur = now.replace(second=0, microsecond=0)
    out = []
    for b in bars:
        ts = parse_ts(str(b.get("ts") or ""))
        if ts is not None and ts < cur:
            out.append(b)
    return sorted(out, key=lambda x: str(x.get("ts") or ""))


def resolve_market(client: CapitalClient, symbol: str, query: str) -> Dict[str, Any]:
    # Known Capital.com index EPICs are tried first; search is only fallback.
    try:
        d = client.market_details(symbol)
        if d:
            return {"epic": symbol, "details": d}
    except Exception:
        pass
    candidates: List[Dict[str, Any]] = []
    for term in (query, symbol):
        try:
            candidates.extend(client.search_markets(term))
        except Exception:
            pass
    if not candidates:
        raise CapitalAPIError(f"No se encontró {symbol}")
    def score(m: Dict[str, Any]) -> Tuple[int, int, int]:
        epic = str(m.get("epic") or "").upper()
        name = str(m.get("instrumentName") or m.get("name") or "").upper()
        typ = str(m.get("instrumentType") or "").upper()
        exact = 3 if epic == symbol else 2 if symbol in epic else 1 if all(w in name for w in query.upper().split()[:2]) else 0
        idx = 1 if "IND" in typ else 0
        openish = 1 if str(m.get("marketStatus") or "").upper() in ("TRADEABLE", "OPEN", "EDITS_ONLY") else 0
        return exact, idx, openish
    best = max(candidates, key=score)
    if score(best)[0] <= 0:
        raise CapitalAPIError(f"Resultados ambiguos para {symbol}")
    epic = str(best.get("epic") or "")
    return {"epic": epic, "details": client.market_details(epic)}


@dataclass
class Signal:
    symbol: str
    epic: str
    direction: str
    signal_ts: str
    entry: float
    stop: float
    target: float
    risk: float
    target_r: float
    spread_atr: float
    atr1: float
    trend_gap_atr15: float
    m5_distance_atr: float
    reason: str


@dataclass
class Tracked:
    deal_id: str
    symbol: str
    epic: str
    direction: str
    entry: float
    stop: float
    target: float
    risk: float
    target_r: float
    opened_ts: float
    signal_ts: str
    spread_atr: float
    max_r: float = 0.0
    min_r: float = 0.0
    last_r: float = 0.0


def evaluate(symbol: str, epic: str, q: Quote, raw_m1: Sequence[dict], now: Optional[datetime] = None) -> Tuple[Optional[Signal], str, Dict[str, Any]]:
    """One strategy only: M15 trend -> M5 pullback/resume -> M1 trigger."""
    now = now or datetime.now(timezone.utc)
    b1 = closed_m1(raw_m1, now)
    b5 = aggregate(b1, 5, now)
    b15 = aggregate(b1, 15, now)
    diag: Dict[str, Any] = {"m1": len(b1), "m5": len(b5), "m15": len(b15)}
    if len(b1) < 60 or len(b5) < 55 or len(b15) < 55:
        return None, "WARMUP", diag

    a1 = atr(b1, 14); a5 = atr(b5, 14); a15 = atr(b15, 14)
    if min(a1, a5, a15) <= 0:
        return None, "NO_ATR", diag
    spread = max(0.0, q.ask - q.bid)
    spread_atr = spread / max(a1, 1e-12)
    diag.update({"atr1": a1, "atr5": a5, "atr15": a15, "spread_atr": spread_atr})
    spread_max = fnum(os.getenv("AION_CORE_SPREAD_ATR_MAX"), 0.60)
    if spread_atr > spread_max:
        return None, "SPREAD", diag

    c15 = [fnum(x["c"]) for x in b15]
    e20_15 = ema(c15, 20); e50_15 = ema(c15, 50)
    slope15 = e20_15[-1] - e20_15[-4]
    trend_long = e20_15[-1] > e50_15[-1] and slope15 > 0 and c15[-1] > e20_15[-1]
    trend_short = e20_15[-1] < e50_15[-1] and slope15 < 0 and c15[-1] < e20_15[-1]
    trend_gap = abs(e20_15[-1] - e50_15[-1]) / max(a15, 1e-12)
    diag.update({"trend_long": trend_long, "trend_short": trend_short, "trend_gap_atr15": trend_gap})
    if not (trend_long or trend_short):
        return None, "M15_NO_TREND", diag

    c5 = [fnum(x["c"]) for x in b5]
    e20_5 = ema(c5, 20)
    recent = b5[-4:]
    recent_e = e20_5[-4:]
    touched = any(fnum(x["l"]) - .18 * a5 <= e <= fnum(x["h"]) + .18 * a5 for x, e in zip(recent, recent_e))
    long_resume = c5[-1] > e20_5[-1] and c5[-1] > c5[-2]
    short_resume = c5[-1] < e20_5[-1] and c5[-1] < c5[-2]
    m5_dist = abs(c5[-1] - e20_5[-1]) / max(a5, 1e-12)
    diag.update({"m5_touched": touched, "long_resume": long_resume, "short_resume": short_resume, "m5_distance_atr": m5_dist})

    direction: Optional[str] = None
    if trend_long and touched and long_resume:
        direction = "BUY"
    elif trend_short and touched and short_resume:
        direction = "SELL"
    else:
        return None, "M5_NO_PULLBACK_RESUME", diag

    cur, prev = b1[-1], b1[-2]
    if direction == "BUY":
        trigger = fnum(cur["c"]) > fnum(prev["h"]) and fnum(cur["c"]) > fnum(cur["o"])
    else:
        trigger = fnum(cur["c"]) < fnum(prev["l"]) and fnum(cur["c"]) < fnum(cur["o"])
    diag["m1_trigger"] = trigger
    if not trigger:
        return None, "M1_NO_TRIGGER", diag

    entry = q.ask if direction == "BUY" else q.bid
    swing = min(fnum(x["l"]) for x in b1[-6:]) if direction == "BUY" else max(fnum(x["h"]) for x in b1[-6:])
    structural = (entry - swing + .10 * a1) if direction == "BUY" else (swing - entry + .10 * a1)
    risk = max(.90 * a1, 3.0 * spread, structural)
    if risk > 2.20 * a1:
        diag["risk_atr"] = risk / a1
        return None, "RISK_TOO_WIDE", diag
    risk = max(risk, .90 * a1)
    rr = clamp(fnum(os.getenv("AION_CORE_RR"), 1.50), 1.0, 2.5)
    stop = entry - risk if direction == "BUY" else entry + risk
    target = entry + rr * risk if direction == "BUY" else entry - rr * risk
    signal_ts = str(cur.get("ts") or "")
    reason = "M15 trend + M5 pullback/resume + M1 break"
    return Signal(symbol, epic, direction, signal_ts, entry, stop, target, risk, rr, spread_atr, a1, trend_gap, m5_dist, reason), "SIGNAL", diag


class CoreBot:
    def __init__(self):
        self.client = CapitalClient.from_env()
        self.scan_sec = max(10, int(fnum(os.getenv("AION_CORE_SCAN_SEC"), 20)))
        self.max_hold_min = max(5, int(fnum(os.getenv("AION_CORE_MAX_HOLD_MIN"), 45)))
        self.markets: Dict[str, Dict[str, Any]] = {}
        self.tracked: Dict[str, Tracked] = {}
        self.session_deals: set[str] = set()
        self.last_signal_bar: Dict[str, str] = {}
        self.decisions: List[Dict[str, Any]] = []
        self.closed: List[Dict[str, Any]] = []
        self.errors: List[str] = []
        self.started_iso = ""
        self.cycles = 0
        self.decision_path = HERE / "AION_CORE_DECISIONS.csv"
        self.trade_path = HERE / "AION_CORE_TRADES.csv"

    def validate(self):
        if not self.client.configured():
            raise RuntimeError("Faltan credenciales Capital.com en .env")
        if self.client.environment != "demo":
            raise RuntimeError("AION CORE R1H sólo permite CAPITAL_ENV=demo")

    def discover(self):
        for symbol, query in MARKETS.items():
            try:
                obj = resolve_market(self.client, symbol, query)
                self.markets[symbol] = obj
                print(f"{symbol} -> {obj['epic']}")
            except Exception as e:
                print(f"[SKIP] {symbol}: {type(e).__name__}: {e}")
        if not self.markets:
            raise RuntimeError("No se pudo resolver ningún índice CORE")

    @staticmethod
    def _deal_id(result: Dict[str, Any]) -> str:
        return str(result.get("dealId") or result.get("dealIdOrigin") or result.get("dealReference") or "")

    @staticmethod
    def _append_csv(path: Path, row: Dict[str, Any]):
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()), extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow(row)

    def _record_decision(self, symbol: str, status: str, diag: Dict[str, Any], sig: Optional[Signal] = None):
        row = {
            "utc": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "status": status,
            "signal_ts": sig.signal_ts if sig else "", "direction": sig.direction if sig else "",
            "spread_atr": round(fnum(diag.get("spread_atr")), 4),
            "trend_gap_atr15": round(fnum(diag.get("trend_gap_atr15")), 4),
            "m5_distance_atr": round(fnum(diag.get("m5_distance_atr")), 4),
            "m1": int(fnum(diag.get("m1"))), "m5": int(fnum(diag.get("m5"))), "m15": int(fnum(diag.get("m15"))),
        }
        self.decisions.append(row); self._append_csv(self.decision_path, row)

    def _current_position_ids(self) -> set[str]:
        out = set()
        try:
            for item in self.client.positions():
                p = item.get("position") or item
                did = str(p.get("dealId") or item.get("dealId") or "")
                if did:
                    out.add(did)
        except Exception as e:
            self.errors.append(f"positions: {type(e).__name__}: {e}")
        return out

    def _has_symbol_open(self, symbol: str) -> bool:
        return any(t.symbol == symbol for t in self.tracked.values())

    def _close_record(self, t: Tracked, r: float, reason: str):
        r = clamp(r, -1.25, 3.0)
        outcome = "WIN" if r > .02 else "LOSS" if r < -.02 else "FLAT"
        row = {"closed_utc": datetime.now(timezone.utc).isoformat(), **asdict(t), "r": round(r, 4), "outcome": outcome, "close_reason": reason}
        self.closed.append(row); self._append_csv(self.trade_path, row)
        print(f"[CLOSE] {t.symbol} {t.direction} {r:+.2f}R {reason}")

    def manage(self):
        ids = self._current_position_ids()
        now_ts = time.time()
        for did, t in list(self.tracked.items()):
            try:
                q = self.client.quote(t.epic)
                px = q.bid if t.direction == "BUY" else q.ask
                r = ((px - t.entry) / t.risk) * (1 if t.direction == "BUY" else -1)
                t.last_r = r; t.max_r = max(t.max_r, r); t.min_r = min(t.min_r, r)
                if did not in ids:
                    # Broker-side TP/SL likely happened between scans. Last marked R plus
                    # proximity to fixed target/stop is enough for a clean research label.
                    if r >= t.target_r * .80:
                        rr, reason = t.target_r, "BROKER_TP"
                    elif r <= -.70:
                        rr, reason = -1.0, "BROKER_SL"
                    else:
                        rr, reason = r, "BROKER_CLOSED"
                    self._close_record(t, rr, reason); self.tracked.pop(did, None); continue
                if now_ts - t.opened_ts >= self.max_hold_min * 60:
                    self.client.close_position(did)
                    self._close_record(t, r, "TIME")
                    self.tracked.pop(did, None)
            except Exception as e:
                self.errors.append(f"manage {t.symbol}: {type(e).__name__}: {e}")

    def execute(self, sig: Signal):
        if self._has_symbol_open(sig.symbol):
            self._record_decision(sig.symbol, "DUPLICATE_POSITION", {}, sig); return
        if self.last_signal_bar.get(sig.symbol) == sig.signal_ts:
            return
        size = self.client.min_deal_size(sig.epic, default=1.0)
        result = self.client.create_position(sig.epic, sig.direction, size, sig.stop, sig.target)
        did = self._deal_id(result)
        actual_entry = fnum(result.get("level"), sig.entry)
        if not did:
            raise RuntimeError(f"Capital.com no devolvió dealId para {sig.symbol}")
        # Preserve the same price risk around the actual fill.
        stop = actual_entry - sig.risk if sig.direction == "BUY" else actual_entry + sig.risk
        target = actual_entry + sig.target_r * sig.risk if sig.direction == "BUY" else actual_entry - sig.target_r * sig.risk
        try:
            self.client.update_position(did, stop_level=stop, profit_level=target)
        except Exception as e:
            self.errors.append(f"reanchor {sig.symbol}: {type(e).__name__}: {e}")
        t = Tracked(did, sig.symbol, sig.epic, sig.direction, actual_entry, stop, target, sig.risk, sig.target_r, time.time(), sig.signal_ts, sig.spread_atr)
        self.tracked[did] = t; self.session_deals.add(did); self.last_signal_bar[sig.symbol] = sig.signal_ts
        self._record_decision(sig.symbol, "DEMO_ACCEPTED", {"spread_atr": sig.spread_atr, "trend_gap_atr15": sig.trend_gap_atr15, "m5_distance_atr": sig.m5_distance_atr}, sig)
        print(f"[DEMO] {sig.symbol} {sig.direction} size={size} RR={sig.target_r:.2f} spreadATR={sig.spread_atr:.2f} deal={did}")

    def cycle(self):
        self.manage()
        now = datetime.now(timezone.utc)
        for symbol, obj in self.markets.items():
            epic = str(obj["epic"])
            try:
                q = self.client.quote(epic)
                raw = self.client.prices(epic, resolution="MINUTE", max_bars=1000)
                sig, status, diag = evaluate(symbol, epic, q, raw, now)
                if sig:
                    if self._has_symbol_open(symbol):
                        self._record_decision(symbol, "DUPLICATE_POSITION", diag, sig)
                    elif self.last_signal_bar.get(symbol) != sig.signal_ts:
                        self.execute(sig)
                else:
                    # Log one diagnostic per symbol/cycle; report aggregates rejection reasons.
                    self._record_decision(symbol, status, diag)
            except Exception as e:
                msg = f"{symbol}: {type(e).__name__}: {e}"; self.errors.append(msg); print(f"[WARN] {msg}")

    def close_session(self):
        if not self.tracked:
            return
        print(f"[CORE] Cerrando {len(self.tracked)} posiciones de esta prueba...")
        for did, t in list(self.tracked.items()):
            try:
                q = self.client.quote(t.epic); px = q.bid if t.direction == "BUY" else q.ask
                r = ((px - t.entry) / t.risk) * (1 if t.direction == "BUY" else -1)
                self.client.close_position(did); self._close_record(t, r, "R1H_END")
                self.tracked.pop(did, None)
            except Exception as e:
                self.errors.append(f"close {t.symbol}: {type(e).__name__}: {e}")

    def report(self, minutes: float) -> Dict[str, Any]:
        reasons = Counter(x["status"] for x in self.decisions)
        rs = [fnum(x.get("r")) for x in self.closed]
        wins = sum(r > .02 for r in rs); losses = sum(r < -.02 for r in rs); flats = len(rs) - wins - losses
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for s in self.markets:
            tr = [x for x in self.closed if x["symbol"] == s]
            srs = [fnum(x["r"]) for x in tr]
            by_symbol[s] = {"closed": len(tr), "net_r": round(sum(srs), 4), "wins": sum(r > .02 for r in srs), "losses": sum(r < -.02 for r in srs)}
        report = {
            "version": VERSION, "strategy": "M15 trend -> M5 pullback/resume -> M1 break",
            "markets": list(self.markets), "started_utc": self.started_iso, "ended_utc": datetime.now(timezone.utc).isoformat(),
            "requested_minutes": minutes, "cycles": self.cycles, "closed": len(self.closed), "wins": wins, "losses": losses, "flats": flats,
            "win_rate_pct": round(100 * wins / len(rs), 2) if rs else None, "net_r": round(sum(rs), 4),
            "avg_r": round(statistics.fmean(rs), 4) if rs else None, "best_r": round(max(rs), 4) if rs else None, "worst_r": round(min(rs), 4) if rs else None,
            "decision_reasons": dict(reasons), "by_symbol": by_symbol, "errors": self.errors[-30:],
            "rules": {"rr": fnum(os.getenv("AION_CORE_RR"), 1.5), "max_hold_min": self.max_hold_min, "spread_atr_max": fnum(os.getenv("AION_CORE_SPREAD_ATR_MAX"), .60), "global_position_cap": None, "per_symbol_cap": 1, "time_filter": False},
        }
        jp = HERE / "AION_CORE_R1H_REPORT.json"; mp = HERE / "AION_CORE_R1H_REPORT.md"
        jp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        lines = [
            f"# {VERSION}", f"Estrategia: {report['strategy']}", f"Mercados: {', '.join(report['markets'])}",
            f"Inicio: {report['started_utc']}", f"Fin: {report['ended_utc']}",
            f"Ciclos: {report['cycles']} | Cerradas: {report['closed']} | W/L/F={wins}/{losses}/{flats}",
            f"Net R: {report['net_r']:+.3f} | Avg R: {report['avg_r']} | Win rate: {report['win_rate_pct']}",
            "", "## Resultado por índice",
        ]
        for s, v in by_symbol.items():
            lines.append(f"- {s}: closed={v['closed']} netR={v['net_r']:+.3f} W/L={v['wins']}/{v['losses']}")
        lines += ["", "## Por qué NO entró"]
        for k, v in reasons.most_common(): lines.append(f"- {k}: {v}")
        if self.errors:
            lines += ["", "## Errores recientes"] + [f"- {e}" for e in self.errors[-15:]]
        mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        bundle = HERE / f"AION_CORE_R1H_RESULTS_{stamp}.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
            for p in (jp, mp, self.decision_path, self.trade_path):
                if p.exists(): z.write(p, arcname=p.name)
        report["results_zip"] = bundle.name; jp.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("\n" + "=" * 70)
        print("AION CORE — RESUMEN FINAL")
        print("=" * 70)
        print(f"5 índices | 1 estrategia | ciclos={self.cycles}")
        print(f"Cerradas={len(rs)} W/L/F={wins}/{losses}/{flats} | NET R={sum(rs):+.3f} | WR={report['win_rate_pct']}")
        print("Rechazos principales:", dict(reasons.most_common(5)))
        print(f"ENVÍAME: {bundle.name}")
        print("=" * 70)
        return report

    def run(self, minutes: float):
        self.validate(); self.client.login(); self.discover(); self.started_iso = datetime.now(timezone.utc).isoformat()
        deadline = time.time() + max(1.0, minutes * 60)
        print(f"[{VERSION}] DEMO {minutes:.1f} min | mercados={len(self.markets)} | sin filtro horario | sin cap global | 1 pos/símbolo")
        try:
            while time.time() < deadline:
                t0 = time.time(); self.cycles += 1
                self.cycle()
                remain = deadline - time.time()
                if remain <= 0: break
                time.sleep(min(remain, max(1.0, self.scan_sec - (time.time() - t0))))
        finally:
            self.close_session(); self.report(minutes)


def self_check() -> int:
    # Synthetic sanity checks. No broker calls.
    vals = [1,2,3,4,5]
    assert len(ema(vals, 3)) == 5
    bars = [{"ts": f"2026-01-01T00:{i:02d}:00+00:00", "o": 100+i, "h": 101+i, "l": 99+i, "c": 100.5+i, "v": 1} for i in range(60)]
    assert atr(bars, 14) > 0
    ag = aggregate(bars, 5, datetime(2026,1,1,1,1,tzinfo=timezone.utc))
    assert len(ag) >= 10
    assert set(MARKETS) == {"US100","US500","US30","DE40","FR40"}
    print("SELF-CHECK OK — AION CORE simple/causal")
    return 0


def main():
    ap = argparse.ArgumentParser(description=VERSION)
    ap.add_argument("--minutes", type=float, default=60.0)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()
    if args.self_check:
        raise SystemExit(self_check())
    CoreBot().run(args.minutes)

if __name__ == "__main__":
    main()
