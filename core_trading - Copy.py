
# Strategy: selectByDelta + selectByVega (frontend handles all selection logic)
# Backend responsibility:
#   1. Return ALL strikes with real IV computed from LTP (threshold 0.1)
#   2. Use ATM IV as fallback (NOT hardcoded 18%) — critical for expiry-day accuracy
#   3. Skip only truly dead strikes: ltp=0 AND more than 8 steps from ATM

from kiteconnect import KiteConnect
import re, math, time as time_mod, concurrent.futures
from datetime import datetime, time as dtime, timedelta
import os
from dotenv import load_dotenv

load_dotenv()  # Load .env file

api_key = os.getenv("API_KEY")
access_token = os.getenv("ACCESS_TOKEN")

# ─── Credentials ──────────────────────────────────────────────────────────────
kite = KiteConnect(api_key=api_key)
kite.set_access_token(access_token)

STOCK_SYMBOLS = {
    "hdfc":"HDFCBANK","icici":"ICICIBANK","reliance":"RELIANCE","tcs":"TCS",
    "infosys":"INFY","sbin":"SBIN","bharti":"BHARTIARTL","itc":"ITC",
    "axis":"AXISBANK","kotak":"KOTAKBANK","yesbank":"YESBANK",
}

INDICES = {
    "NIFTY 50":"NSE:NIFTY 50","NIFTY BANK":"NSE:NIFTY BANK","SENSEX":"BSE:SENSEX",
    "NIFTY IT":"NSE:NIFTY IT","NIFTY FIN SERVICE":"NSE:NIFTY FIN SERVICE",
    "NIFTY MIDCAP 50":"NSE:NIFTY MIDCAP 50","NIFTY NEXT 50":"NSE:NIFTY NEXT 50",
    "INDIA VIX":"NSE:INDIA VIX",
}

INDEX_CONFIG = {
    "NIFTY":      {"spot":"NSE:NIFTY 50",          "exchange":"NFO","step":100, "lot":65 },
    "BANKNIFTY":  {"spot":"NSE:NIFTY BANK",        "exchange":"NFO","step":100,"lot":30 },
    "SENSEX":     {"spot":"BSE:SENSEX",            "exchange":"BFO","step":100,"lot":20 },
    "FINNIFTY":   {"spot":"NSE:NIFTY FIN SERVICE", "exchange":"NFO","step":100, "lot":60 },
    "MIDCPNIFTY": {"spot":"NSE:NIFTY MIDCAP 50",   "exchange":"NFO","step":25, "lot":120},
}

SLUG_TO_INDEX = {
    "nifty50":"NIFTY","nifty":"NIFTY","banknifty":"BANKNIFTY","niftybank":"BANKNIFTY",
    "sensex":"SENSEX","finnifty":"FINNIFTY","niftyfinservice":"FINNIFTY","niftyfin":"FINNIFTY",
    "midcpnifty":"MIDCPNIFTY","niftymidcap50":"MIDCPNIFTY",
}

# ── Chain inclusion rules ─────────────────────────────────────────────────────
# Backend returns ALL strikes with valid Greeks — frontend selectByDelta /
# selectByVega handle all selection logic. No delta/vega thresholds here.
# Only rule: skip strikes with ltp=0 that are far from ATM (truly dead).
MIN_LTP_FOR_IV  = 0.1   # min LTP to compute real IV; else use ATM IV fallback
NEAR_ATM_STEPS  = 20    # include all strikes within num_strikes window even if ltp=0
                        # ensures target-delta strikes are never skipped in after-hours

# ── Helpers ──────────────────────────────────────────────────────────────────
def market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5: return False
    return dtime(9,15) <= now.time() <= dtime(15,30)

def _norm_date(val) -> str:
    if hasattr(val,"strftime"): return val.strftime("%Y-%m-%d")
    return str(val)[:10]

def _exchange_for(symbol: str) -> str:
    s = symbol.upper()
    return "BFO" if (s.startswith("SENSEX") or s.startswith("BANKEX")) else "NFO"

# ── Instrument cache (4h TTL) ─────────────────────────────────────────────────
_inst_cache: dict = {}
_inst_at:    dict = {}

def _get_instruments(exchange: str, force: bool = False) -> list:
    now  = datetime.now()
    last = _inst_at.get(exchange)
    if force or last is None or (now - last) > timedelta(hours=4):
        print(f"[cache] Downloading {exchange}...", end="", flush=True)
        try:
            _inst_cache[exchange] = kite.instruments(exchange)
            _inst_at[exchange]    = now
            print(f" {len(_inst_cache[exchange])} instruments cached")
        except Exception as e:
            print(f" FAILED: {e}")
            return _inst_cache.get(exchange, [])
    return _inst_cache.get(exchange, [])

# ── Black-Scholes ─────────────────────────────────────────────────────────────
def _npdf(x): return math.exp(-0.5*x*x)/math.sqrt(2*math.pi)
def _ncdf(x):
    t=1.0/(1.0+0.2316419*abs(x))
    p=t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
    c=1.0-_npdf(x)*p
    return c if x>=0 else 1.0-c

def bs_greeks(S,K,T,r,sigma,opt_type="CE"):
    if T<=0 or sigma<=0 or S<=0 or K<=0: return None
    try:
        sq=math.sqrt(T); d1=(math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*sq); d2=d1-sigma*sq; nd1=_npdf(d1)
        # Theta /252 = per trading day (matches Angel One / broker convention)
        # Old /365 was per calendar day → 44% too small
        # Vega /100 = per 1% IV move (matches broker convention) ✓
        if opt_type=="CE":
            delta=_ncdf(d1); theta=(-(S*nd1*sigma)/(2*sq)-r*K*math.exp(-r*T)*_ncdf(d2))/252; rho=K*T*math.exp(-r*T)*_ncdf(d2)/100
        else:
            delta=_ncdf(d1)-1; theta=(-(S*nd1*sigma)/(2*sq)+r*K*math.exp(-r*T)*_ncdf(-d2))/252; rho=-K*T*math.exp(-r*T)*_ncdf(-d2)/100
        return {"delta":round(delta,6),"gamma":round(nd1/(S*sigma*sq),6),"theta":round(theta,6),"vega":round(S*nd1*sq/100,6),"rho":round(rho,6)}
    except: return None

def calc_iv(S,K,T,r,mkt_price,opt_type="CE",max_iter=100):
    if T<=0 or mkt_price<=0.5: return None
    sigma=0.20
    for _ in range(max_iter):
        try:
            sq=math.sqrt(T); d1=(math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*sq); d2=d1-sigma*sq; nd1=_npdf(d1)
            price=(S*_ncdf(d1)-K*math.exp(-r*T)*_ncdf(d2) if opt_type=="CE" else K*math.exp(-r*T)*_ncdf(-d2)-S*_ncdf(-d1))
            vraw=S*nd1*sq; diff=mkt_price-price
            if abs(diff)<1e-5: break
            if vraw<1e-8: return None
            sigma+=diff/vraw
            if sigma<=0: return None
        except: return None
    return round(sigma*100,2) if 0.01<sigma<5 else None

# ── Get Greeks — v3.5.0 ──────────────────────────────────────────────────────
def get_greeks(index_name:str, expiry:str=None, num_strikes:int=10) -> dict:
    index_name=index_name.upper().strip()
    cfg=INDEX_CONFIG.get(index_name)
    if not cfg: return {"status":"error","error":f"Unknown index '{index_name}'. Valid: {list(INDEX_CONFIG)}"}
    try:
        # ── Fetch LIVE spot — retry once if value looks stale/wrong ──
        # Wrong spot = wrong ATM = wrong deltas for ALL strikes
        # NIFTY valid range: 15000-30000 | BANKNIFTY: 40000-80000 | SENSEX: 60000-120000
        SPOT_RANGE = {
            "NIFTY":      (15000, 32000),
            "BANKNIFTY":  (35000, 85000),
            "SENSEX":     (55000, 125000),
            "FINNIFTY":   (15000, 32000),
            "MIDCPNIFTY": (8000,  20000),
        }
        spot_min, spot_max = SPOT_RANGE.get(index_name, (1000, 200000))

        def _fetch_spot():
            q = kite.quote([cfg["spot"]])
            q0 = q.get(cfg["spot"], {})
            s = float(q0.get("last_price", 0) or 0)
            if s <= 0:
                ohlc = q0.get("ohlc", {})
                s = float(ohlc.get("close", 0) or ohlc.get("open", 0) or 0)
            return s

        spot = _fetch_spot()
        # Sanity check: if spot is outside valid range, wait 1s and retry once
        if not (spot_min <= spot <= spot_max):
            print(f"[get_greeks] WARNING: spot={spot} outside valid range [{spot_min},{spot_max}] for {index_name} — retrying")
            time_mod.sleep(1)
            spot = _fetch_spot()
        if spot <= 0:
            return {"status":"error","error":f"Spot price unavailable for {index_name}. Market may be closed."}
        if not (spot_min <= spot <= spot_max):
            return {"status":"error","error":f"Spot price {spot} is invalid for {index_name} (expected {spot_min}–{spot_max}). Kite API may be returning stale data."}

        step=cfg["step"]; atm=round(spot/step)*step; exchange=cfg["exchange"]
        print(f"[get_greeks] {index_name} spot={spot} ATM={atm} expiry={expiry} num_strikes={num_strikes}")
        all_insts=_get_instruments(exchange)
        if not all_insts:
            return {"status":"error","error":f"Instrument cache empty for {exchange}."}

        idx_insts=[i for i in all_insts if str(i.get("name","")).strip().upper()==index_name and i["instrument_type"] in ("CE","PE")]
        if not idx_insts:
            all_insts=_get_instruments(exchange,force=True)
            idx_insts=[i for i in all_insts if str(i.get("name","")).strip().upper()==index_name and i["instrument_type"] in ("CE","PE")]
            if not idx_insts:
                return {"status":"error","error":f"No F&O instruments for '{index_name}' on {exchange}."}

        all_expiries=sorted(set(_norm_date(i["expiry"]) for i in idx_insts))
        target_expiry=expiry if (expiry and expiry in all_expiries) else all_expiries[0]

        strikes_needed={atm+step*o for o in range(-num_strikes,num_strikes+1)}
        chain_insts=[i for i in idx_insts if _norm_date(i["expiry"])==target_expiry and float(i["strike"]) in strikes_needed]
        if not chain_insts:
            all_s=sorted(set(float(i["strike"]) for i in idx_insts if _norm_date(i["expiry"])==target_expiry))
            if all_s:
                closest=min(all_s,key=lambda s:abs(s-atm))
                strikes_needed={closest+step*o for o in range(-num_strikes,num_strikes+1)}
                chain_insts=[i for i in idx_insts if _norm_date(i["expiry"])==target_expiry and float(i["strike"]) in strikes_needed]
            if not chain_insts:
                return {"status":"error","error":f"No instruments near ATM {atm} for {target_expiry}."}

        sym_keys=[f"{exchange}:{i['tradingsymbol']}" for i in chain_insts]
        quotes={}
        for i in range(0,len(sym_keys),400):
            try: quotes.update(kite.quote(sym_keys[i:i+400]))
            except Exception as e: print(f"[greeks] Quote batch {i//400} error: {e}")

        now=datetime.now()
        exp_dt=datetime.strptime(target_expiry,"%Y-%m-%d").replace(hour=15,minute=30)
        T_secs=max((exp_dt-now).total_seconds(),60)
        T_yrs=T_secs/(365*24*3600); T_days=T_secs/86400; r_free=0.065

        # ── v3.4.0: Pre-compute ATM IV to use as fallback instead of hardcoded 18% ──
        # CRITICAL FIX: On expiry day ATM IV is 30-50%, NOT 18%.
        # Using IV=18% as fallback gives completely wrong deltas for OTM strikes
        # (e.g. our model gets Δ=0.068 instead of Δ=0.18 for the same strike).
        # Solution: compute real ATM IV from ATM strike LTP, use that as fallback.
        atm_iv_fallback = 0.18   # will be overwritten with real ATM IV below
        for _otype in ("CE", "PE"):
            _atm_inst = next((i for i in chain_insts
                              if float(i["strike"]) == atm and i["instrument_type"] == _otype), None)
            if _atm_inst:
                _fkey = f"{exchange}:{_atm_inst['tradingsymbol']}"
                _ltp  = float(quotes.get(_fkey, {}).get("last_price", 0) or 0)
                if _ltp > 0.1:
                    _iv = calc_iv(spot, atm, T_yrs, r_free, _ltp, _otype)
                    if _iv:
                        atm_iv_fallback = _iv / 100
                        print(f"[get_greeks] ATM IV fallback: {_otype} @{atm} LTP={_ltp} → IV={_iv:.1f}%")
                        break
        if atm_iv_fallback == 0.18:
            print(f"[get_greeks] WARNING: ATM IV fallback stuck at 18% — ATM LTP unavailable")

        chain=[]
        skipped_zero=0
        for inst in chain_insts:
            sym=inst["tradingsymbol"]; fkey=f"{exchange}:{sym}"; q=quotes.get(fkey,{})
            ltp=float(q.get("last_price",0) or 0); oi=int(q.get("oi",0) or 0); vol=int(q.get("volume",0) or 0)
            K=float(inst["strike"]); opt_type=inst["instrument_type"]

            # Skip only truly dead strikes: zero LTP AND far from ATM
            near_atm = abs(K - atm) <= step * NEAR_ATM_STEPS
            if ltp <= 0 and not near_atm:
                skipped_zero += 1
                continue

            # Use real LTP for IV when available; fall back to ATM IV (not hardcoded 18%)
            iv_pct = calc_iv(spot, K, T_yrs, r_free, ltp, opt_type) if ltp > MIN_LTP_FOR_IV else None
            iv_dec = (iv_pct / 100) if iv_pct else atm_iv_fallback
            g = bs_greeks(spot, K, T_yrs, r_free, iv_dec, opt_type)
            if not g: continue
            # Note: no delta/vega threshold filter — frontend selectByDelta/selectByVega
            # handle all selection logic and pick the best match themselves.

            m="ATM" if K==atm else ("ITM" if (opt_type=="CE" and K<spot) or (opt_type=="PE" and K>spot) else "OTM")
            intrinsic=max(0.0,(spot-K) if opt_type=="CE" else (K-spot))
            depth=q.get("depth",{})
            bid=float((depth.get("buy",[{}])[0] or {}).get("price") or ltp)
            ask=float((depth.get("sell",[{}])[0] or {}).get("price") or ltp)
            chain.append({"strike":K,"type":opt_type,"symbol":sym,"moneyness":m,"ltp":round(ltp,2),
                "bid":round(bid,2),"ask":round(ask,2),"spread":round(ask-bid,2),"iv_pct":iv_pct,
                "delta":g["delta"],"gamma":g["gamma"],"theta":g["theta"],"vega":g["vega"],"rho":g["rho"],
                "intrinsic":round(intrinsic,2),"extrinsic":round(max(0.0,ltp-intrinsic),2),"oi":oi,"volume":vol})

        if skipped_zero:
            print(f"[get_greeks] Skipped {skipped_zero} near-zero delta/vega strikes for {index_name} {target_expiry}")

        if not chain:
            return {"status":"error","error":f"Greeks calculation failed for all strikes near ATM {atm}. Market may be closed."}

        # ── Sanity check: ATM CE delta should be near 0.50 ──
        # If spot is wrong, ATM delta will be far from 0.50 → warn clearly
        atm_ce = next((r for r in chain if r["strike"]==atm and r["type"]=="CE"), None)
        if atm_ce:
            atm_delta = atm_ce["delta"]
            if abs(atm_delta - 0.50) > 0.15:
                print(f"[get_greeks] WARNING: ATM CE delta={atm_delta:.4f} (expected ~0.50). "
                      f"Spot={spot} may be stale. Check live spot feed.")

        chain.sort(key=lambda x:(x["strike"],x["type"]))
        return {"status":"success","index":index_name,"spot":round(spot,2),"expiry":target_expiry,
                "all_expiries":all_expiries,"dte_days":round(T_days,1),"atm_strike":atm,
                "lot_size":cfg["lot"],"step":step,"market_open":market_open(),"chain":chain}
    except Exception as e:
        import traceback; print(f"[get_greeks] ERROR:\n{traceback.format_exc()}")
        return {"status":"error","error":str(e)}

# ── Order placement (unchanged from v3.2.0) ──────────────────────────────────
def _resolve_product(product: str):
    """Resolve product string to (kite_constant, display_label)."""
    p = product.upper()
    if p in ("NRML", "CNC"):
        return kite.PRODUCT_NRML, "CNC (Carry Forward)"
    elif p == "MIS":
        return kite.PRODUCT_MIS, "MIS (Intraday)"
    return kite.PRODUCT_NRML, "CNC (Carry Forward)"

def _verify_order(order_id:str, max_wait:int=3) -> str:
    deadline=time_mod.time()+max_wait
    while time_mod.time()<deadline:
        try:
            history=kite.order_history(order_id)
            if history:
                status=history[-1].get("status","UNKNOWN")
                if status in ("COMPLETE","REJECTED","CANCELLED"): return status
        except: pass
        time_mod.sleep(0.5)
    try:
        h=kite.order_history(order_id); return h[-1].get("status","PENDING") if h else "PENDING"
    except: return "PENDING"

def _validate_leg(leg:dict, kite_prod, cached_nfo:list, cached_bfo:list) -> dict:
    symbol=str(leg.get("symbol","")).strip().upper()
    txn_str=str(leg.get("transaction_type","")).strip().upper()
    quantity=int(leg.get("quantity",0))
    row={"symbol":symbol,"transaction_type":txn_str,"quantity":quantity,"order_id":None,
         "order_status":None,"status":"pending","message":"","_kite_prod":kite_prod,
         "ltp":float(leg.get("ltp",0) or 0),"strike":float(leg.get("strike",0) or 0),"expiry":str(leg.get("expiry","") or "")}
    if not symbol: row.update({"status":"error","message":"Empty symbol"}); return row
    if txn_str not in ("BUY","SELL"): row.update({"status":"error","message":f"Bad transaction_type '{txn_str}'"}); return row
    if quantity<=0: row.update({"status":"error","message":f"Bad quantity {quantity}"}); return row
    detected_lot=None
    for idx_name in sorted(INDEX_CONFIG.keys(),key=len,reverse=True):
        if symbol.startswith(idx_name): detected_lot=INDEX_CONFIG[idx_name]["lot"]; break
    if detected_lot and quantity%detected_lot!=0:
        row.update({"status":"error","message":f"Qty {quantity} not a multiple of lot size {detected_lot}."}); return row
    exchange=_exchange_for(symbol); cached=cached_nfo if exchange=="NFO" else cached_bfo
    valid=next((i for i in cached if i["tradingsymbol"]==symbol),None)
    if not valid:
        refreshed=_get_instruments(exchange,force=True)
        valid=next((i for i in refreshed if i["tradingsymbol"]==symbol),None)
        if not valid:
            ci=next((i for i in refreshed if i["tradingsymbol"].upper()==symbol.upper()),None)
            pfx=symbol[:10] if len(symbol)>10 else symbol[:6]
            sim=[i["tradingsymbol"] for i in refreshed if i["tradingsymbol"].startswith(pfx)][:5]
            diag=(f"Case mismatch — Zerodha has '{ci['tradingsymbol']}'" if ci else f"Similar: {sim}" if sim else f"No match with prefix '{pfx}'")
            row.update({"status":"error","message":f"Symbol '{symbol}' not found. {diag}"}); return row
    row["status"]="ready"; row["_exchange"]=exchange; return row

def _fire_one_leg(row:dict) -> dict:
    txn=kite.TRANSACTION_TYPE_BUY if row["transaction_type"]=="BUY" else kite.TRANSACTION_TYPE_SELL
    try:
        oid=kite.place_order(variety=kite.VARIETY_REGULAR,exchange=row["_exchange"],
            tradingsymbol=row["symbol"],transaction_type=txn,quantity=row["quantity"],
            order_type=kite.ORDER_TYPE_MARKET,product=row["_kite_prod"],validity=kite.VALIDITY_DAY)
        row["order_id"]=oid; vstatus=_verify_order(oid,max_wait=3); row["order_status"]=vstatus
        if vstatus=="REJECTED": row.update({"status":"rejected","message":f"REJECTED by exchange. OID:{oid}"})
        else: row.update({"status":"success","message":f"OID:{oid} → {vstatus}"})
    except Exception as e: row.update({"status":"error","message":str(e)})
    row.pop("_kite_prod",None); row.pop("_exchange",None); return row

def place_option_legs(legs:list, product:str="NRML", allow_after_hours:bool=False) -> dict:
    if not allow_after_hours and not market_open():
        return {"status":"error","error":"Market closed — NSE F&O: Mon–Fri 09:15–15:30 IST"}
    if not legs: return {"status":"error","error":"No legs provided"}
    kite_prod, product_label = _resolve_product(product)
    cached_nfo=_get_instruments("NFO"); cached_bfo=_get_instruments("BFO")
    validated=[_validate_leg(leg,kite_prod,cached_nfo,cached_bfo) for leg in legs]
    if any(r["status"]=="error" for r in validated):
        for r in validated: r.pop("_kite_prod",None); r.pop("_exchange",None)
        return {"status":"error","results":validated,"placed":0,"failed":len(legs)}
    ready=[r for r in validated if r["status"]=="ready"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ready)) as ex:
        futures={ex.submit(_fire_one_leg,row):row for row in ready}
        results=[f.result() for f in concurrent.futures.as_completed(futures)]
    order_map={r["symbol"]:r for r in results}
    ordered=[order_map.get(str(leg.get("symbol","")).strip().upper(),results[i]) for i,leg in enumerate(legs)]
    placed=sum(1 for r in ordered if r["status"]=="success")
    failed=sum(1 for r in ordered if r["status"] in ("error","rejected"))
    overall="success" if failed==0 else ("error" if placed==0 else "partial")
    return {"status":overall,"results":ordered,"placed":placed,"failed":failed,"product_label":product_label}

def exit_option_legs(legs:list, product:str="NRML") -> dict:
    exit_legs=[]
    for leg in legs:
        orig=(leg.get("transaction_type") or leg.get("original_transaction_type") or "SELL").strip().upper()
        exit_legs.append({"symbol":str(leg.get("symbol","")).strip().upper(),
            "transaction_type":"BUY" if orig=="SELL" else "SELL",
            "quantity":int(leg.get("quantity",0)),"ltp":float(leg.get("ltp",0) or 0),
            "strike":float(leg.get("strike",0) or 0),"expiry":str(leg.get("expiry","") or "")})
    return place_option_legs(exit_legs,product=product,allow_after_hours=True)

def check_order_margins(legs:list, product:str="NRML") -> dict:
    try:
        margins_data=kite.margins()
        available=float(margins_data.get("equity",{}).get("available",{}).get("live_balance",0) or 0)
        orders=[{"exchange":_exchange_for(str(leg.get("symbol","")).upper()),
                 "tradingsymbol":str(leg.get("symbol","")).upper(),
                 "transaction_type":str(leg.get("transaction_type","BUY")).upper(),
                 "variety":"regular","product":product.upper(),"order_type":"MARKET",
                 "quantity":int(leg.get("quantity",0)),"price":0,"trigger_price":0} for leg in legs]
        margin_data=kite.order_margins(orders)
        total_required=float(sum(m.get("total",0) for m in margin_data) if isinstance(margin_data,list) else margin_data.get("total",0))
        return {"status":"success","required_margin":round(total_required,2),"available_balance":round(available,2),
                "sufficient":available>=total_required,"shortfall":round(max(0.0,total_required-available),2),
                "source":"zerodha_basket_margin"}
    except Exception as e: return {"status":"error","error":str(e)}

# ── Market data (unchanged) ──────────────────────────────────────────────────
def get_all_indices() -> dict:
    try:
        quotes=kite.quote(list(INDICES.values())); data=[]
        for name,sym in INDICES.items():
            if sym not in quotes: continue
            q=quotes[sym]; lp=q.get("last_price",0); pc=q["ohlc"].get("close",0)
            ch=lp-pc if pc else 0; pct=(ch/pc*100) if pc else 0
            data.append({"name":name,"symbol":sym,"last_price":lp,"open":q["ohlc"].get("open",0),
                "high":q["ohlc"].get("high",0),"low":q["ohlc"].get("low",0),"close":pc,
                "change":round(ch,2),"change_percent":round(pct,2),"timestamp":str(q.get("last_trade_time",""))})
        return {"status":"success","indices":data,"market_open":market_open(),"timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e: return {"status":"error","error":str(e)}

def get_quote(symbol:str) -> dict:
    try:
        symbol=symbol.upper(); q=kite.quote(f"NSE:{symbol}")[f"NSE:{symbol}"]
        return {"status":"success","symbol":symbol,"last_price":q["last_price"],"open":q["ohlc"]["open"],
                "high":q["ohlc"]["high"],"low":q["ohlc"]["low"],"close":q["ohlc"]["close"],"volume":q.get("volume",0)}
    except Exception as e: return {"status":"error","error":str(e)}

def get_positions() -> dict:
    try:
        p=kite.positions(); return {"status":"success","net_positions":p.get("net",[]),"day_positions":p.get("day",[])}
    except Exception as e: return {"status":"error","error":str(e)}

def get_orders() -> dict:
    try: return {"status":"success","orders":kite.orders()}
    except Exception as e: return {"status":"error","error":str(e)}

def get_holdings() -> dict:
    try: return {"status":"success","holdings":kite.holdings()}
    except Exception as e: return {"status":"error","error":str(e)}

def get_margins() -> dict:
    try:
        m=kite.margins(); eq=m.get("equity",{}); avail=eq.get("available",{})
        return {"status":"success","equity":eq,"commodity":m.get("commodity",{}),
                "available_balance":float(avail.get("live_balance",0) or 0),
                "used_margin":float(eq.get("utilised",{}).get("debits",0) or 0)}
    except Exception as e: return {"status":"error","error":str(e)}

def get_support_resistance(index:str) -> dict:
    m={"nifty50":"NSE:NIFTY 50","banknifty":"NSE:NIFTY BANK","sensex":"BSE:SENSEX","finnifty":"NSE:NIFTY FIN SERVICE"}
    sym=m.get(index.lower())
    if not sym: return {"error":"Invalid index"}
    try:
        q=kite.quote(sym)[sym]["ohlc"]; h,l,c=q["high"],q["low"],q["close"]; p=(h+l+c)/3
        return {"pivot":round(p,2),"support_1":round(2*p-h,2),"resistance_1":round(2*p-l,2),
                "support_2":round(p-(h-l),2),"resistance_2":round(p+(h-l),2)}
    except Exception as e: return {"error":str(e)}

def place_trade(command:str) -> dict:
    try:
        if not market_open(): return {"error":"Market closed"}
        text=command.lower()
        if "buy" in text: txn,tt=kite.TRANSACTION_TYPE_BUY,"BUY"
        elif "sell" in text: txn,tt=kite.TRANSACTION_TYPE_SELL,"SELL"
        else: return {"error":"Specify BUY or SELL"}
        symbol=next((v for k,v in STOCK_SYMBOLS.items() if k in text),None)
        if not symbol: return {"error":"Stock not recognized"}
        m=re.search(r"\d+",text); qty=int(m.group()) if m else 1
        oid=kite.place_order(variety=kite.VARIETY_REGULAR,exchange=kite.EXCHANGE_NSE,
            tradingsymbol=symbol,transaction_type=txn,quantity=qty,
            order_type=kite.ORDER_TYPE_MARKET,product=kite.PRODUCT_CNC,validity=kite.VALIDITY_DAY)
        return {"status":"success","order_id":oid,"symbol":symbol,"quantity":qty,"transaction_type":tt}
    except Exception as e: return {"status":"error","error":str(e)}

def get_option_chain(index:str) -> dict:
    return {"index":index.upper(),"note":"Use /greeks for chain with Greeks"}

def place_option_order(symbol:str, qty:int, transaction_type:str, product:str="NRML") -> dict:
    return place_option_legs([{"symbol":symbol,"transaction_type":transaction_type,"quantity":qty}],product=product)

def send_whatsapp_alert(phone:str, message:str, twilio_sid:str, twilio_token:str,
                        from_number:str="whatsapp:+14155238886") -> dict:
    try:
        from twilio.rest import Client
        phone=phone.strip()
        if not phone.startswith("whatsapp:"):
            if phone.startswith("+"): phone="whatsapp:"+phone
            else: return {"status":"error","error":"Phone must be whatsapp:+CCXXXXXXXXXX or +CCXXXXXXXXXX"}
        client=Client(twilio_sid,twilio_token)
        msg=client.messages.create(from_=from_number,to=phone,body=message)
        return {"status":"success","sid":msg.sid,"phone":phone,"from":from_number}
    except ImportError: return {"status":"error","error":"twilio not installed. Run: pip install twilio"}
    except Exception as e: return {"status":"error","error":str(e)}