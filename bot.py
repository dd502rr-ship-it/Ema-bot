"""
بوت تنبيهات EMA100 / EMA200 + دخول السيولة (نسخة GitHub Actions)
كل تشغيل = فحص واحد ثم يتوقف. التوكن يُقرأ من GitHub Secrets (لا تكتبه هنا).
"""
import os
import json
import time
import logging
import requests
from statistics import mean

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
MANUAL = os.environ.get("EVENT") == "workflow_dispatch"

BASES = [
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api.binance.us",
]

TOP_N = 100
EMA_PERIODS = (100, 200)
TOUCH_PCT = 0.5
VOL_SPIKE = 3.0
BUY_RATIO = 0.60
COOLDOWN = 6 * 3600

STATE_FILE = "state.json"
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "USD1", "XUSD"}
LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

state = {}
_good_base = [None]


def load_state():
    global state
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}


def save_state():
    cutoff = time.time() - 24 * 3600
    pruned = {k: v for k, v in state.items() if v > cutoff}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(pruned, f)


def send(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.status_code != 200:
            logging.warning("Telegram %s: %s", r.status_code, r.text[:200])
    except Exception as e:
        logging.warning("Telegram error: %s", e)


def can_alert(*key):
    k = "|".join(str(x) for x in key)
    now = time.time()
    if now - state.get(k, 0) < COOLDOWN:
        return False
    state[k] = now
    return True


def api(path, params=None):
    bases = BASES[:]
    if _good_base[0] in bases:
        bases.remove(_good_base[0])
        bases.insert(0, _good_base[0])
    last = None
    for base in bases:
        try:
            r = requests.get(base + path, params=params, timeout=20)
            if r.status_code == 200:
                _good_base[0] = base
                return r.json()
            last = f"{base} -> HTTP {r.status_code}"
        except Exception as e:
            last = f"{base} -> {e}"
    raise RuntimeError(last)


def get_symbols():
    data = api("/api/v3/ticker/24hr")
    pairs = []
    for t in data:
        s = t["symbol"]
        if not s.endswith("USDT") or s.endswith(LEVERAGED):
            continue
        if s[:-4] in STABLES:
            continue
        pairs.append((s, float(t["quoteVolume"])))
    pairs.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pairs[:TOP_N]]


def klines(symbol, interval, limit):
    return api("/api/v3/klines",
               {"symbol": symbol, "interval": interval, "limit": limit})


def ema_series(values, period):
    k = 2 / (period + 1)
    e = mean(values[:period])
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def check_ema(symbol):
    k = klines(symbol, "1d", 600)
    closes = [float(c[4]) for c in k]
    if len(closes) < max(EMA_PERIODS) + 2:
        return
    price, prev = closes[-1], closes[-2]

    for p in EMA_PERIODS:
        series = ema_series(closes, p)
        ema, ema_prev = series[-1], series[-2]
        dist = (price - ema) / ema * 100

        if prev < ema_prev <= price or prev > ema_prev >= price:
            kind = "اخترق للأعلى ✅" if price > ema else "كسر للأسفل ❌"
            if can_alert(symbol, p, "cross"):
                send(f"📊 <b>{symbol}</b> {kind} EMA{p} (يومي)\n"
                     f"السعر: {price:g} | EMA{p}: {ema:.6g}")
        elif abs(dist) <= TOUCH_PCT:
            if can_alert(symbol, p, "touch"):
                send(f"🎯 <b>{symbol}</b> وصل إلى EMA{p} (يومي)\n"
                     f"السعر: {price:g} | EMA{p}: {ema:.6g} | البعد: {dist:+.2f}%")


def check_liquidity(symbol):
    k = klines(symbol, "1h", 27)
    if len(k) < 27:
        return
    last = k[-2]
    base = [float(c[7]) for c in k[-26:-2]]
    vol = float(last[7])
    avg = mean(base)
    if avg <= 0 or vol <= 0:
        return
    ratio = vol / avg
    buy = float(last[10]) / vol
    change = (float(last[4]) / float(last[1]) - 1) * 100

    if ratio >= VOL_SPIKE and buy >= BUY_RATIO and change > 0:
        if can_alert(symbol, "liq"):
            send(f"💰 <b>{symbol}
