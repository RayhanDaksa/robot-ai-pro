"""
ROBOT AI PRO — Webhook Server untuk Render.com
TradingView Webhook → Binance USDⓈ-M Futures

Flow:
  BUY/SELL → entry MARKET + SL + TP1 + TP2 + TP3 + TP4
  TP1 hit  → SL geser ke entry (breakeven)
  TP2 hit  → SL geser ke TP1
  TP3 hit  → SL geser ke TP2
  TP4 hit  → trade selesai
  SL hit   → bersihkan semua, reset state
"""

import os, time, hmac, hashlib, json
from urllib.parse import urlencode
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ───────── KONFIGURASI (isi via Environment Variables di Render) ─────────
API_KEY    = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
BASE_URL   = "https://fapi.binance.com"

SYMBOL     = os.environ.get("SYMBOL", "BTCUSDT")
RISK_USDT  = float(os.environ.get("RISK_USDT", "2.0"))
SL_MULT    = float(os.environ.get("SL_MULT", "1.5"))
RR1        = 1.0   # TP1
RR2        = 2.0   # TP2
RR3        = 3.0   # TP3
RR4        = 4.0   # TP4
LEVERAGE   = int(os.environ.get("LEVERAGE", "10"))

# Qty per TP (masing-masing 25%)
QTY_PCT = 0.25

# ───────── STATE (in-memory, reset kalau Render restart) ─────────
state = {
    "active":  False,
    "side":    "",
    "entry":   0.0,
    "sl":      0.0,
    "tp1":     0.0, "tp2": 0.0, "tp3": 0.0, "tp4": 0.0,
    "qty":     0.0,
    "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
}

# ───────── ROUTES ─────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ROBOT AI PRO running", "symbol": SYMBOL})

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        body   = request.get_json(force=True)
        action = (body.get("action") or "").upper()
        log(f"📩 Webhook: {body}")

        if action in ("BUY", "SELL"):
            handle_entry(action, body)
        elif action == "TP1":
            handle_tp_hit(1)
        elif action == "TP2":
            handle_tp_hit(2)
        elif action == "TP3":
            handle_tp_hit(3)
        elif action == "TP4":
            handle_tp4()
        elif action == "SL":
            handle_sl()
        else:
            log(f"⚠️ Action tidak dikenal: {action}")

        return jsonify({"ok": True})
    except Exception as e:
        log(f"❌ Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

# ───────── HANDLERS ─────────
def handle_entry(side, body):
    global state
    if state["active"]:
        log(f"🔄 Flip — tutup posisi {state['side']} lama")
        close_all_orders()
        close_position()
        reset_state()

    set_leverage(SYMBOL, LEVERAGE)

    close_px = float(body["close"]) if "close" in body else get_market_price(SYMBOL)
    atr      = float(body["atr"]) if "atr" in body else close_px * 0.003
    risk     = atr * SL_MULT
    d        = 1 if side == "BUY" else -1

    sl_px  = round(close_px - d * risk, 2)
    tp1_px = round(close_px + d * risk * RR1, 2)
    tp2_px = round(close_px + d * risk * RR2, 2)
    tp3_px = round(close_px + d * risk * RR3, 2)
    tp4_px = round(close_px + d * risk * RR4, 2)

    sl_dist   = abs(close_px - sl_px)
    qty_total = round(RISK_USDT / sl_dist, 3)
    q_each    = round(qty_total * QTY_PCT, 3)
    q_last    = round(qty_total - q_each * 3, 3)  # hindari rounding error

    opp = "SELL" if side == "BUY" else "BUY"

    log(f"📐 {side} | entry={close_px} SL={sl_px} TP1={tp1_px} TP4={tp4_px} qty={qty_total}")

    # 1. Market entry
    place_order(symbol=SYMBOL, side=side, type="MARKET", quantity=qty_total)

    # 2. SL
    place_order(symbol=SYMBOL, side=opp, type="STOP_MARKET",
                stopPrice=sl_px, quantity=qty_total,
                reduceOnly="true", timeInForce="GTE_GTC",
                newClientOrderId="ROBOT_SL")

    # 3–6. TP1–TP4
    for i, (tp_px, qty, cid) in enumerate([
        (tp1_px, q_each, "ROBOT_TP1"),
        (tp2_px, q_each, "ROBOT_TP2"),
        (tp3_px, q_each, "ROBOT_TP3"),
        (tp4_px, q_last, "ROBOT_TP4"),
    ], 1):
        place_order(symbol=SYMBOL, side=opp, type="TAKE_PROFIT_MARKET",
                    stopPrice=tp_px, quantity=qty,
                    reduceOnly="true", timeInForce="GTE_GTC",
                    newClientOrderId=cid)

    state.update({
        "active": True, "side": side,
        "entry": close_px, "sl": sl_px,
        "tp1": tp1_px, "tp2": tp2_px, "tp3": tp3_px, "tp4": tp4_px,
        "qty": qty_total,
        "tp1_hit": False, "tp2_hit": False, "tp3_hit": False,
    })
    log(f"✅ Entry {side} selesai qty={qty_total}")


def handle_tp_hit(tp_num):
    if not state["active"]:
        log(f"⚠️ TP{tp_num} alert tapi tidak ada posisi aktif"); return
    hit_key = f"tp{tp_num}_hit"
    if state[hit_key]:
        log(f"⚠️ TP{tp_num} sudah diproses"); return

    # SL baru: TP1→entry, TP2→TP1, TP3→TP2
    new_sl = state["entry"] if tp_num == 1 else state[f"tp{tp_num-1}"]
    log(f"🎯 TP{tp_num} hit! Geser SL ke {new_sl}")

    cancel_order("ROBOT_SL")
    cancel_order("ROBOT_SL_BE")
    for n in range(2, tp_num + 1):
        cancel_order(f"ROBOT_SL_T{n}")

    qty_sisa = round(state["qty"] * QTY_PCT * (4 - tp_num), 3)
    opp      = "SELL" if state["side"] == "BUY" else "BUY"
    sl_id    = "ROBOT_SL_BE" if tp_num == 1 else f"ROBOT_SL_T{tp_num}"

    place_order(symbol=SYMBOL, side=opp, type="STOP_MARKET",
                stopPrice=new_sl, quantity=qty_sisa,
                reduceOnly="true", timeInForce="GTE_GTC",
                newClientOrderId=sl_id)

    state[hit_key] = True
    state["sl"]    = new_sl
    log(f"✅ SL baru {new_sl}, qty sisa {qty_sisa}")


def handle_tp4():
    if not state["active"]:
        log("⚠️ TP4 alert tapi tidak ada posisi aktif"); return
    log("🏆 TP4 hit — trade selesai!")
    close_all_orders()
    reset_state()


def handle_sl():
    if not state["active"]:
        log("⚠️ SL alert tapi tidak ada posisi aktif"); return
    tipe = "BREAKEVEN" if state["tp1_hit"] else "LOSS"
    log(f"🛑 SL hit — {tipe}")
    close_all_orders()
    reset_state()

# ───────── BINANCE HELPERS ─────────
def place_order(**params):
    params["timestamp"]  = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs  = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}/fapi/v1/order?{qs}&signature={sig}"
    res = requests.post(url, headers={"X-MBX-APIKEY": API_KEY})
    data = res.json()
    if isinstance(data, dict) and data.get("code", 0) < 0:
        log(f"❌ Order error: {data}")
        raise Exception(f"Binance: {data.get('msg')}")
    log(f"📦 Order OK: {str(data)[:150]}")
    return data


def cancel_order(client_order_id):
    try:
        params = {"symbol": SYMBOL, "origClientOrderId": client_order_id,
                  "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        qs  = urlencode(params)
        sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE_URL}/fapi/v1/order?{qs}&signature={sig}"
        res = requests.delete(url, headers={"X-MBX-APIKEY": API_KEY})
        log(f"🗑️ Cancel {client_order_id}: {res.text[:80]}")
    except Exception as e:
        log(f"⚠️ Cancel {client_order_id} gagal: {e}")


def close_all_orders():
    try:
        params = {"symbol": SYMBOL, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        qs  = urlencode(params)
        sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{BASE_URL}/fapi/v1/allOpenOrders?{qs}&signature={sig}"
        requests.delete(url, headers={"X-MBX-APIKEY": API_KEY})
        log("🗑️ Semua open orders di-cancel")
    except Exception as e:
        log(f"⚠️ closeAllOrders error: {e}")


def close_position():
    if not state["qty"]: return
    try:
        opp = "SELL" if state["side"] == "BUY" else "BUY"
        place_order(symbol=SYMBOL, side=opp, type="MARKET",
                    quantity=state["qty"], reduceOnly="true")
        log("🔒 Posisi ditutup market")
    except Exception as e:
        log(f"⚠️ closePosition error: {e}")


def get_market_price(symbol):
    try:
        url = f"{BASE_URL}/fapi/v1/ticker/price?symbol={symbol}"
        res = requests.get(url)
        price = float(res.json()["price"])
        log(f"💰 Harga {symbol}: {price}")
        return price
    except Exception as e:
        log(f"⚠️ Gagal ambil harga: {e}")
        raise


def set_leverage(symbol, leverage):
    params = {"symbol": symbol, "leverage": leverage,
              "timestamp": int(time.time() * 1000), "recvWindow": 5000}
    qs  = urlencode(params)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{BASE_URL}/fapi/v1/leverage?{qs}&signature={sig}"
    requests.post(url, headers={"X-MBX-APIKEY": API_KEY})


def reset_state():
    global state
    state = {k: (False if isinstance(v, bool) else (0.0 if isinstance(v, float) else ""))
             for k, v in state.items()}
    state["active"] = False


def log(msg):
    print(msg, flush=True)

# ───────── MAIN ─────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
