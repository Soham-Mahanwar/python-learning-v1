# server.py — v3.3.0
# Changes from v3.2.0:
#   - /greeks default num_strikes raised from 10 → 20
#     (ensures delta=0.20 OTM strikes are always in the initial chain)
#   - PlaceLegsRequest: accepts "CNC" as alias for "NRML"
#   - Added /debug/products endpoint

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from typing import Optional, List, Any

from core_trading import (
    place_trade, get_quote, get_positions, get_orders,
    get_holdings, get_margins, get_all_indices,
    get_greeks, get_support_resistance, get_option_chain,
    place_option_legs, exit_option_legs, check_order_margins,
    send_whatsapp_alert,
    INDEX_CONFIG, SLUG_TO_INDEX, _get_instruments,
    market_open,
)

from paper_trading import (
    place_paper_legs, exit_paper_legs,
    get_paper_positions, get_paper_orders, get_paper_summary,
    reset_paper_trading
)

from paper_margin import calc_paper_margin

# ── PAPER TRADING MODE ────────────────────────────────────────────────────────
PAPER_TRADING_MODE = False   # Set True for paper, False for live (REAL MONEY)

# ── Startup / shutdown ────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    loop = asyncio.get_event_loop()
    print("[startup] Pre-warming instrument cache...")
    try:
        await loop.run_in_executor(None, _get_instruments, "NFO")
        await loop.run_in_executor(None, _get_instruments, "BFO")
        print("[startup] Cache ready ✓")
    except Exception as e:
        print(f"[startup] Cache warm-up failed: {e}")
    yield
    print("[shutdown] Goodbye.")

app = FastAPI(title="Zerodha Options Terminal", version="3.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Request models ────────────────────────────────────────────────────────────
class LegModel(BaseModel):
    symbol:           str
    transaction_type: str
    quantity:         int
    strike:           Optional[float] = None
    ltp:              Optional[float] = None
    expiry:           Optional[str]   = None

    @field_validator("transaction_type")
    @classmethod
    def validate_txn(cls, v):
        if v.upper() not in ("BUY", "SELL"):
            raise ValueError("transaction_type must be BUY or SELL")
        return v.upper()

    @field_validator("quantity")
    @classmethod
    def validate_qty(cls, v):
        if v <= 0:
            raise ValueError("quantity must be > 0")
        return v

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, v):
        v = v.strip().upper()
        if not v:
            raise ValueError("symbol cannot be empty")
        return v


class PlaceLegsRequest(BaseModel):
    legs:    List[LegModel]
    product: Optional[str] = "NRML"

    @field_validator("product")
    @classmethod
    def validate_product(cls, v):
        # Accept NRML, CNC (alias for NRML/carry-forward), MIS
        v = v.upper()
        if v not in ("NRML", "CNC", "MIS"):
            raise ValueError("product must be NRML, CNC, or MIS")
        return "NRML" if v == "CNC" else v   # normalise CNC → NRML

    @field_validator("legs")
    @classmethod
    def validate_legs_count(cls, v):
        if len(v) == 0: raise ValueError("At least 1 leg required")
        if len(v) > 4:  raise ValueError("Maximum 4 legs allowed")
        return v


class ExitLegsRequest(BaseModel):
    legs:    List[dict]
    product: Optional[str] = "NRML"


class MarginCheckRequest(BaseModel):
    legs:    List[LegModel]
    product: Optional[str] = "NRML"


class TradeCommand(BaseModel):
    command: str


class QuoteRequest(BaseModel):
    symbol: str


class WhatsAppRequest(BaseModel):
    phone:        str
    message:      str
    level:        Optional[str] = "info"
    twilio_sid:   str
    twilio_token: str
    from_number:  Optional[str] = "whatsapp:+14155238886"


class ConfigSaveRequest(BaseModel):
    config: dict


class PayoffRequest(BaseModel):
    legs: List[dict]
    spot_range: Optional[List[float]] = None
    num_points: Optional[int] = 100


# ── Health & root ─────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "status":        "online",
        "version":       "3.3.0",
        "market_open":   market_open(),
        "paper_trading": PAPER_TRADING_MODE,
        "mode":          "PAPER" if PAPER_TRADING_MODE else "LIVE",
    }

@app.get("/health")
def health():
    return {
        "status":      "healthy",
        "market_open": market_open(),
        "version":     "3.3.0",
        "paper_mode":  PAPER_TRADING_MODE,
    }

# ── Auth: Generate Access Token ───────────────────────────────────────────────
class GenerateTokenReq(BaseModel):
    api_key:       str
    api_secret:    str
    request_token: str

@app.post("/generate-token")
def generate_token(req: GenerateTokenReq):
    """
    Exchange request_token for access_token.
    Flow: api_key + api_secret + request_token → access_token
    """
    from kiteconnect import KiteConnect as _KC
    try:
        k = _KC(api_key=req.api_key.strip())
        data = k.generate_session(
            req.request_token.strip(),
            api_secret=req.api_secret.strip()
        )
        access_token = data.get("access_token", "")
        if not access_token:
            return {"status": "error", "error": "No access_token in Zerodha response — request_token may be expired or already used"}
        return {
            "status":       "success",
            "access_token": access_token,
            "user_name":    data.get("user_name") or data.get("user_shortname") or "",
            "user_id":      data.get("user_id", ""),
        }
    except Exception as e:
        err = str(e)
        if "InvalidInputException" in err or "TokenException" in err:
            return {"status": "error", "error": f"request_token is invalid or already used. Please login again. ({err})"}
        if "NetworkException" in err:
            return {"status": "error", "error": f"Cannot connect to Zerodha. Check internet. ({err})"}
        return {"status": "error", "error": err}

# ── Auth: Hot-reload access token without restart ─────────────────────────────
class UpdateTokenReq(BaseModel):
    api_key:      str
    access_token: str

@app.put("/update-token")
def update_token(req: UpdateTokenReq):
    """Hot-reload token in running server — no restart needed."""
    import core_trading as ct
    try:
        # Recreate kite instance with new credentials
        from kiteconnect import KiteConnect
        ct.kite = KiteConnect(api_key=req.api_key.strip())
        ct.kite.set_access_token(req.access_token.strip())
        
        # Update global variables
        ct.api_key = req.api_key.strip()
        ct.access_token = req.access_token.strip()
        
        return {"status": "success", "message": "Token updated in running server"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

# ── Paper: record paper trade legs ───────────────────────────────────────────
@app.post("/paper/place-legs")
def paper_place_legs_endpoint(req: PlaceLegsRequest):
    legs = [l.model_dump() for l in req.legs]
    return place_paper_legs(legs, req.product)

# ── Market data ───────────────────────────────────────────────────────────────
@app.get("/indices")
def indices():
    return get_all_indices()

@app.get("/greeks")
def greeks(
    index:       str           = Query("NIFTY"),
    expiry:      Optional[str] = Query(None),
    num_strikes: int           = Query(20),   # 20 steps each side of ATM
                                               # NIFTY step=50 → ±1000pts covers delta=0.20 strike
                                               # Frontend selectByDelta picks closest to ±0.20 from returned chain
):
    """
    Returns full option chain with Black-Scholes Greeks.
    num_strikes=20 default: ±20 steps from ATM
      NIFTY (step=50):    ATM ± 1000 pts → covers delta=0.20 at ~ATM+600
      BANKNIFTY (step=100): ATM ± 2000 pts → covers delta=0.20 at ~ATM+1200
    Garbage strikes (delta≈0, vega≈0) are filtered in core_trading.get_greeks().
    Frontend further filters with MEANINGFUL_DELTA=0.01, MEANINGFUL_VEGA=0.01.
    """
    idx = index.upper()
    if idx not in INDEX_CONFIG:
        return {"status": "error", "error": f"Invalid index '{idx}'. Valid: {list(INDEX_CONFIG.keys())}"}
    result = get_greeks(idx, expiry, num_strikes)
    return result

@app.get("/index/{slug}/greeks")
def greeks_by_slug(
    slug:        str,
    expiry:      Optional[str] = Query(None),
    num_strikes: int           = Query(20),   # v3.3.0: raised from 10 → 20
):
    idx = SLUG_TO_INDEX.get(slug.lower())
    if not idx:
        return {"status": "error", "error": f"Unknown index slug: {slug}"}
    return get_greeks(idx, expiry, num_strikes)

@app.post("/quote")
def quote(req: QuoteRequest):
    return get_quote(req.symbol)

@app.get("/positions")
def positions(): return get_positions()

@app.get("/orders")
def orders(): return get_orders()

@app.get("/holdings")
def holdings(): return get_holdings()

@app.get("/margins")
def margins(): return get_margins()

@app.get("/index/{index_name}/levels")
def levels(index_name: str):
    return get_support_resistance(index_name)

# ── Margin check ──────────────────────────────────────────────────────────────
@app.post("/check-margin")
def check_margin(req: MarginCheckRequest):
    legs = [l.model_dump() for l in req.legs]
    if PAPER_TRADING_MODE:
        return calc_paper_margin(legs)
    else:
        return check_order_margins(legs, req.product)

# ── Option order endpoints ────────────────────────────────────────────────────
@app.post("/place-legs")
def place_legs(req: PlaceLegsRequest):
    legs = [l.model_dump() for l in req.legs]
    if PAPER_TRADING_MODE:
        return place_paper_legs(legs, req.product)
    else:
        return place_option_legs(legs, req.product)

@app.post("/exit-legs")
def exit_legs_endpoint(req: ExitLegsRequest):
    if PAPER_TRADING_MODE:
        return exit_paper_legs(req.legs, req.product)
    else:
        return exit_option_legs(req.legs, req.product)

# ── Paper trading management ──────────────────────────────────────────────────
@app.get("/paper/status")
def paper_trading_status():
    return {
        "paper_trading_enabled": PAPER_TRADING_MODE,
        "mode":    "PAPER TRADING" if PAPER_TRADING_MODE else "LIVE TRADING",
        "warning": None if PAPER_TRADING_MODE else "⚠️ LIVE MODE — REAL MONEY AT RISK",
    }

@app.get("/paper/positions")
def get_paper_positions_endpoint():
    if not PAPER_TRADING_MODE:
        return {"status": "error", "error": "Paper trading is disabled."}
    return get_paper_positions()

@app.get("/paper/orders")
def get_paper_orders_endpoint():
    if not PAPER_TRADING_MODE:
        return {"status": "error", "error": "Paper trading is disabled."}
    return get_paper_orders()

@app.get("/paper/summary")
def get_paper_summary_endpoint():
    if not PAPER_TRADING_MODE:
        return {"status": "error", "error": "Paper trading is disabled."}
    return get_paper_summary()

@app.post("/paper/reset")
def reset_paper_trading_endpoint():
    if not PAPER_TRADING_MODE:
        return {"status": "error", "error": "Paper trading is disabled."}
    return reset_paper_trading()

# ── Config persistence ────────────────────────────────────────────────────────
import json, os

CONFIG_FILE = "greeks_config.json"

@app.get("/config")
def get_config():
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                return {"status": "success", "config": json.load(f)}
        return {"status": "not_found", "config": None}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/config")
def save_config(req: ConfigSaveRequest):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(req.config, f, indent=2)
        return {"status": "success", "message": "Config saved"}
    except Exception as e:
        return {"status": "error", "error": str(e)}

# ── Payoff Chart ─────────────────────────────────────────────────────────────
@app.post("/calculate-payoff")
def calculate_payoff(req: PayoffRequest):
    """
    Calculate payoff diagram for options strategy.
    Returns payoff data points for charting.
    """
    try:
        if not req.legs:
            return {"status": "error", "error": "No legs provided"}
        
        # Extract strikes and premiums from legs
        strikes = []
        premiums = []
        quantities = []
        types = []
        
        for leg in req.legs:
            strike = float(leg.get('strike', 0))
            premium = float(leg.get('ltp', 0) or leg.get('entryPrice', 0))
            quantity = int(leg.get('quantity', 0))
            leg_type = leg.get('type', '').upper()
            txn_type = leg.get('txnType', 'BUY').upper()
            
            strikes.append(strike)
            premiums.append(premium)
            quantities.append(quantity)
            types.append(leg_type)
        
        if not strikes:
            return {"status": "error", "error": "No valid strikes found"}
        
        # Determine price range
        min_strike = min(strikes)
        max_strike = max(strikes)
        
        if req.spot_range:
            price_min, price_max = req.spot_range
        else:
            # Default range: ±20% from min/max strike
            buffer = max(max_strike - min_strike, 100) * 0.2
            price_min = min_strike - buffer
            price_max = max_strike + buffer
        
        # Generate price points
        num_points = req.num_points or 100
        price_points = [
            price_min + (price_max - price_min) * i / (num_points - 1)
            for i in range(num_points)
        ]
        
        # Calculate payoff at each price point
        payoff_data = []
        for price in price_points:
            total_payoff = 0
            
            for i, leg in enumerate(req.legs):
                strike = strikes[i]
                premium = premiums[i]
                quantity = quantities[i]
                leg_type = types[i]
                txn_type = leg.get('txnType', 'BUY').upper()
                
                # Calculate individual leg payoff
                if leg_type == 'CE':
                    if price > strike:
                        leg_payoff = price - strike - premium
                    else:
                        leg_payoff = -premium
                elif leg_type == 'PE':
                    if price < strike:
                        leg_payoff = strike - price - premium
                    else:
                        leg_payoff = -premium
                else:
                    leg_payoff = 0
                
                # Apply transaction type and quantity
                if txn_type == 'SELL':
                    leg_payoff = -leg_payoff
                
                total_payoff += leg_payoff * quantity
            
            payoff_data.append({
                "price": round(price, 2),
                "payoff": round(total_payoff, 2)
            })
        
        # Find breakeven points
        breakeven_points = []
        for i in range(1, len(payoff_data)):
            if (payoff_data[i-1]["payoff"] <= 0 <= payoff_data[i]["payoff"]) or \
               (payoff_data[i-1]["payoff"] >= 0 >= payoff_data[i]["payoff"]):
                breakeven_points.append(payoff_data[i]["price"])
        
        # Calculate max profit/loss
        payoffs = [p["payoff"] for p in payoff_data]
        max_profit = max(payoffs) if payoffs else 0
        max_loss = min(payoffs) if payoffs else 0
        
        return {
            "status": "success",
            "data": payoff_data,
            "analysis": {
                "max_profit": round(max_profit, 2),
                "max_loss": round(max_loss, 2),
                "breakeven_points": [round(p, 2) for p in breakeven_points],
                "price_range": [round(price_min, 2), round(price_max, 2)]
            }
        }
        
    except Exception as e:
        return {"status": "error", "error": str(e)}

# ── WhatsApp ──────────────────────────────────────────────────────────────────
@app.post("/send-whatsapp")
def send_whatsapp(req: WhatsAppRequest):
    phone        = req.phone.strip()
    message      = req.message.strip()
    twilio_sid   = req.twilio_sid.strip()
    twilio_token = req.twilio_token.strip()
    from_number  = req.from_number or "whatsapp:+14155238886"
    if not phone:    raise HTTPException(400, "phone is required")
    if not message:  raise HTTPException(400, "message is required")
    if not twilio_sid or not twilio_token:
        raise HTTPException(400, "twilio_sid and twilio_token are required")
    return send_whatsapp_alert(phone, message, twilio_sid, twilio_token, from_number)

@app.get("/debug/whatsapp")
def debug_whatsapp():
    try:
        import twilio
        return {"status": "ok", "twilio_installed": True, "twilio_version": twilio.__version__}
    except ImportError:
        return {"status": "error", "twilio_installed": False, "error": "Run: pip install twilio"}

# ── Debug endpoints ───────────────────────────────────────────────────────────
@app.post("/trade")
def trade(cmd: TradeCommand):
    if not cmd.command.strip():
        raise HTTPException(400, "Command cannot be empty")
    return place_trade(cmd.command)

@app.get("/debug/symbol")
def debug_symbol(symbol: str = Query(...), exchange: str = Query(None)):
    from core_trading import _exchange_for, _inst_at
    from datetime import datetime
    sym     = symbol.strip().upper()
    exch    = (exchange or _exchange_for(sym)).upper()
    cached  = _get_instruments(exch)
    exact   = next((i for i in cached if i["tradingsymbol"] == sym), None)
    ci      = next((i for i in cached if i["tradingsymbol"].upper() == sym.upper()), None)
    prefix  = sym[:10] if len(sym) > 10 else sym[:6]
    similar = [i["tradingsymbol"] for i in cached if i["tradingsymbol"].startswith(prefix)][:10]
    age_min = int((datetime.now() - _inst_at.get(exch, datetime.now())).total_seconds() // 60)
    return {
        "symbol": sym, "exchange": exch, "found": exact is not None,
        "exact_match": exact["tradingsymbol"] if exact else None,
        "ci_match": ci["tradingsymbol"] if ci and not exact else None,
        "similar": similar, "cache_size": len(cached), "cache_age_min": age_min,
    }

@app.get("/debug/cache/refresh")
def debug_cache_refresh():
    nfo = _get_instruments("NFO", force=True)
    bfo = _get_instruments("BFO", force=True)
    return {"NFO": len(nfo), "BFO": len(bfo), "status": "refreshed"}

@app.get("/debug/config")
def debug_config():
    return {
        "paper_trading_mode": PAPER_TRADING_MODE,
        "market_open":        market_open(),
        "config_file":        CONFIG_FILE,
        "config_exists":      os.path.exists(CONFIG_FILE),
    }

@app.get("/debug/products")
def debug_products():
    """Show available product types and their mappings."""
    return {
        "products": {
            "NRML": {"display": "CNC (Carry Forward)", "description": "Overnight/positional F&O trades"},
            "CNC":  {"display": "CNC (Carry Forward)", "description": "Alias for NRML — accepted by this server"},
            "MIS":  {"display": "MIS (Intraday)",      "description": "Intraday only — auto square-off before 3:20 PM"},
        },
        "default": "NRML"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)