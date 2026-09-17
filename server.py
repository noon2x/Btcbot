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
    print("poll_spot: thread started", flush=True)
    while True:
        try:
            r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=8)
            r.raise_for_status()
            price = float(r.json()["price"])
            with state_lock:
                state["spot"] = price
                state["status"]["coinbase"] = "ok"
            print(f"poll_spot: ok, price={price}", flush=True)
        except Exception as e:
            with state_lock:
                state["status"]["coinbase"] = "error"
            print(f"poll_spot: ERROR {type(e).__name__}: {e}", flush=True)
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
    print("poll_kalshi: thread started", flush=True)
    while True:
        try:
            r = requests.get(
                "https://api.elections.kalshi.com/trade-api/v2/markets"
                "?series_ticker=KXBTC15M&status=open&limit=5",
                timeout=8,
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
            print(f"poll_kalshi: ok, {len(markets)} markets", flush=True)
        except Exception as e:
            with state_lock:
                state["status"]["kalshi"] = "error"
            print(f"poll_kalshi: ERROR {type(e).__name__}: {e}", flush=True)
        time.sleep(15)


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def minutes_to_next_boundary():
    now = datetime.datetime.now(datetime.timezone.utc)
    mins = now.minute + now.second / 60 + now.microsecond / 60_000_000
    rem = 15 - (mins % 15)
    return 15.0 if rem <= 0.001 else rem


# ---------------- routes ----------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with state_lock:
        spot = state["spot"]
        vol = state["annual_vol"]
        kalshi = dict(state["kalshi"])
        status = dict(state["status"])

    # target: client sends its own value, or "auto" to use live spot
    target_param = request.args.get("target")
    if target_param and target_param != "auto":
        try:
            target = float(target_param)
        except ValueError:
            target = spot
    else:
        target = spot

    # minutes left: client value, or auto-computed to next 15-min clock boundary,
    # or (best) derived from the live Kalshi market's actual close_time
    mins_param = request.args.get("mins_left")
    if mins_param and mins_param != "auto":
        try:
            mins_left = float(mins_param)
        except ValueError:
            mins_left = minutes_to_next_boundary()
    else:
        mins_left = minutes_to_next_boundary()
        if kalshi.get("close_time"):
            try:
                close_dt = datetime.datetime.strptime(kalshi["close_time"], "%Y-%m-%dT%H:%M:%SZ")
                close_dt = close_dt.replace(tzinfo=datetime.timezone.utc)
                calc = (close_dt.timestamp() - time.time()) / 60
                if 0 < calc < 20:
                    mins_left = calc
            except Exception:
                pass

    whale_thresh = float(request.args.get("whale_thresh", 5))
    flow_weight = max(0.0, min(1.0, float(request.args.get("flow_weight", 40)) / 100))

    # order-flow aggregation over trailing window
    cutoff = time.time() * 1000 - FLOW_WINDOW_MS
    buy_vol = sell_vol = whale_buy_vol = whale_sell_vol = 0.0
    whale_buy_count = whale_sell_count = 0
    with trade_lock:
        snapshot = list(trade_buffer)
    for t in snapshot:
        if t["time"] < cutoff:
            continue
        if t["side"] == "buy":
            buy_vol += t["qty"]
            if t["qty"] >= whale_thresh:
                whale_buy_vol += t["qty"]
                whale_buy_count += 1
        else:
            sell_vol += t["qty"]
            if t["qty"] >= whale_thresh:
                whale_sell_vol += t["qty"]
                whale_sell_count += 1

    total_vol = buy_vol + sell_vol
    net_ratio = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0
    whale_total = whale_buy_vol + whale_sell_vol
    whale_ratio = (whale_buy_vol - whale_sell_vol) / whale_total if whale_total > 0 else 0.0
    combined_signal = (0.4 * net_ratio + 0.6 * whale_ratio) if whale_total > 0 else net_ratio

    p_base = p_final = tilt = None
    if spot and target and vol and mins_left and mins_left > 0:
        T = mins_left / 525600.0
        try:
            d2 = (math.log(spot / target) - 0.5 * vol * vol * T) / (vol * math.sqrt(T))
            p_base = norm_cdf(d2)
            tilt = flow_weight * combined_signal * FLOW_TILT_MAX
            p_final = min(0.995, max(0.005, p_base + tilt))
        except (ValueError, ZeroDivisionError):
            pass

    return jsonify(
        {
            "spot": spot,
            "target": target,
            "annual_vol": vol,
            "mins_left": mins_left,
            "kalshi": kalshi,
            "status": status,
            "flow": {
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "whale_buy_vol": whale_buy_vol,
                "whale_sell_vol": whale_sell_vol,
                "whale_buy_count": whale_buy_count,
                "whale_sell_count": whale_sell_count,
                "combined_signal": combined_signal,
            },
            "prob": {"base": p_base, "tilt": tilt, "final": p_final},
        }
    )


print("server.py: starting background pollers...", flush=True)
for fn in (poll_spot, poll_vol, poll_trades, poll_kalshi):
    threading.Thread(target=fn, daemon=True).start()
print("server.py: pollers launched", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
