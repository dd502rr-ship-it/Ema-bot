import os
import ccxt
import pandas as pd
import requests
import time

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
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"خطأ في إرسال التنبيه: {e}")

def check_market():
    try:
        exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        exchange.load_markets()
        
        # جلب جميع أزواج الـ USDT النشطة
        symbols = [s for s in exchange.symbols if '/USDT' in s]
        
        for symbol in symbols:
            for tf in TIMEFRAMES:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=250)
                    if len(ohlcv) < 200:
                        continue
                        
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    # حساب مؤشر EMA 200
                    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()
                    
                    last_row = df.iloc[-1]
                    low_price = last_row['low']
                    high_price = last_row['high']
                    ema_200 = last_row['ema_200']
                    close_price = last_row['close']
                    
                    # شرط ملامسة خط الـ EMA 200 (إذا كان السعر الأدنى أقل أو يساوي والأسفل أعلى أو يساوي)
                    if low_price <= ema_200 <= high_price:
                        msg = (
                            f"🚨 **تنبيه ملامسة EMA 200!**\n\n"
                            f"🪙 العملة: `{symbol}`\n"
                            f"⏱ الإطار الزمني: `{tf}`\n"
                            f"📉 قيمة EMA 200: `{ema_200:.4f}`\n"
                            f"📍 سعر الإغلاق الحالي: `{close_price:.4f}`\n"
                            f"💡 *تم رصد ملامسة السعر للخط مباشرة دون انتظار الإغلاق!*"
                        )
                        send_telegram_message(msg)
                        time.sleep(1) # لتجنب الضغط على واجهة البرمجة
                except Exception as ex:
                    # تخطي أي خطأ مؤقت في زوج معين
                    continue
                    
    except Exception as e:
        print(f"خطأ عام في جلب أسواق بايننس: {e}")

if __name__ == "__main__":
    check_market()
