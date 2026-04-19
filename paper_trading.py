# paper_trading.py — v4.0.0
# Changes:
#   - Fully separated from real Zerodha trade flow
#   - FastAPI router with /paper/place-legs, /paper/exit-legs,
#     /paper/status, /paper/positions, /paper/orders, /paper/summary, /paper/reset
#   - Config-aware: reads greeks_config.json for pre/mon ranges (same file used by real trade)
#   - Thread-safe balance + position tracking

import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel
from typing import List, Optional

# ─────────────────────────────────────────────────────────────
#  In-memory state
# ─────────────────────────────────────────────────────────────
_lock            = threading.Lock()
_starting_bal    = 1_000_000.0   # ₹10 lakh
_virtual_balance = 1_000_000.0
_paper_orders:    list = []
_paper_positions: dict = {}

# ─────────────────────────────────────────────────────────────
#  Config loader  (shared with real trade — greeks_config.json)
# ─────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent / "greeks_config.json"

def load_config() -> dict:
    """Load greeks_config.json — returns {} if missing/corrupt."""
    try:
        with open(_CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def get_pre_ranges() -> dict:
    """Pre-trade check ranges from config (same as real trade uses)."""
    cfg = load_config()
    pre = cfg.get("pre", {})
    return {
        "delta_min":  pre.get("delta_min",  -0.07),
        "delta_max":  pre.get("delta_max",   0.07),
        "theta_min":  pre.get("theta_min",   5),
        "theta_max":  pre.get("theta_max",   500),
        "vega_min":   pre.get("vega_min",   -7),
        "vega_max":   pre.get("vega_max",    7),
        "gamma_min":  pre.get("gamma_min",  -0.004),
        "gamma_max":  pre.get("gamma_max",   0.004),
    }

def get_mon_ranges() -> dict:
    """Live-monitoring ranges from config (same as real trade uses)."""
    cfg = load_config()
    mon = cfg.get("mon", {})
    return {
        "delta_min":  mon.get("delta_min",  -0.10),
        "delta_max":  mon.get("delta_max",   0.10),
        "theta_min":  mon.get("theta_min",   3),
        "theta_max":  mon.get("theta_max",   5000),
        "vega_min":   mon.get("vega_min",   -10),
        "vega_max":   mon.get("vega_max",    10),
        "gamma_min":  mon.get("gamma_min",  -0.005),
        "gamma_max":  mon.get("gamma_max",   0.005),
    }

# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────
def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _order_id():
    import random, string
    return "PAPER-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

def _detect_lot_size(symbol: str) -> int:
    s = symbol.upper()
    if s.startswith("BANKNIFTY"):   return 30
    if s.startswith("FINNIFTY"):    return 60
    if s.startswith("MIDCPNIFTY"):  return 120
    if s.startswith("SENSEX"):      return 20
    if s.startswith("NIFTY"):       return 65
    return 1

# ─────────────────────────────────────────────────────────────
#  Core paper trading functions
# ─────────────────────────────────────────────────────────────
def place_paper_legs(legs: list, product: str = "NRML") -> dict:
    """
    Simulate placing option legs at current LTP.
    BUY  → debit premium from virtual balance
    SELL → credit premium to virtual balance (margin blocked conceptually)
    Never touches Zerodha / Kite API.
    """
    global _virtual_balance
    if not legs:
        return {"status": "error", "error": "No legs provided"}

    results = []
    with _lock:
        for leg in legs:
            symbol   = str(leg.get("symbol", "")).strip().upper()
            txn      = str(leg.get("transaction_type", "SELL")).strip().upper()
            quantity = int(leg.get("quantity", 0))
            ltp      = float(leg.get("ltp", 0) or 0)
            strike   = float(leg.get("strike", 0) or 0)
            expiry   = str(leg.get("expiry", "") or "")

            if not symbol:
                results.append({"symbol": symbol, "status": "error",
                                 "message": "Empty symbol", "order_id": None}); continue
            if txn not in ("BUY", "SELL"):
                results.append({"symbol": symbol, "status": "error",
                                 "message": f"Bad transaction_type '{txn}'", "order_id": None}); continue
            if quantity <= 0:
                results.append({"symbol": symbol, "status": "error",
                                 "message": f"Bad quantity {quantity}", "order_id": None}); continue

            premium = round(ltp * quantity, 2)
            oid     = _order_id()

            # ── Balance update ──────────────────────────────────────────
            if txn == "BUY":
                _virtual_balance -= premium   # Pay premium out
            else:
                _virtual_balance += premium   # Collect premium (SELL)

            order = {
                "order_id": oid, "symbol": symbol, "transaction_type": txn,
                "quantity": quantity, "ltp": ltp, "strike": strike, "expiry": expiry,
                "premium": premium, "product": product,
                "timestamp": _now_str(), "status": "COMPLETE", "paper": True,
            }
            _paper_orders.append(order)

            # ── Position update ─────────────────────────────────────────
            key = symbol
            if key in _paper_positions:
                pos = _paper_positions[key]
                if pos["transaction_type"] == txn:
                    # Adding to existing side — weighted average
                    old_qty = pos["quantity"]
                    new_qty = old_qty + quantity
                    pos["avg_price"] = round(
                        (pos["avg_price"] * old_qty + ltp * quantity) / new_qty, 2)
                    pos["quantity"] = new_qty
                else:
                    # Reducing / closing
                    pos["quantity"] -= quantity
                    if pos["quantity"] <= 0:
                        del _paper_positions[key]
            else:
                _paper_positions[key] = {
                    "symbol": symbol, "transaction_type": txn,
                    "quantity": quantity, "avg_price": ltp, "entry_price": ltp,
                    "strike": strike, "expiry": expiry, "product": product,
                    "open_at": _now_str(), "paper": True,
                }

            results.append({
                "symbol": symbol, "transaction_type": txn,
                "quantity": quantity, "order_id": oid, "order_status": "COMPLETE",
                "status": "success",
                "message": f"Paper {oid} filled @ ₹{ltp:.2f} | Premium ₹{premium:.2f}",
                "ltp": ltp, "strike": strike, "expiry": expiry,
                "virtual_balance": round(_virtual_balance, 2),
                "paper": True,
            })

    placed  = sum(1 for r in results if r["status"] == "success")
    failed  = sum(1 for r in results if r["status"] != "success")
    overall = "success" if failed == 0 else ("error" if placed == 0 else "partial")
    return {
        "status": overall, "results": results,
        "placed": placed, "failed": failed,
        "virtual_balance": round(_virtual_balance, 2),
    }


def exit_paper_legs(legs: list, product: str = "NRML") -> dict:
    """Close paper positions by reversing transaction type. Never touches Zerodha."""
    if not legs:
        return {"status": "error", "error": "No legs provided"}
    exit_list = []
    for leg in legs:
        orig = (leg.get("transaction_type") or
                leg.get("original_transaction_type") or "SELL").strip().upper()
        exit_list.append({
            "symbol":           str(leg.get("symbol", "")).strip().upper(),
            "transaction_type": "BUY" if orig == "SELL" else "SELL",
            "quantity":         int(leg.get("quantity", 0)),
            "ltp":              float(leg.get("ltp", 0) or 0),
            "strike":           float(leg.get("strike", 0) or 0),
            "expiry":           str(leg.get("expiry", "") or ""),
        })
    return place_paper_legs(exit_list, product)


def get_paper_positions() -> dict:
    with _lock:
        positions = [p for p in _paper_positions.values() if p.get("quantity", 0) > 0]
    return {"status": "success", "positions": positions, "count": len(positions)}


def get_paper_orders() -> dict:
    with _lock:
        orders = list(reversed(_paper_orders))
    return {"status": "success", "orders": orders, "count": len(orders)}


def get_paper_summary() -> dict:
    with _lock:
        positions = [p for p in _paper_positions.values() if p.get("quantity", 0) > 0]
        orders    = list(_paper_orders)
        balance   = _virtual_balance

    realised_pnl = round(balance - _starting_bal, 2)

    return {
        "status":           "success",
        "open_positions":   len(positions),
        "total_orders":     len(orders),
        "virtual_balance":  round(balance, 2),
        "starting_balance": round(_starting_bal, 2),
        "realised_pnl":     realised_pnl,
        "positions":        positions,
        # Expose current config ranges so frontend can verify alignment
        "config_pre":       get_pre_ranges(),
        "config_mon":       get_mon_ranges(),
    }


def reset_paper_trading() -> dict:
    global _paper_orders, _paper_positions, _virtual_balance
    with _lock:
        _paper_orders    = []
        _paper_positions = {}
        _virtual_balance = _starting_bal
    return {"status": "success",
            "message": "Paper trading reset. Balance restored to ₹10,00,000"}


# ─────────────────────────────────────────────────────────────
#  FastAPI Router  — mount with:  app.include_router(paper_router)
#  in server.py
# ─────────────────────────────────────────────────────────────
paper_router = APIRouter(prefix="/paper", tags=["paper"])


class LegIn(BaseModel):
    symbol:           str
    transaction_type: str
    quantity:         int
    ltp:              float = 0.0
    strike:           float = 0.0
    expiry:           str   = ""
    original_transaction_type: Optional[str] = None


class LegsPayload(BaseModel):
    legs:    List[LegIn]
    product: str = "NRML"


@paper_router.get("/status")
def paper_status():
    """Health check — confirms paper trading module is active."""
    return {
        "paper_trading_enabled": True,
        "virtual_balance": round(_virtual_balance, 2),
        "open_positions":  len([p for p in _paper_positions.values()
                                if p.get("quantity", 0) > 0]),
    }


@paper_router.post("/place-legs")
def api_place_paper_legs(payload: LegsPayload):
    """
    Place paper legs — records in virtual balance only.
    Identical request shape to /place-legs so the frontend
    can switch endpoint without changing payload.
    """
    legs = [l.dict() for l in payload.legs]
    return place_paper_legs(legs, payload.product)


@paper_router.post("/exit-legs")
def api_exit_paper_legs(payload: LegsPayload):
    """
    Exit paper legs — reverses transaction type and updates virtual balance.
    Identical request shape to /exit-legs.
    """
    legs = [l.dict() for l in payload.legs]
    return exit_paper_legs(legs, payload.product)


@paper_router.get("/positions")
def api_paper_positions():
    return get_paper_positions()


@paper_router.get("/orders")
def api_paper_orders():
    return get_paper_orders()


@paper_router.get("/summary")
def api_paper_summary():
    """Returns balance, P&L, positions, AND current config ranges."""
    return get_paper_summary()


@paper_router.post("/reset")
def api_paper_reset():
    return reset_paper_trading()


@paper_router.get("/config")
def api_paper_config():
    """
    Return the current greeks_config.json ranges used by both
    paper and real trade — so frontend always gets the same config.
    """
    return {
        "status": "success",
        "pre":    get_pre_ranges(),
        "mon":    get_mon_ranges(),
        "raw":    load_config(),
    }