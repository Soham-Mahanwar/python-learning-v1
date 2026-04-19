# Paper trading margin calculation — no Zerodha API needed
# Uses NSE SPAN margin formula: ~18-20% of contract value for index options

INDEX_MARGIN_PCT = {
    "NIFTY":      0.105,   # ~10.5% SPAN + 3% exposure = ~13.5% total (actual Zerodha rates)
    "BANKNIFTY":  0.105,
    "FINNIFTY":   0.105,
    "MIDCPNIFTY": 0.105,
    "SENSEX":     0.105,
}

# Lot sizes (copy from core_trading.py)
INDEX_LOT = {
    "NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20,
    "FINNIFTY": 60, "MIDCPNIFTY": 120,
}

def calc_paper_margin(legs: list, spot_prices: dict = None) -> dict:
    """
    Calculate margin for paper trading without Zerodha API.
    
    For SELL options: SPAN margin = Strike × Qty × margin_pct
    For BUY options:  Premium = LTP × Qty (no margin, just debit)
    
    legs: [{symbol, transaction_type, quantity, strike, ltp, index_name}]
    Returns same format as check_order_margins()
    """
    total_margin = 0.0
    leg_margins = []
    
    for leg in legs:
        txn = str(leg.get("transaction_type", "SELL")).upper()
        qty = int(leg.get("quantity", 0))
        ltp = float(leg.get("ltp", 0) or 0)
        strike = float(leg.get("strike", 0) or 0)
        symbol = str(leg.get("symbol", ""))
        
        # Detect index from symbol prefix
        index = "NIFTY"  # default
        for idx in sorted(INDEX_LOT.keys(), key=len, reverse=True):
            if symbol.upper().startswith(idx):
                index = idx
                break
        
        margin_pct = INDEX_MARGIN_PCT.get(index, 0.105)
        
        if txn == "SELL":
            # SPAN margin on contract value
            if strike > 0:
                leg_margin = strike * qty * margin_pct
            else:
                leg_margin = ltp * qty * 5  # fallback
        else:
            # BUY — just premium paid
            leg_margin = ltp * qty
        
        total_margin += leg_margin
        leg_margins.append({
            "symbol": symbol,
            "transaction_type": txn,
            "quantity": qty,
            "margin": round(leg_margin, 2),
        })
    
    return {
        "status": "success",
        "required_margin": round(total_margin, 2),
        "available_balance": 10_000_000.0,  # virtual 1Cr for paper trading
        "sufficient": True,
        "shortfall": 0.0,
        "leg_margins": leg_margins,
        "source": "paper_span_estimate",
        "note": "Paper trading estimate: SELL=Strike×Qty×10.5%, BUY=LTP×Qty"
    }   