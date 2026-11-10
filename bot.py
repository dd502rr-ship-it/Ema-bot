"""
EMA100/EMA200 + liquidity inflow + pre-breakout squeeze alerts.
GitHub Actions version: one scan per run. Secrets come from environment.
"""
import os
import json
import time
import logging
import requests
from statistics import mean, pstdev
from concurrent.futures import ThreadPoolExecutor

TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["TG_CHAT_ID"]
MANUAL = os.environ.get("EVENT") == "workflow_dispatch"

# markets: spot + USDT-M futures (Binance)
# note: futures API (fapi) is blocked for US servers, bot reports it and keeps scanning spot
MARKETS = {
    "spot": {
        "label": "\u0633\u0628\u0648\u062a",
        "bases": ["https://data-api.binance.vision",
                  "https://api.binance.com",
                  "https://api.binance.us"],
        "prefix": "/api/v3",
        "workers": 6,
        "m15_limit": 100,
    },
    "futures": {
        "label": "\u0639\u0642\u0648\u062f",
        "bases": ["https://fapi.binance.com"],
        "prefix": "/fapi/v1",
        "workers": 3,
        "m15_limit": 99,
    },
    "alpha": {
        "label": "\u0623\u0644\u0641\u0627",
        "bases": ["https://www.binance.com"],
        "prefix": "/bapi/defi/v1/public",
        "workers": 5,
        "m15_limit": 100,
    },
}

# ---- scan scope ----
MIN_QUOTE_VOL_24H = 300_000     # skip dead coins (24h volume in USDT)
ALPHA_MIN_VOL_24H = 100_000     # Binance Alpha tokens: min 24h volume in USD
ALPHA_MAX = 150                 # cap for alpha tokens (keeps run time short)
MAX_SYMBOLS = 300               # safety cap per market

# ---- EMA alerts ----
EMA_PERIODS = (100, 200)
TOUCH_PCT = 0.3                 # "reached EMA" if within 0.3 percent
# interval -> (label, cooldown seconds)
TF = {
    "1h": ("\u0633\u0627\u0639\u0629", 2 * 3600),
    "4h": ("4 \u0633\u0627\u0639\u0627\u062a", 4 * 3600),
    "1d": ("\u064a\u0648\u0645\u064a", 12 * 3600),
}

# ---- liquidity inflow (15m candle) ----
LIQ_SPIKE = 4.0                 # last 15m volume >= 4x average of prior 24h
LIQ_BUY_RATIO = 0.60            # aggressive buying share >= 60 percent
LIQ_MIN_CANDLE_USD = 50_000     # ignore tiny volumes
LIQ_COOLDOWN = 3 * 3600

# ---- pre-breakout squeeze ----
SQ_LOOKBACK = 120               # candles used to rank the current band width
SQ_RANK = 0.10                  # narrowest 10 percent of the lookback
SQ_VOL_RATIO = 1.5              # recent volume >= 1.5x normal
SQ_TFS = ("1h", "4h")
SQ_COOLDOWN = {"1h": 6 * 3600, "4h": 12 * 3600}

STATE_FILE = "state.json"
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "EUR", "AEUR", "USD1", "XUSD", "USDE", "PYUSD"}
LEVERAGED = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

state = {}
_good_base = {}
DISPLAY = {}                    # (market, api symbol) -> name shown in alerts
SPOT_BASES = set()              # coins already scanned on spot (skip on alpha)


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


def flush(alerts):
    """Send all alerts of this run in a few combined messages."""
    alerts.sort(key=lambda a: a[0])
    chunk = ""
    for _, text in alerts:
        if len(chunk) + len(text) + 2 > 3500:
            send(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + text
    if chunk:
        send(chunk)


def can_alert(*key, cooldown):
    k = "|".join(str(x) for x in key)
    now = time.time()
    if now - state.get(k, 0) < cooldown:
        return False
    state[k] = now
    return True


def api(m, path, params=None):
    bases = MARKETS[m]["bases"][:]
    good = _good_base.get(m)
    if good in bases:
        bases.remove(good)
        bases.insert(0, good)
    last = None
    for base in bases:
        try:
            r = requests.get(base + path, params=params, timeout=20)
            if r.status_code == 200:
                _good_base[m] = base
                return r.json()
            if r.status_code in (418, 429):
                time.sleep(3)
            last = f"{base} -> HTTP {r.status_code}"
        except Exception as e:
            last = f"{base} -> {e}"
    raise RuntimeError(last)


def get_alpha_symbols():
    res = api("alpha", "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list")
    pairs = []
    for t in res.get("data") or []:
        aid = t.get("alphaId")
        name = (t.get("symbol") or "").upper()
        if not aid or not name:
            continue
        if t.get("offline") or t.get("fullyDelisted"):
            continue
        if name in SPOT_BASES or name in STABLES:
            continue
        try:
            qv = float(t.get("volume24h") or 0)
        except ValueError:
            continue
        if qv < ALPHA_MIN_VOL_24H:
            continue
        sym = aid + "USDT"
        DISPLAY[("alpha", sym)] = name + "USDT"
        pairs.append((sym, qv))
    pairs.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pairs[:ALPHA_MAX]]


def get_symbols(m):
    if m == "alpha":
        return get_alpha_symbols()
    data = api(m, MARKETS[m]["prefix"] + "/ticker/24hr")
    pairs = []
    for t in data:
        s = t["symbol"]
        if not s.endswith("USDT") or s.endswith(LEVERAGED):
            continue
        if s[:-4] in STABLES:
            continue
        qv = float(t["quoteVolume"])
        if qv < MIN_QUOTE_VOL_24H:
            continue
        pairs.append((s, qv))
    pairs.sort(key=lambda x: x[1], reverse=True)
    if m == "spot":
        SPOT_BASES.update(s[:-4] for s, _ in pairs)
    return [s for s, _ in pairs[:MAX_SYMBOLS]]


def klines(m, symbol, interval, limit):
    if m == "alpha":
        res = api(m, "/bapi/defi/v1/public/alpha-trade/klines",
                  {"symbol": symbol, "interval": interval, "limit": limit})
        data = res.get("data") if isinstance(res, dict) else None
        if not data:
            raise RuntimeError("alpha klines empty")
        return data
    return api(m, MARKETS[m]["prefix"] + "/klines",
               {"symbol": symbol, "interval": interval, "limit": limit})


def fetch_symbol(m, s):
    d = {}
    for tf, limit in [("15m", MARKETS[m]["m15_limit"])] + [(t, 400) for t in TF]:
        try:
            d[tf] = klines(m, s, tf, limit)
        except Exception as e:
            logging.warning("%s %s %s fetch error: %s", m, s, tf, e)
    return s, (d or None)


def ema_series(values, period):
    """EMA seeded with SMA of the first period values (like TradingView)."""
    k = 2 / (period + 1)
    e = mean(values[:period])
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def check_ema(m, symbol, interval, k, alerts):
    label, cooldown = TF[interval]
    mk = MARKETS[m]["label"]
    closes = [float(c[4]) for c in k]
    if len(closes) < max(EMA_PERIODS) + 2:
        return
    price, prev = closes[-1], closes[-2]
    hi, lo = float(k[-1][2]), float(k[-1][3])   # current candle high/low (wick)
    candle = k[-1][0]                            # current candle open time
    tf = "(" + label + ")"

    for p in EMA_PERIODS:
        series = ema_series(closes, p)
        ema, ema_prev = series[-1], series[-2]
        dist = (price - ema) / ema * 100

        if prev < ema_prev <= price or prev > ema_prev >= price:
            kind = "\u0627\u062e\u062a\u0631\u0642 \u0644\u0644\u0623\u0639\u0644\u0649 \u2705" if price > ema else "\u0643\u0633\u0631 \u0644\u0644\u0623\u0633\u0641\u0644 \u274c"
            if can_alert(m, symbol, interval, p, "cross", cooldown=cooldown):
                alerts.append((2, f"\U0001f4ca <b>{symbol}</b> [{mk}] {kind} EMA{p} {tf}\n"
                                  f"\u0627\u0644\u0633\u0639\u0631: {price:g} | EMA{p}: {ema:.6g}"))
        elif lo <= ema <= hi or abs(dist) <= TOUCH_PCT:
            # touched the line (wick or body) in the current candle: one alert per candle
            if can_alert(m, symbol, interval, p, "touch", candle, cooldown=24 * 3600):
                alerts.append((3, f"\U0001f3af <b>{symbol}</b> [{mk}] \u0648\u0635\u0644 \u0625\u0644\u0649 EMA{p} {tf}\n"
                                  f"\u0627\u0644\u0633\u0639\u0631: {price:g} | EMA{p}: {ema:.6g} | \u0627\u0644\u0628\u0639\u062f: {dist:+.2f}%"))


def check_liquidity(m, symbol, k, alerts):
    mk = MARKETS[m]["label"]
    if len(k) < 30:
        return
    last = k[-2]                       # last closed 15m candle
    base = [float(c[7]) for c in k[-98:-2]]
    vol = float(last[7])
    avg = mean(base)
    if avg <= 0 or vol < LIQ_MIN_CANDLE_USD:
        return
    ratio = vol / avg
    buy = float(last[10]) / vol
    change = (float(last[4]) / float(last[1]) - 1) * 100

    if ratio >= LIQ_SPIKE and buy >= LIQ_BUY_RATIO and change > 0:
        if can_alert(m, symbol, "liq", cooldown=LIQ_COOLDOWN):
            alerts.append((0, f"\U0001f4b0 <b>{symbol}</b> [{mk}] \u062f\u062e\u0648\u0644 \u0633\u064a\u0648\u0644\u0629!\n"
                              f"\u062d\u062c\u0645 \u0622\u062e\u0631 15 \u062f\u0642\u064a\u0642\u0629: {vol:,.0f}$ ({ratio:.1f}x \u0627\u0644\u0645\u0639\u062a\u0627\u062f)\n"
                              f"\u0646\u0633\u0628\u0629 \u0627\u0644\u0634\u0631\u0627\u0621: {buy*100:.0f}% | \u062a\u063a\u064a\u0631 \u0627\u0644\u0634\u0645\u0639\u0629: {change:+.2f}%"))


def bb_width(seg):
    m = mean(seg)
    if m == 0:
        return None
    return 4 * pstdev(seg) / m          # (upper - lower) / middle with 2 std


def check_squeeze(m, symbol, interval, k, alerts):
    mk = MARKETS[m]["label"]
    k = k[:-1]                          # closed candles only
    closes = [float(c[4]) for c in k]
    vols = [float(c[7]) for c in k]
    if len(closes) < SQ_LOOKBACK + 30:
        return
    widths = []
    for i in range(len(closes) - SQ_LOOKBACK, len(closes)):
        w = bb_width(closes[i - 19:i + 1])
        if w is None:
            return
        widths.append(w)
    rank = sum(1 for w in widths if w <= widths[-1]) / len(widths)
    base = mean(vols[-27:-3])
    if base <= 0:
        return
    vratio = mean(vols[-3:]) / base

    if rank <= SQ_RANK and vratio >= SQ_VOL_RATIO:
        ema200 = ema_series(closes, 200)[-1] if len(closes) >= 202 else None
        if ema200 is None:
            pos = ""
        elif closes[-1] >= ema200:
            pos = "\n\u0627\u0644\u0633\u0639\u0631 \u0641\u0648\u0642 EMA200"
        else:
            pos = "\n\u0627\u0644\u0633\u0639\u0631 \u062a\u062d\u062a EMA200"
        label = TF[interval][0]
        if can_alert(m, symbol, interval, "squeeze", cooldown=SQ_COOLDOWN[interval]):
            alerts.append((1, f"\u26a1 <b>{symbol}</b> [{mk}] \u0627\u062d\u062a\u0645\u0627\u0644 \u0627\u0646\u0641\u062c\u0627\u0631 \u0633\u0639\u0631\u064a (\u0641\u0631\u064a\u0645 {label})\n"
                              f"\u0627\u0646\u0636\u063a\u0627\u0637 \u0627\u0644\u062a\u0630\u0628\u0630\u0628: \u0636\u0645\u0646 \u0623\u0636\u064a\u0642 {rank*100:.0f}% \u0645\u0646 \u0622\u062e\u0631 {SQ_LOOKBACK} \u0634\u0645\u0639\u0629\n"
                              f"\u0627\u0644\u062d\u062c\u0645 \u064a\u0631\u062a\u0641\u0639: {vratio:.1f}x \u0627\u0644\u0645\u0639\u062a\u0627\u062f{pos}"))


def main():
    load_state()
    if MANUAL:
        send("\u2705 \u0627\u0644\u0628\u0648\u062a \u064a\u0639\u0645\u0644 (\u062a\u0634\u063a\u064a\u0644 \u064a\u062f\u0648\u064a)")

    alerts = []
    total = 0
    for m, cfg in MARKETS.items():
        try:
            symbols = get_symbols(m)
        except Exception as e:
            logging.warning("%s symbols error: %s", m, e)
            if can_alert("market_fail", m, cooldown=6 * 3600):
                if "451" in str(e):
                    send("\u26a0\ufe0f \u0628\u064a\u0646\u0627\u0646\u0633 \u062a\u062d\u062c\u0628 \u0633\u064a\u0631\u0641\u0631\u0627\u062a GitHub \u0627\u0644\u0623\u0645\u0631\u064a\u0643\u064a\u0629 \u0639\u0646 \u0633\u0648\u0642 (" + cfg["label"] + ")\u060c \u0648\u062a\u0645 \u062a\u062e\u0637\u064a\u0647.")
                else:
                    send("\u26a0\ufe0f \u062a\u0639\u0630\u0631 \u062c\u0644\u0628 \u0628\u064a\u0627\u0646\u0627\u062a \u0633\u0648\u0642 (" + cfg["label"] + ").")
            continue

        with ThreadPoolExecutor(cfg["workers"]) as ex:
            results = list(ex.map(lambda sym: fetch_symbol(m, sym), symbols))

        for s, d in results:
            if not d:
                continue
            name = DISPLAY.get((m, s), s)
            try:
                if "15m" in d:
                    check_liquidity(m, name, d["15m"], alerts)
                for tf in TF:
                    if tf in d:
                        check_ema(m, name, tf, d[tf], alerts)
                for tf in SQ_TFS:
                    if tf in d:
                        check_squeeze(m, name, tf, d[tf], alerts)
            except Exception as e:
                logging.warning("%s %s error: %s", m, s, e)
        logging.info("%s: %d symbols", m, len(symbols))
        total += len(symbols)

    flush(alerts)
    save_state()
    logging.info("scanned %d symbols, %d alerts", total, len(alerts))


if __name__ == "__main__":
    main()
