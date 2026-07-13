import os
import json
import time
import requests
import threading
import base64
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
PORT = int(os.environ.get("PORT", 8080))

PAIRS = ["USD/JPY", "AUD/USD"]
TIMEFRAMES = ["15min", "1h", "4h"]
OPPORTUNITIES_FILE = "opportunities.json"

PAIR_CURRENCIES = {
    "USD/JPY": ["USD", "JPY"],
    "AUD/USD": ["AUD", "USD"],
}

# حالة التريدات المنتظرة للتأكيد — dict بالـ pair كـ key
pending_trades = {}        # {"USD/JPY": trade_dict, ...}
waiting_confirmation = {}  # {"USD/JPY": True/False, ...}


# Cache ديال البيانات باش ما نطلبوش أكثر من مرة
data_cache = {}

def fetch_all_data():
    """كيجيب بيانات كل الأزواج مرة واحدة ويحفظها فالـ cache"""
    global data_cache
    data_cache = {}
    for pair in PAIRS:
        data_cache[pair] = {}
        for tf in ["15min", "1h", "4h"]:
            result = get_price_data(pair, tf)
            data_cache[pair][tf] = result

def get_cached_data(pair, interval):
    """كيرجع البيانات من الـ cache"""
    return data_cache.get(pair, {}).get(interval, None)

def send_telegram(msg, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload)

def send_with_buttons(msg, trade):
    pair_key = trade["pair"].replace("/", "")  # "USD/JPY" → "USDJPY"
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ نعم، دخلها!", "callback_data": f"yes_{pair_key}"},
            {"text": "❌ لا، تجاوزها", "callback_data": f"no_{pair_key}"}
        ]]
    }
    send_telegram(msg, reply_markup=keyboard)

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id})

def set_webhook():
    # امسح الـ webhook القديم أولاً
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
    time.sleep(2)
    # سجل الجديد
    webhook_url = "https://forex-trading-bot-2-production.up.railway.app/webhook"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    r = requests.post(url, json={"url": webhook_url})
    print(f"Webhook set: {r.json()}")

def get_high_impact_news(pair):
    try:
        currencies = PAIR_CURRENCIES.get(pair, [])
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        danger_events = []
        warning_events = []
        for event in events:
            if event.get("impact") != "High":
                continue
            if event.get("currency") not in currencies:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            diff_minutes = (event_time - now).total_seconds() / 60
            if -30 <= diff_minutes <= 120:
                danger_events.append(event["title"])
            elif 120 < diff_minutes <= 480:
                warning_events.append(event["title"])
        return danger_events, warning_events
    except:
        return [], []

def get_market_summary(pair):
    """كيجيب ملخص تحركات السوق ديال اليوم"""
    try:
        result_1h = get_cached_data(pair, "1h") or get_price_data(pair, "1h", 24)
        result_15 = get_cached_data(pair, "15min") or get_price_data(pair, "15min", 8)
        if not result_1h or not result_15:
            return None

        closes_1h = result_1h[0]
        closes_15 = result_15[0]

        # تحرك اليوم
        open_price = closes_1h[0]
        current = closes_1h[-1]
        change = round(current - open_price, 6)
        change_pct = round((change / open_price) * 100, 3)
        direction_emoji = "📈" if change > 0 else "📉"

        # أعلى وأدنى اليوم
        highs_1h = result_1h[1]
        lows_1h = result_1h[2]
        high_day = round(max(highs_1h), 6)
        low_day = round(min(lows_1h), 6)

        # تحرك آخر ساعة
        last_hour_change = round(closes_15[-1] - closes_15[0], 6)
        last_hour_emoji = "⬆️" if last_hour_change > 0 else "⬇️"

        return {
            "change": change,
            "change_pct": change_pct,
            "direction_emoji": direction_emoji,
            "high_day": high_day,
            "low_day": low_day,
            "last_hour_change": last_hour_change,
            "last_hour_emoji": last_hour_emoji,
            "current": current
        }
    except:
        return None

def get_news_summary(pair):
    """كيجيب ملخص الأخبار ديال اليوم"""
    try:
        currencies = PAIR_CURRENCIES.get(pair, [])
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        today_news = []
        for event in events:
            if event.get("impact") not in ["High", "Medium"]:
                continue
            if event.get("currency") not in currencies:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            if event_time.strftime("%Y-%m-%d") == today:
                impact_emoji = "🔴" if event.get("impact") == "High" else "🟡"
                diff = (event_time - now).total_seconds() / 60
                if diff < -60:
                    status = "مرات"
                elif diff < 0:
                    status = "داز دابا"
                else:
                    status = f"بعد {int(diff)} دقيقة"
                today_news.append(f"{impact_emoji} {event['title']} ({status})")
        return today_news
    except:
        return []
price_cache = {}

CACHE_SECONDS = {
    "15min": 900,
    "1h": 3600,
    "4h": 14400
}

def get_price_data(pair, interval="15min", outputsize=250):
    global price_cache

    cache_key = f"{pair}_{interval}"
    now_ts = time.time()

    if cache_key in price_cache:
        cached_time = price_cache[cache_key]["time"]

        if now_ts - cached_time < CACHE_SECONDS.get(interval, 900):
            return price_cache[cache_key]["data"]

    params = {
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY
    }

    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params=params,
            timeout=15
        )

        data = r.json()

        if "values" not in data:
            print(
                f"API Error {pair} {interval}: "
                f"{data.get('message', data.get('code', 'unknown'))}"
            )
            return None

        closes = [float(v["close"]) for v in reversed(data["values"])]
        highs = [float(v["high"]) for v in reversed(data["values"])]
        lows = [float(v["low"]) for v in reversed(data["values"])]

        result = (closes, highs, lows)

        price_cache[cache_key] = {
            "time": now_ts,
            "data": result
        }

        return result

    except Exception as e:
        print(f"Price API Error {pair} {interval}: {e}")
        return None

def calc_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calc_macd(closes):
    def ema(data, period):
        k = 2 / (period + 1)
        result = [data[0]]
        for v in data[1:]:
            result.append(v * k + result[-1] * (1 - k))
        return result
    if len(closes) < 26:
        return None, None
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
    signal = ema(macd_line, 9)
    return round(macd_line[-1], 6), round(signal[-1], 6)

def calc_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return round(sum(trs[-period:]) / period, 6)
def calc_ema(prices, period=200):
    if len(prices) < period:
        return None

    ema = sum(prices[:period]) / period
    multiplier = 2 / (period + 1)

    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def get_trend_structure(closes):
    if len(closes) < 20:
        return None

    recent = closes[-10:]
    older = closes[-20:-10]

    if max(recent) > max(older) and min(recent) > min(older):
        return "UP"

    if max(recent) < max(older) and min(recent) < min(older):
        return "DOWN"

    return "SIDEWAYS"


def get_support_resistance(highs, lows):
    support = min(lows[-20:])
    resistance = max(highs[-20:])
    return support, resistance
    
def analyze_timeframe(pair, interval):
    result = get_cached_data(pair, interval) or get_price_data(pair, interval)

    if not result:
        return None

    closes, highs, lows = result

    rsi = calc_rsi(closes)
    macd, signal = calc_macd(closes)
    atr = calc_atr(highs, lows, closes)

    if rsi is None or macd is None or atr is None:
        return None

    current_price = closes[-1]

    # EMA200
    ema200 = calc_ema(closes, 200)

    if ema200 is None:
        return None

    # Trend Structure
    trend = get_trend_structure(closes)

    # Support / Resistance
    support, resistance = get_support_resistance(highs, lows)
    distance_to_resistance = abs(resistance - current_price)
    distance_to_support    = abs(current_price - support)
    sr_threshold = round(atr * 1.2, 6)
    macd_diff = round(macd - signal, 6)

    # BUY checks — RSI اختياري دايما True، MACD+EMA200+Trend إلزاميين
    buy_checks = {
        "RSI":    True,
        "MACD":   macd > signal,
        "EMA200": current_price > ema200,
        "Trend":  trend == "UP",
    }
    buy_score = sum(buy_checks.values())
    buy_sr_ok = distance_to_resistance > sr_threshold   # فلتر حماية — ماشي فالـ score
    buy_ready = buy_score == 4 and buy_sr_ok

    # SELL checks
    sell_checks = {
        "RSI":    True,
        "MACD":   macd < signal,
        "EMA200": current_price < ema200,
        "Trend":  trend == "DOWN",
    }
    sell_score = sum(sell_checks.values())
    sell_sr_ok = distance_to_support > sr_threshold     # فلتر حماية — ماشي فالـ score
    sell_ready = sell_score == 4 and sell_sr_ok

    base = {
        "rsi": rsi, "atr": atr,
        "price": current_price, "ema200": ema200, "trend": trend,
        "macd": macd, "signal": signal, "macd_diff": macd_diff,
        "support": support, "resistance": resistance,
        "distance_to_support": distance_to_support,
        "distance_to_resistance": distance_to_resistance,
        "sr_threshold": sr_threshold,
        "buy_sr_ok": buy_sr_ok, "sell_sr_ok": sell_sr_ok,
        "buy_checks": buy_checks, "buy_score": buy_score,
        "sell_checks": sell_checks, "sell_score": sell_score,
    }

    if buy_ready:
        return {**base, "direction": "BUY"}
    elif sell_ready:
        return {**base, "direction": "SELL"}
    return {**base, "direction": None}
    
def analyze_pair(pair):
    results = {}
    for tf in TIMEFRAMES:
        res = analyze_timeframe(pair, tf)
        if res and res["direction"] is not None:
            results[tf] = res
    if len(results) < 2:
        return None
    directions = [r["direction"] for r in results.values()]
    if directions.count("BUY") >= 2:
        direction = "BUY 📈"
    elif directions.count("SELL") >= 2:
        direction = "SELL 📉"
    else:
        return None
    confirmed_tfs = [tf for tf, r in results.items() if r["direction"] in direction]
    main = list(results.values())[0]
    price = main["price"]
    atr = main["atr"]
    if "BUY" in direction:
        is_jpy = pair.endswith("JPY") or pair.startswith("JPY")
        min_tp = 1.80 if is_jpy else 0.00180
        max_tp = 2.20 if is_jpy else 0.00220
        tp_distance = min(atr * 1.5, max_tp)
        sl_distance = tp_distance / 1.5
        rr = round(tp_distance / sl_distance, 2)
        if is_jpy:
            tp = round(price + tp_distance, 3)
            sl = round(price - sl_distance, 3)
        else:
            tp = round(price + tp_distance, 5)
            sl = round(price - sl_distance, 5)
    else:
        is_jpy = pair.endswith("JPY") or pair.startswith("JPY")
        min_tp = 1.80 if is_jpy else 0.00180
        max_tp = 2.20 if is_jpy else 0.00220
        tp_distance = min(atr * 1.5, max_tp)
        sl_distance = tp_distance / 1.5
        rr = round(tp_distance / sl_distance, 2)
        if is_jpy:
            tp = round(price - tp_distance, 3)
            sl = round(price + sl_distance, 3)
        else:
            tp = round(price - tp_distance, 5)
            sl = round(price + sl_distance, 5)
    return {
        "pair": pair,
        "direction": direction,
        "price": price,
        "tp": tp,
        "sl": sl,
        "rr": rr,
        "strength": len(confirmed_tfs),
        "confirmed_tfs": confirmed_tfs,
        "details": results
    }

def get_strength_label(strength):
    if strength == 3:
        return "⭐⭐⭐ قوية جداً"
    elif strength == 2:
        return "⭐⭐ متوسطة"
    return "⭐ ضعيفة"


def check_pre_signal(pair, rsi_15):
    """كيشوف واش RSI ديال 15min + 1h كيقتربو من منطقة الإشارة"""
    # جيب RSI ديال 1h من الـ cache
    result_1h = get_cached_data(pair, "1h") or get_price_data(pair, "1h")
    if not result_1h:
        return None, None
    rsi_1h = calc_rsi(result_1h[0])
    if not rsi_1h:
        return None, None

    # الاثنين خاصهم يكونو متفقين
    if 55 <= rsi_15 <= 59 and 55 <= rsi_1h <= 65:
        return "SELL", rsi_15
    elif 40 <= rsi_15 <= 45 and 35 <= rsi_1h <= 45:
        return "BUY", rsi_15
    return None, None

def pull_from_github():
    if not GH_TOKEN or not GITHUB_REPO:
        return []
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{OPPORTUNITIES_FILE}"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return []
    content = base64.b64decode(r.json()["content"]).decode()
    try:
        return json.loads(content)
    except:
        return []

def push_to_github(opportunities):
    if not GH_TOKEN or not GITHUB_REPO:
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{OPPORTUNITIES_FILE}"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(url, headers=headers)
    sha = r.json().get("sha", "") if r.status_code == 200 else ""
    content = json.dumps(opportunities, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    payload = {"message": "update opportunities", "content": encoded, "sha": sha}
    requests.put(url, headers=headers, json=payload)

def monitor_trade(trade):
    global waiting_confirmation, pending_trades
    pair = trade["pair"]

    for i in range(3):
        time.sleep(600)  # كل 10 دقائق
        if not waiting_confirmation.get(pair):
            return

        result = get_price_data(pair)
        if not result:
            continue
        closes, _, _ = result
        current_price = closes[-1]

        if "BUY" in trade["direction"]:
            progress = "📈 السوق ماشي فالاتجاه الصح" if current_price > trade["price"] else "⚠️ السوق راجع شوية"
        else:
            progress = "📈 السوق ماشي فالاتجاه الصح" if current_price < trade["price"] else "⚠️ السوق راجع شوية"

        remaining = 20 - (i + 1) * 10
        send_telegram(
            f"🔄 <b>تحديث — {pair}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"{progress}\n"
            f"💰 السعر دابا: <b>{current_price}</b>\n"
            f"⏳ باقي: <b>{remaining} دقيقة</b>\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

    if waiting_confirmation.get(pair):
        result = get_price_data(pair)
        current_price = result[0][-1] if result else trade["price"]
        send_telegram(
            f"🎯 <b>وقت الدخول — {pair}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"الإشارة باقية قوية ✅\n"
            f"💰 السعر دابا: <b>{current_price}</b>\n"
            f"🎯 TP: <b>{trade['tp']}</b>\n"
            f"🛑 SL: <b>{trade['sl']}</b>\n"
            f"⚖️ R/R: <b>1:{trade['rr']}</b>\n\n"
            f"واش واجد تدخل؟ 🚀\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
    waiting_confirmation[pair] = False
    pending_trades.pop(pair, None)

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

    def do_POST(self):
        global waiting_confirmation, pending_trades
        content_length = int(self.headers['Content-Length'])
        body = self.rfile.read(content_length)
        self.send_response(200)
        self.end_headers()

        try:
            update = json.loads(body)

            if "callback_query" in update:
                cb = update["callback_query"]
                data = cb.get("data", "")
                answer_callback(cb["id"])

                # استخراج الأمر والزوج من callback_data (مثلا: "yes_USDJPY")
                if "_" in data:
                    action, pair_key = data.split("_", 1)
                    # نرجع الزوج الأصلي من pending_trades
                    pair = next((p for p in pending_trades if p.replace("/", "") == pair_key), None)
                else:
                    action, pair = data, None

                if action == "yes" and pair and pair in pending_trades:
                    waiting_confirmation[pair] = True
                    trade = pending_trades[pair].copy()
                    send_telegram(
                        f"✅ <b>واخا! غادي نراقب التريد 30 دقيقة</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"غادي نبعت ليك تحديث كل 10 دقائق 👀\n"
                        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
                    )
                    t = threading.Thread(target=monitor_trade, args=(trade,))
                    t.daemon = True
                    t.start()

                elif action == "no" and pair:
                    pending_trades.pop(pair, None)
                    waiting_confirmation[pair] = False
                    send_telegram("❌ واخا، تجاوزنا هاد التريد. غادي نكملو نراقبو السوق 👀")

        except Exception as e:
            print(f"Webhook error: {e}")

    def log_message(self, format, *args):
        pass

def run_server():
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"Server running on port {PORT}")
    server.serve_forever()


def get_debug_report(pair):
    """Debug report — يستعمل analyze_timeframe مباشرة، نفس الـ checks والـ logic"""
    lines = [f"🔍 {pair}"]

    REQUIRED_CONFIRMATIONS = 2
    buy_ready_count = 0
    sell_ready_count = 0

    for tf in TIMEFRAMES:
        tf_label = {"15min": "15min", "1h": "1H", "4h": "4H"}.get(tf, tf)
        lines.append(f"\n━━━━━━━━")
        lines.append(tf_label)

        tf_result = analyze_timeframe(pair, tf)

        if tf_result is None:
            lines.append("⚠️ بيانات ناقصة")
            continue

        buy_checks  = tf_result["buy_checks"]
        sell_checks = tf_result["sell_checks"]
        buy_score   = tf_result["buy_score"]
        sell_score  = tf_result["sell_score"]
        buy_ready   = buy_score == 4 and tf_result["buy_sr_ok"]
        sell_ready  = sell_score == 4 and tf_result["sell_sr_ok"]

        rsi         = tf_result["rsi"]
        macd        = tf_result["macd"]
        signal_val  = tf_result["signal"]
        macd_diff   = tf_result["macd_diff"]
        ema200      = tf_result["ema200"]
        price       = tf_result["price"]
        trend       = tf_result["trend"]
        atr         = tf_result["atr"]
        d_resist    = round(tf_result["distance_to_resistance"], 6)
        d_support   = round(tf_result["distance_to_support"], 6)
        threshold   = tf_result["sr_threshold"]

        # ---------- BUY ----------
        lines.append("\nBUY")
        lines.append(f"ℹ️ RSI = {rsi} (Optional)")
        lines.append(f"{'✅' if buy_checks['MACD'] else '❌'} MACD = {macd} | Signal = {signal_val} | Diff = {'+' if macd_diff >= 0 else ''}{macd_diff}")
        lines.append(f"{'✅' if buy_checks['EMA200'] else '❌'} EMA200 → Price = {round(price,6)} | EMA200 = {round(ema200,6)}")
        lines.append(f"{'✅' if buy_checks['Trend'] else '❌'} Trend = {trend}")
        lines.append(f"Score: {buy_score}/4")
        lines.append(f"{'✅' if tf_result['buy_sr_ok'] else '❌'} SR Filter → Resistance Dist = {d_resist} | Required > {threshold}")

        if buy_ready:
            buy_ready_count += 1

        # ---------- SELL ----------
        lines.append("\nSELL")
        lines.append(f"ℹ️ RSI = {rsi} (Optional)")
        lines.append(f"{'✅' if sell_checks['MACD'] else '❌'} MACD = {macd} | Signal = {signal_val} | Diff = {'+' if macd_diff >= 0 else ''}{macd_diff}")
        lines.append(f"{'✅' if sell_checks['EMA200'] else '❌'} EMA200 → Price = {round(price,6)} | EMA200 = {round(ema200,6)}")
        lines.append(f"{'✅' if sell_checks['Trend'] else '❌'} Trend = {trend}")
        lines.append(f"Score: {sell_score}/4")
        lines.append(f"{'✅' if tf_result['sell_sr_ok'] else '❌'} SR Filter → Support Dist = {d_support} | Required > {threshold}")

        if sell_ready:
            sell_ready_count += 1

    trade_ready = (
        buy_ready_count  >= REQUIRED_CONFIRMATIONS or
        sell_ready_count >= REQUIRED_CONFIRMATIONS
    )
    lines.append(f"\n━━━━━━━━")
    lines.append(f"BUY Confirmations: {buy_ready_count}/{REQUIRED_CONFIRMATIONS}")
    lines.append(f"SELL Confirmations: {sell_ready_count}/{REQUIRED_CONFIRMATIONS}")
    lines.append(f"Trade Status: {'✅ Trade Ready' if trade_ready else '❌ No Trade'}")

    return "\n".join(lines)


def send_hourly_report(pairs_status):
    """كيبعت تقرير ساعي — رسالة debug منفصلة لكل pair"""
    for pair in pairs_status:
        send_telegram(get_debug_report(pair))

def main_loop():
    global pending_trades, waiting_confirmation
    time.sleep(5)
    set_webhook()

    opportunities = pull_from_github()
    last_report_hour = -1
    already_warned = {}  # كيتذكر واش بعت تحذير لكل زوج
    last_signal = {}       # آخر إشارة مرسلة: {"USD/JPY": "BUY", "AUD/USD": None}
    last_signal_valid = {} # واش الشروط كانت محققة فالـ iteration السابقة

    while True:
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%H:%M UTC")

        try:
            if now.hour == 21 and now.minute < 15:
                today = now.strftime("%Y-%m-%d")
                today_ops = [o for o in opportunities if o.get("date", "").startswith(today)]

                if not today_ops:
                    send_telegram(
                        f"📊 <b>التقرير اليومي — {today}</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"ما كانت كاينة حتى فرصة اليوم\n"
                        f"🕐 {now_str}"
                    )
                else:
                    msg = f"📊 <b>التقرير اليومي — {today}</b>\n━━━━━━━━━━━━━━━━\n"
                    msg += f"📈 عدد الفرص: <b>{len(today_ops)}</b>\n\n"
                    for i, op in enumerate(today_ops, 1):
                        status = "🚫 ملغاة (news)" if op.get("cancelled") else "✅ أُرسلت"
                        msg += (
                            f"<b>{i}. {op['pair']}</b> — {op['direction']}\n"
                            f"   💰 {op['price']} | 🎯 {op['tp']} | 🛑 {op['sl']}\n"
                            f"   ⏱ {op['time']} | {status}\n\n"
                        )
                    msg += "━━━━━━━━━━━━━━━━\n⚠️ هاد المعلومات للتعلم فقط"
                    send_telegram(msg)

                time.sleep(900)
                continue

            # جيب كل البيانات مرة واحدة فبداية كل run
            fetch_all_data()

            # تقرير كل ساعة
            if now.hour != last_report_hour and now.minute < 15 and not any(waiting_confirmation.values()):
                last_report_hour = now.hour
                pairs_status = {pair: {} for pair in PAIRS}
                send_hourly_report(pairs_status)

            # تحذير مسبق 15 دقيقة قبل الإشارة
            if not any(waiting_confirmation.values()):
                for pair in PAIRS:
                    result = get_cached_data(pair, "15min")
                    if result:
                        rsi_current = calc_rsi(result[0])
                        if rsi_current:
                            direction, rsi_val = check_pre_signal(pair, rsi_current)
                            if direction:
                                # ما يعاودش يبعت تحذير إلا إذا تغير الاتجاه
                                if already_warned.get(pair) != direction:
                                    already_warned[pair] = direction
                                    direction_emoji = "📉 SELL" if direction == "SELL" else "📈 BUY"
                                    send_telegram(
                                        f"⚠️ <b>تحذير مسبق — {pair}</b>\n"
                                        f"━━━━━━━━━━━━━━━━\n"
                                        f"RSI = <b>{rsi_val}</b> — كيقترب من منطقة {direction_emoji}\n"
                                        f"⏳ كون مستعد — ممكن تجي إشارة فـ 15 دقيقة\n"
                                        f"🕐 {now_str}"
                                    )
                            else:
                                # RSI رجع للمنطقة المحايدة — نريسيتو
                                already_warned.pop(pair, None)

            for pair in PAIRS:
                    if waiting_confirmation.get(pair):
                        continue

                    trade = analyze_pair(pair)
                    current_direction = "BUY" if trade and "BUY" in trade["direction"] else ("SELL" if trade and "SELL" in trade["direction"] else None)

                    # إذا ماكانش trade — ريسيت الذاكرة باش تسمح بإشارة جديدة لما يرجع
                    if not current_direction:
                        last_signal_valid[pair] = False
                        last_signal.pop(pair, None)
                        continue

                    # واش الشروط كانت فاشلة فالـ iteration السابقة؟ (revalidation)
                    was_invalid = not last_signal_valid.get(pair, True)
                    last_signal_valid[pair] = True

                    # إذا نفس الاتجاه وما فشلتش الشروط بينهم → skip (لا تكرار)
                    if last_signal.get(pair) == current_direction and not was_invalid:
                        continue

                    danger_news, warning_news = get_high_impact_news(pair)

                    op = {
                        "date": now.strftime("%Y-%m-%d %H:%M"),
                        "time": now_str,
                        "pair": pair,
                        "direction": trade["direction"],
                        "price": trade["price"],
                        "tp": trade["tp"],
                        "sl": trade["sl"],
                        "rr": trade["rr"],
                        "strength": trade["strength"],
                        "cancelled": bool(danger_news)
                    }
                    opportunities.append(op)
                    push_to_github(opportunities)

                    if danger_news:
                        send_telegram(
                            f"⚠️ <b>تحذير — {pair}</b>\n"
                            f"━━━━━━━━━━━━━━━━\n"
                            f"كانت كاينة إشارة {trade['direction']} ولكن تم إلغاؤها:\n\n"
                            + "\n".join([f"🔴 {n}" for n in danger_news]) +
                            f"\n\n⏳ استنى تعدي الأخبار\n🕐 {now_str}"
                        )
                        continue

                    tfs_text = " + ".join(trade["confirmed_tfs"])
                    strength_text = get_strength_label(trade["strength"])
                    details_lines = "".join([f"  • {tf}: RSI {data['rsi']}\n" for tf, data in trade["details"].items()])

                    news_warning = ""
                    if warning_news:
                        news_warning = "\n⚠️ <b>أخبار قادمة:</b>\n" + "\n".join([f"🟡 {n}" for n in warning_news]) + "\n"

                    market = get_market_summary(trade['pair'])
                    today_news = get_news_summary(trade['pair'])

                    market_section = ""
                    if market:
                        market_section = (
                            f"\n📊 <b>السوق اليوم:</b>\n"
                            f"  {market['direction_emoji']} التغيير: {market['change']:+.6f} ({market['change_pct']:+.3f}%)\n"
                            f"  🔝 أعلى: {market['high_day']} | 🔻 أدنى: {market['low_day']}\n"
                            f"  {market['last_hour_emoji']} آخر ساعة: {market['last_hour_change']:+.6f}\n"
                        )

                    news_section = ""
                    if today_news:
                        news_section = f"\n📰 <b>أخبار اليوم:</b>\n" + "\n".join([f"  {n}" for n in today_news]) + "\n"

                    msg = (
                        f"🔔 <b>فرصة تريد — {trade['pair']}</b>\n"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"📊 الإشارة: <b>{trade['direction']}</b>\n"
                        f"💪 القوة: <b>{strength_text}</b>\n"
                        f"⏱ مؤكدة على: <b>{tfs_text}</b>\n"
                        f"{market_section}"
                        f"{news_section}"
                        f"\n💰 السعر الحالي: <b>{trade['price']}</b>\n"
                        f"🎯 TP: <b>{trade['tp']}</b>\n"
                        f"🛑 SL: <b>{trade['sl']}</b>\n"
                        f"⚖️ R/R: <b>1:{trade['rr']}</b>\n\n"
                        f"📋 RSI Details:\n{details_lines}"
                        f"{news_warning}"
                        f"━━━━━━━━━━━━━━━━\n"
                        f"🕐 {now_str}\n\n"
                        f"واش بغيتي تدخل هاد التريد؟"
                    )

                    pending_trades[pair] = trade          # كل زوج عنده trade خاص بيه
                    last_signal[pair] = current_direction  # تسجيل الإشارة باش ما تتكررش
                    send_with_buttons(msg, trade)
                    # ماكاينش break — كيكمل على باقي الأزواج

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(900)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
    main_loop()
