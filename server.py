import time
import math
import threading
import datetime
import os

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# ---------------- shared state ----------------
state_lock = threading.Lock()
state = {
    "spot": None,
    "annual_vol": None,
    "kalshi": {"ticker": None, "yes_ask": None, "close_time": None},
    "status": {"coinbase": "connecting", "binance": "connecting", "kalshi": "connecting"},
}

trade_buffer = []       # list of {id, side, qty, time_ms}
last_trade_id = None
trade_lock = threading.Lock()
FLOW_WINDOW_MS = 15 * 60 * 1000
FLOW_TILT_MAX = 0.18    # max probability shift (fraction) at full weight + full signal


# ---------------- background pollers ----------------
def poll_spot():
    while True:
        try:
            r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=5)
            r.raise_for_status()
            price = float(r.json()["price"])
            with state_lock:
                state["spot"] = price
                state["status"]["coinbase"] = "ok"
        except Exception:
            with state_lock:
                state["status"]["coinbase"] = "error"
        time.sleep(4)


def poll_vol():
    while True:
        try:
            r = requests.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60",
                timeout=5,
            )
            r.raise_for_status()
            candles = r.json()  # newest first: [time, low, high, open, close, volume]
            closes = [c[4] for c in reversed(candles) if c[4] > 0]
            if len(closes) > 10:
                rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
                mean = sum(rets) / len(rets)
                var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
                stdev = math.sqrt(var)
                annual = stdev * math.sqrt(525600)
                with state_lock:
                    state["annual_vol"] = annual
        except Exception:
            with state_lock:
                if state["annual_vol"] is None:
                    state["annual_vol"] = 0.45  # sane fallback default
        time.sleep(60)


def poll_trades():
    global last_trade_id
    while True:
        try:
            if last_trade_id is not None:
                url = (
                    "https://api.binance.com/api/v3/aggTrades"
                    f"?symbol=BTCUSDT&limit=1000&fromId={last_trade_id + 1}"
                )
            else:
                url = "https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=1000"
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            data = r.json()
            if data:
                with trade_lock:
                    for t in data:
                        tid = t["a"]
                        if last_trade_id is not None and tid <= last_trade_id:
                            continue
                        trade_buffer.append(
                            {
                                "id": tid,
                                "side": "sell" if t["m"] else "buy",  # m=True: taker sold
                                "qty": float(t["q"]),
                                "time": t["T"],
                            }
                        )
                    last_trade_id = data[-1]["a"]
                    cutoff = time.time() * 1000 - FLOW_WINDOW_MS - 30000
                    while trade_buffer and trade_buffer[0]["time"] < cutoff:
                        trade_buffer.pop(0)
            with state_lock:
                state["status"]["binance"] = "ok"
        except Exception:
            with state_lock:
                state["status"]["binance"] = "error"
        time.sleep(5)


def poll_kalshi():
    while True:
        try:
            r = requests.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets"
                "?series_ticker=KXBTC15M&status=open&limit=5",
                timeout=6,
            )
            r.raise_for_status()
            markets = r.json().get("markets", [])
            with state_lock:
                if markets:
                    m = markets[0]
                    state["kalshi"] = {
                        "ticker": m.get("ticker"),
                        "yes_ask": (m["yes_ask"] / 100) if m.get("yes_ask") is not None else None,
                        "close_time": m.get("close_time"),
                    }
                    state["status"]["kalshi"] = "ok"
                else:
                    state["status"]["kalshi"] = "no open market found"
        except Exception:
            with state_lock:
                state["status"]["kalshi"] = "error"
        time.sleep(15)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def minutes_to_next_boundary():
    now = datetime.datetime.now(datetime.timezone.utc)
    mins = now.minute + now.second / 60 + now.microsecond / 60_000_000
    r
