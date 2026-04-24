# 📈 Zerodha Trading Terminal

A comprehensive trading system for Zerodha with natural language interface, FastAPI backend, web frontend, and MCP integration for Claude.

## 🌟 Features

- **Natural Language Trading**: Place trades using simple commands like "buy 10 hdfc"
- **Live Quotes**: Get real-time stock prices and OHLC data
- **Position Tracking**: View current positions
- **Order History**: Check all order statuses
- **Web Interface**: Beautiful, responsive web UI
- **REST API**: FastAPI backend for integration
- **MCP Server**: Claude integration for AI-powered trading

## 📋 Prerequisites

- Python 3.8 or higher
- Zerodha account with API access
- API Key and Access Token from Zerodha

## 🚀 Installation

### 1. Clone or download this project

```bash
cd zerodha-trading-terminal
```

### 2. Create virtual environment

```bash
python -m venv venv

# On Windows
venv\Scripts\activate

# On Mac/Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

# Edit .env and add your credentials
# KITE_API_KEY=your_actual_api_key
# KITE_ACCESS_TOKEN=your_actual_access_token
```

## 🔧 Getting Zerodha API Credentials

1. Go to https://kite.zerodha.com/
2. Login to your account
3. Navigate to Apps → Create New App
4. Get your API Key
5. Generate Access Token (valid for 24 hours)

##TWILIO whatsApp
1.https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn
2.https://console.twilio.com/us1/account/keys-credentials/api-keys
3.Recovery code
YQCSKYQ5NQQ4GSEC5KMQMYZQ

4.https://console.twilio.com/ - Auth Token and Account SID




## 📖 Usage

### Start the FastAPI Server

```bash
uvicorn api_server:app --reload --port 8000/
python -m uvicorn api_server:app --reload --port 8000
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Application startup complete.
```

### Access the Web Interface

1. Open `index.html` in your browser
2. The page will automatically test the connection
3. Start trading!

### API Endpoints

**Root**
```bash
GET http://127.0.0.1:8000/
```

**Place Trade**
```bash
POST http://127.0.0.1:8000/trade
Content-Type: application/json

{
  "command": "buy 10 hdfc"
}
```

**Get Quote**
```bash
POST http://127.0.0.1:8000/quote
Content-Type: application/json

{
  "symbol": "HDFCBANK"
}
```

**Get Positions**
```bash
GET http://127.0.0.1:8000/positions
```

**Get Orders**
```bash
GET http://127.0.0.1:8000/orders
```

## 🤖 MCP Server (Claude Integration)

The MCP server allows Claude to interact with your trading system.

### Run MCP Server

```bash
python zerodha_mcp.py
```

### Available MCP Tools

1. **natural_trade** - Place trades using natural language
2. **get_quote** - Get live stock quotes
3. **get_positions** - View current positions
4. **get_orders** - Check order history

## 📝 Supported Stocks

Currently configured stocks (easily expandable):

- HDFC Bank (`hdfc`, `HDFCBANK`)
- ICICI Bank (`icici`, `ICICIBANK`)
- Reliance (`reliance`, `RELIANCE`)
- TCS (`tcs`, `TCS`)
- Infosys (`infosys`, `INFY`)
- SBI (`sbin`, `SBIN`)
- Bharti Airtel (`bharti`, `BHARTIARTL`)
- ITC (`itc`, `ITC`)
- Axis Bank (`axis`, `AXISBANK`)
- Kotak Bank (`kotak`, `KOTAKBANK`)

### Add More Stocks

Edit `core_trading.py` and add to `STOCK_SYMBOLS` dictionary:

```python
STOCK_SYMBOLS = {
    "wipro": "WIPRO",
    "tatamotors": "TATAMOTORS",
    # Add more...
}
```

## 📊 Trading Commands Examples

```
buy 10 hdfc
sell 5 reliance
buy 20 tcs
sell 15 infosys
buy 100 sbin
```

## ⚠️ Important Notes

### Security
- **NEVER** commit your `.env` file to git
- **NEVER** share your API credentials
- Access tokens expire after 24 hours - regenerate daily

### Market Hours
- NSE trading hours: 9:15 AM - 3:30 PM IST (Mon-Fri)
- The system automatically checks if market is open
- Weekend trades will be rejected

### Order Types
- Currently uses **MARKET** orders (immediate execution)
- Product type: **CNC** (delivery)
- Exchange: **NSE**

## 🔍 Troubleshooting

### "Market is closed" error
- Check if it's 9:15 AM - 3:30 PM IST on a weekday
- Verify your system time is correct

### "Connection failed" on web page
- Make sure FastAPI server is running
- Check if using correct port (8000)
- Look at browser console (F12) for errors

### "Invalid API key" or "Token expired"
- Regenerate access token (valid for 24 hours)
- Update `.env` file with new token

### Import errors
- Ensure all dependencies are installed: `pip install -r requirements.txt`
- Activate virtual environment

## 📁 Project Structure

```
zerodha-trading-terminal/
├── core_trading.py       # Core trading logic
├── api_server.py         # FastAPI REST API
├── zerodha_mcp.py        # MCP server for Claude
├── index.html            # Web interface
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variables template
├── .gitignore           # Git ignore rules
└── README.md            # This file
```

## 🛡️ Risk Disclaimer

**This software is for educational purposes only.**

- Trading involves substantial risk of loss
- Past performance does not guarantee future results
- The authors are not responsible for any financial losses
- Always verify trades before execution
- Start with paper trading or small amounts
- Understand the risks before trading

## 📄 License

MIT License - Use at your own risk

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## 📧 Support

For issues:
- Check the troubleshooting section
- Review Zerodha API documentation
- Open an issue on GitHub

## 🙏 Acknowledgments

- Built with [KiteConnect](https://kite.trade/) Python library
- Powered by [FastAPI](https://fastapi.tiangolo.com/)
- MCP integration for [Claude](https://www.anthropic.com/claude)

---

**Happy Trading! 🚀**

*Remember: Only invest what you can afford to lose.*