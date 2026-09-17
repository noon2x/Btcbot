import time
import math
import threading
import datetime
import os

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

cache_lock = threading.Lock()
CACHE = {
    "spot": None, "spot_ts": 0, "spot_err": None,
    "vol": None, "vol_ts": 0,
    "kalshi": {"ticker": None, "yes_ask": None, "close_time": None}, "kalshi_ts": 0, "kalshi_err": None,
    "flow": None, "flow_ts": 0, "flow_err": None,
}

SPOT_TTL = 3
VOL_TTL = 60
KALSHI_TTL = 15
FLOW_TTL = 5
FLOW_WINDOW_MS = 15 * 60 * 1000
FLOW_TILT_MAX = 0.18


def get_spot():
    now = time.time()
    with cache_lock:
        if CACHE["spot"] is not None and now - CACHE["spot_ts"] < SPOT_TTL:
            return CACHE["spot"]
    try:
        r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker", timeout=6)
        r.raise_for_status()
        price = float(r.json()["price"])
        with cache_lock:
            CACHE["spot"] = price
            CACHE["spot_ts"] = now
            CACHE["spot_err"] = None
        return price
    except Exception as e:
        with cache_lock:
            CACHE["spot_err"] = f"{type(e).__name__}: {e}"
        return CACHE["spot"]  # stale value if we have one, else None


def get_vol():
    now = time.time()
    with cache_lock:
        if CACHE["vol"] is not None and now - CACHE["vol_ts"] < VOL_TTL:
            return CACHE["vol"]
    try:
        r = requests.get(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=60",
            timeout=6,
        )
        r.raise_for_status()
        candles = r.json()
        closes = [c[4] for c in reversed(candles) if c[4] > 0]
        if len(closes) > 10:
            rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
            mean = sum(rets) / len(rets)
            var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
            annual = math.sqrt(var) * math.sqrt(525600)
            with cache_lock:
                CACHE["vol"] = annual
                CACHE["vol_ts"] = now
            return annual
    except Exception:
        pass
    with cache_lock:
        if CACHE["vol"] is None:
            CACHE["vol"] = 0.45  # sane fallback
        return CACHE["vol"]


def get_kalshi():
    now = time.time()
    with cache_lock:
        if now - CACHE["kalshi_ts"] < KALSHI_TTL and CACHE["kalshi"]["ticker"]:
            return CACHE["kalshi"]
    try:
        r = requests.get(
            "https://api.elections.kalshi.com/trade-api/v2/markets"
            "?series_ticker=KXBTC15M&status=open&limit=5",
            timeout=6,
        )
        r.raise_for_status()
        markets = r.json().get("markets", [])
        result = {"ticker": None, "yes_ask": None, "close_time": None}
        if markets:
            m = markets[0]
            result = {
                "ticker": m.get("ticker"),
                "yes_ask": (m["yes_ask"] / 100) if m.get("yes_ask") is not None else None,
                "close_time": m.get("close_time"),
            }
        with cache_lock:
            CACHE["kalshi"] = result
            CACHE["kalshi_ts"] = now
            CACHE["kalshi_err"] = None
        return result
    except Exception as e:
        with cache_lock:
            CACHE["kalshi_err"] = f"{type(e).__name__}: {e}"
        return CACHE["kalshi"]


def get_flow(whale_thresh):
    now = time.time()
    with cache_lock:
        if CACHE["flow"] is not None and now - CACHE["flow_ts"] < FLOW_TTL:
            trades = CACHE["flow"]
        else:
            trades = None
    if trades is None:
        try:
            end_ms = int(now * 1000)
            start_ms = end_ms - FLOW_WINDOW_MS
            r = requests.get(
                "https://api.binance.com/api/v3/aggTrades"
                f"?symbol=BTCUSDT&startTime={start_ms}&endTime={end_ms}&limit=1000",
                timeout=6,
            )
            r.raise_for_status()
            data = r.json()
            trades = [
                {"side": "sell" if t["m"] else "buy", "qty": float(t["q"])}
                for t in data
            ]
            with cache_lock:
                CACHE["flow"] = trades
                CACHE["flow_ts"] = now
                CACHE["flow_err"] = None
        except Exception as e:
            with cache_lock:
                CACHE["flow_err"] = f"{type(e).__name__}: {e}"
            trades = CACHE["flow"] or []

    buy_vol = sell_vol = whale_buy_vol = whale_sell_vol = 0.0
    whale_buy_count = whale_sell_count = 0
    for t in trades:
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

    return {
        "buy_vol": buy_vol, "sell_vol": sell_vol,
        "whale_buy_vol": whale_buy_vol, "whale_sell_vol": whale_sell_vol,
        "whale_buy_count": whale_buy_count, "whale_sell_count": whale_sell_count,
        "combined_signal": combined_signal,
    }


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def minutes_to_next_boundary():
    now = datetime.datetime.now(datetime.timezone.utc)
    mins = now.minute + now.second / 60 + now.microsecond / 60_000_000
    rem = 15 - (mins % 15)
    return 15.0 if rem <= 0.001 else rem


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    spot = get_spot()
    vol = get_vol()
    kalshi = get_kalshi()

    target_param = request.args.get("target")
    if target_param and target_param != "auto":
        try:
            target = float(target_param)
        except ValueError:
            target = spot
    else:
        target = spot

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
    flow = get_flow(whale_thresh)

    p_base = p_final = tilt = None
    if spot and target and vol and mins_left and mins_left > 0:
        T = mins_left / 525600.0
        try:
            d2 = (math.log(spot / target) - 0.5 * vol * vol * T) / (vol * math.sqrt(T))
            p_base = norm_cdf(d2)
            tilt = flow_weight * flow["combined_signal"] * FLOW_TILT_MAX
            p_final = min(0.995, max(0.005, p_base + tilt))
        except (ValueError, ZeroDivisionError):
            pass

    status = {
        "coinbase": "ok" if spot is not None else "error",
        "binance": "ok" if CACHE["flow_err"] is None else "error",
        "kalshi": "ok" if kalshi.get("ticker") else (CACHE["kalshi_err"] or "no open market found"),
    }

    print(f"api_state: spot={spot} vol={vol} kalshi_err={CACHE['kalshi_err']} spot_err={CACHE['spot_err']}", flush=True)

    return jsonify(
        {
            "spot": spot,
            "target": target,
            "annual_vol": vol,
            "mins_left": mins_left,
            "kalshi": kalshi,
            "status": status,
            "flow": flow,
            "prob": {"base": p_base, "tilt": tilt, "final": p_final},
        }
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
