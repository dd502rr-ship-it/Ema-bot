import os
import ccxt
import pandas as pd
import requests

# قراءة معلومات البوت من الأسرار المحفوظة في المستودع
TELEGRAM_BOT_TOKEN = os.getenv("TG_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TG_CHAT_ID")

# الأطر الزمنية المفحوصة (ساعة، 4 ساعات، يومي)
TIMEFRAMES = ['1h', '4h', '1d']

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"خطأ في إرسال التنبيه: {e}")

def check_market():
    print("جاري بدء المسح الشامل للأسواق (Spot & Futures)...")
    
    # 1. فحص سوق السبوت (Spot)
    try:
        spot_exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
        scan_market_type(spot_exchange, market_type='Spot')
    except Exception as e:
        print(f"خطأ في سوق السبوت: {e}")

    # 2. فحص سوق العقود الآجلة (Futures)
    try:
        futures_exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})
        scan_market_type(futures_exchange, market_type='Futures (عقود آجلة)')
    except Exception as e:
        print(f"خطأ في سوق الفيوتشر: {e}")

def scan_market_type(client, market_type):
    try:
        client.load_markets()
        symbols = [
            symbol for symbol in client.symbols 
            if symbol.endswith('/USDT') and 'UP/' not in symbol and 'DOWN/' not in symbol and ':USDT' not in symbol
        ]
        
        print(f"تم العثور على {len(symbols)} زوج في سوق الـ {market_type}")

        for symbol in symbols:
            for tf in TIMEFRAMES:
                try:
                    ohlcv = client.fetch_ohlcv(symbol, tf, limit=250)
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                    # حساب المتوسطات المتحركة الأسية EMA 100 و EMA 200
                    df['ema_100'] = df['close'].ewm(span=100, adjust=False).mean()
                    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()

                    # حساب السيولة وحجم التداول (آخر 20 شمعة)
                    df['vol_sma'] = df['volume'].rolling(window=20).mean()

                    current_price = df['close'].iloc[-1]
                    ema_100 = df['ema_100'].iloc[-1]
                    ema_200 = df['ema_200'].iloc[-1]
                    
                    current_vol = df['volume'].iloc[-1]
                    avg_vol = df['vol_sma'].iloc[-1]

                    tolerance = 0.003  # نسبة السماح (0.3%)
                    
                    # فحص دخول سيولة ضخمة
                    is_high_volume = current_vol > (avg_vol * 1.5)
                    vol_status = "🚨 **دخول سيولة ضخمة جداً! (شراء مكثف)** 💰" if is_high_volume else "📊 سيولة طبيعية"

                    msg = ""

                    # رصد ملامسة EMA 100
                    if abs(current_price - ema_100) / ema_100 <= tolerance:
                        msg = (
                            f"🎯 **تنبيه ملامسة EMA 100 ({tf})**\n"
                            f"📌 السوق: `{market_type}`\n"
                            f"🪙 العملة: `{symbol}`\n"
                            f"📈 السعر الحالي: `{current_price}`\n"
                            f"🔹 قيمة EMA 100: `{ema_100:.4f}`\n"
                            f"{vol_status}"
                        )
                    
                    # رصد ملامسة EMA 200 (انفجار سعري متوقع)
                    elif abs(current_price - ema_200) / ema_200 <= tolerance:
                        msg = (
                            f"🚀 **تنبيه انفجار سعري متوقع! ({tf})**\n"
                            f"📌 السوق: `{market_type}`\n"
                            f"🪙 العملة: `{symbol}`\n"
                            f"🔥 السعر الحالي: `{current_price}`\n"
                            f"🔸 قيمة EMA 200: `{ema_200:.4f}`\n"
                            f"{vol_status}\n"
                            f"⚡ **تحذير: ملامسة تاريخية لخط EMA 200 مع تدفق سيولة!**"
                        )

                    if msg:
                        send_telegram_message(msg)
                        print(f"تم إرسال تنبيه [{market_type}] للعملة {symbol} على إطار {tf}")

                except Exception as e:
                    continue

    except Exception as e:
        print(f"خطأ في معالجة سوق {market_type}: {e}")

if __name__ == "__main__":
    check_market()
