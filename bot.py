import os
import json
import time
import requests
import threading
import base64
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================
# GOLD SMC BOT v2 — مرحلة التعلم، بتحسينات مبنية على دروس Bot 3
#
# الفرق عن v1:
#   1. BOS ماشي بس close>swing_level — خاصو Displacement حقيقي
#      (مسافة كسر معتبرة + جسم شمعة حقيقي) لتفادي false breaks
#   2. SL/TP هيكليين حقيقيين (SL وراء OB الحقيقي، TP من أقرب
#      liquidity/swing هيكلي) — بلا ATR multiplier أعمى، وبلا
#      تصغير SL باش نوصلو RR=1.5 (نفس غلطة Bot 3 اللي تفادينا)
#   3. RR كيتحسب ويتسجل كيفما طلع — بلا رفض فمرحلة التعلم
#   4. Session مسجلة (Asian/London/Overlap/NY) للتحليل، بلا رفض
#   5. Snapshot كامل فالجورنال (كلشي: bias, structure, sweep,
#      displacement, BOS/CHoCH, OB, FVG, confirmation, session, news)
# ============================================================

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
GH_TOKEN = os.environ.get("GH_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

PAIRS = ["XAU/USD"]
TIMEFRAMES = ["15min", "1h", "4h"]
OPPORTUNITIES_FILE = "opportunities_gold.json"
PRICE_DECIMALS = 2
GOLD_NEWS_CURRENCIES = ["USD"]

SWING_LOOKBACK = 3
MAJOR_SWING_LOOKBACK = 5
PULLBACK_MAX_CANDLES = 6
BOS_MAX_CANDLES = 10
SWEEP_ATR_MULTIPLIER = 0.15
RECENT_CHECK_CANDLES = 3
PULLBACK_TOUCH_ATR = 0.3

# ==== Displacement (BOS حقيقي، ماشي كسر هامشي) ====
DISPLACEMENT_BREAK_ATR = 0.25   # الكسر خاصو يتجاوز المستوى بـ 0.25×ATR على الأقل
DISPLACEMENT_BODY_ATR = 0.30    # جسم شمعة الكسر خاصو يكون ≥0.30×ATR (ماشي دوجي/wick)

# ==== SL/TP هيكلي ====
SL_STRUCTURE_BUFFER_ATR = 0.15  # buffer بسيط تحت/فوق الـ OB (وراء الـ invalidation)
SL_MIN_BUFFER_DOLLARS = 0.10    # حد أدنى للـ buffer بالدولار (باش ما يكونش صفر فحالة ATR صغير جداً)
FALLBACK_TARGET_ATR_MULT = 3.0  # هدف احتياطي إلا ماكانش أي هدف هيكلي صالح (نادر، للتسجيل فقط)

pending_trades = {}
waiting_confirmation = {}
sequence_state = {}
data_cache = {}

def fetch_all_data():
    global data_cache
    data_cache = {}
    for pair in PAIRS:
        data_cache[pair] = {}
        for tf in TIMEFRAMES:
            result = get_price_data(pair, tf)
            data_cache[pair][tf] = result

def get_cached_data(pair, interval):
    return data_cache.get(pair, {}).get(interval, None)

def send_telegram(msg, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(url, json=payload)

def send_with_buttons(msg, trade):
    pair_key = trade["pair"].replace("/", "")
    keyboard = {"inline_keyboard": [[
        {"text": "✅ نعم، دخلها!", "callback_data": f"yes_{pair_key}"},
        {"text": "❌ لا، تجاوزها", "callback_data": f"no_{pair_key}"}
    ]]}
    send_telegram(msg, reply_markup=keyboard)

def answer_callback(callback_query_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id})

def set_webhook():
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook")
    time.sleep(2)
    if not WEBHOOK_URL:
        print("⚠️ WEBHOOK_URL ماشي مضبوط فـ Railway Variables", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    r = requests.post(url, json={"url": WEBHOOK_URL})
    print(f"Webhook set: {r.json()}")

def is_killzone():
    """بوابة الإرسال الفعلي: London+NY overlap بشكل عام (نفس منطق الفوركس)"""
    now_utc = datetime.now(timezone.utc)
    return 7 <= now_utc.hour < 17

def get_session(hour_utc):
    """للتسجيل فقط — بلا رفض. باش نحلل بعدها واش Asian فعلاً أضعف للذهب."""
    if 0 <= hour_utc < 7:
        return "Asian"
    elif 7 <= hour_utc < 12:
        return "London"
    elif 12 <= hour_utc < 16:
        return "London_NY_Overlap"
    elif 16 <= hour_utc < 21:
        return "New_York"
    else:
        return "Late_NY_PreAsian"

def get_news_context():
    """كترجع لائحة كاملة (context)، ماشي بس binary — للتسجيل والتحليل"""
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, timeout=10)
        events = r.json()
        now = datetime.now(timezone.utc)
        context = []
        danger_events = []
        warning_events = []
        for event in events:
            if event.get("impact") != "High":
                continue
            if event.get("currency") not in GOLD_NEWS_CURRENCIES:
                continue
            try:
                event_time = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
            except:
                continue
            diff_minutes = round((event_time - now).total_seconds() / 60, 1)
            if -60 <= diff_minutes <= 240:
                context.append({"title": event["title"], "minutes_to_event": diff_minutes})
            if -30 <= diff_minutes <= 120:
                danger_events.append(event["title"])
            elif 120 < diff_minutes <= 480:
                warning_events.append(event["title"])
        return context, danger_events, warning_events
    except:
        return [], [], []

price_cache = {}
CACHE_SECONDS = {"15min": 900, "1h": 3600, "4h": 14400}

def get_price_data(pair, interval="15min", outputsize=250):
    global price_cache
    cache_key = f"{pair}_{interval}"
    now_ts = time.time()
    if cache_key in price_cache:
        cached_time = price_cache[cache_key]["time"]
        if now_ts - cached_time < CACHE_SECONDS.get(interval, 900):
            return price_cache[cache_key]["data"]
    params = {"symbol": pair, "interval": interval, "outputsize": outputsize, "apikey": TWELVE_DATA_API_KEY}
    try:
        r = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=30)
        data = r.json()
        if "values" not in data:
            print(f"API Error {pair} {interval}: {data.get('message', data.get('code', 'unknown'))}")
            return None

        # ==== فحص تشخيصي: واش آخر شمعة مسدودة ولا لسع كتتكون؟ (بلا تغيير فأي منطق) ====
        if data["values"]:
            last_candle_time = data["values"][0]["datetime"]
            print(f"🕐 [{pair} {interval}] آخر شمعة: {last_candle_time} | الوقت الحالي (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

        closes = [float(v["close"]) for v in reversed(data["values"])]
        highs = [float(v["high"]) for v in reversed(data["values"])]
        lows = [float(v["low"]) for v in reversed(data["values"])]
        opens = [float(v["open"]) for v in reversed(data["values"])]
        result = (closes, highs, lows, opens)
        price_cache[cache_key] = {"time": now_ts, "data": result}
        return result
    except Exception as e:
        print(f"Price API Error {pair} {interval}: {e}")
        return None

def calc_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return round(sum(trs[-period:]) / period, 4)

def get_swing_points(highs, lows):
    """Confirmed swings فقط — الـ loop كيوقف عند n-LOOKBACK، فماكاينش
    طريقة swing يتسجل قبل ما يتأكد بـ LOOKBACK شموع كاملة بعدو (بلا look-ahead bias)."""
    swings = []
    n = len(highs)
    for i in range(SWING_LOOKBACK, n - SWING_LOOKBACK):
        window_highs = highs[i - SWING_LOOKBACK: i + SWING_LOOKBACK + 1]
        window_lows = lows[i - SWING_LOOKBACK: i + SWING_LOOKBACK + 1]
        if highs[i] == max(window_highs) and window_highs.count(highs[i]) == 1:
            swings.append((i, highs[i], "high"))
        if lows[i] == min(window_lows) and window_lows.count(lows[i]) == 1:
            swings.append((i, lows[i], "low"))
    return swings

def get_last_swing(swings, swing_type, before_index=None):
    filtered = [s for s in swings if s[2] == swing_type]
    if before_index is not None:
        filtered = [s for s in filtered if s[0] < before_index]
    if not filtered:
        return None
    return filtered[-1]

def has_displacement(opens, highs, lows, closes, i, swing_level, atr, direction):
    """BOS حقيقي بـ Displacement: الكسر خاصو يكون بمسافة معتبرة + جسم شمعة حقيقي،
    ماشي close>level بالكاد (كيتفادى false breaks/wicks السريعة ديال الذهب)."""
    if atr is None or atr <= 0:
        return False
    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
    body = abs(c - o)
    if direction == "BUY":
        break_distance = c - swing_level
    else:
        break_distance = swing_level - c
    return break_distance >= (atr * DISPLACEMENT_BREAK_ATR) and body >= (atr * DISPLACEMENT_BODY_ATR)

def is_bullish_engulfing(opens, closes, i):
    if i < 1:
        return False
    prev_open, prev_close = opens[i-1], closes[i-1]
    curr_open, curr_close = opens[i], closes[i]
    return prev_close < prev_open and curr_close > curr_open and curr_open <= prev_close and curr_close >= prev_open

def is_bearish_engulfing(opens, closes, i):
    if i < 1:
        return False
    prev_open, prev_close = opens[i-1], closes[i-1]
    curr_open, curr_close = opens[i], closes[i]
    return prev_close > prev_open and curr_close < curr_open and curr_open >= prev_close and curr_close <= prev_open

def is_strong_bull_candle(opens, highs, lows, closes, i):
    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
    total_range = h - l
    return total_range > 0 and (c - o) > 0 and ((c - o) / total_range) > 0.70

def is_strong_bear_candle(opens, highs, lows, closes, i):
    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
    total_range = h - l
    return total_range > 0 and (o - c) > 0 and ((o - c) / total_range) > 0.70

def check_candlestick_confirmation(opens, highs, lows, closes, direction):
    n = len(closes)
    start = max(1, n - RECENT_CHECK_CANDLES)
    for i in range(start, n):
        if direction == "BUY":
            if is_bullish_engulfing(opens, closes, i) or is_strong_bull_candle(opens, highs, lows, closes, i):
                return True
        else:
            if is_bearish_engulfing(opens, closes, i) or is_strong_bear_candle(opens, highs, lows, closes, i):
                return True
    return False

def identify_candlestick_pattern(opens, highs, lows, closes, direction):
    n = len(closes)
    start = max(1, n - RECENT_CHECK_CANDLES)
    for i in range(start, n):
        if direction == "BUY":
            if is_bullish_engulfing(opens, closes, i):
                return "Bullish Engulfing"
            if is_strong_bull_candle(opens, highs, lows, closes, i):
                return "Strong Bullish Candle"
        else:
            if is_bearish_engulfing(opens, closes, i):
                return "Bearish Engulfing"
            if is_strong_bear_candle(opens, highs, lows, closes, i):
                return "Strong Bearish Candle"
    return None

def reset_state(state_key):
    sequence_state[state_key] = {"stage": "waiting_sweep"}

def check_recent_sweep(highs, lows, closes, swings, sweep_threshold):
    n = len(closes)
    start = max(0, n - RECENT_CHECK_CANDLES)
    for i in range(start, n):
        last_swing_low = get_last_swing(swings, "low", before_index=i)
        last_swing_high = get_last_swing(swings, "high", before_index=i)
        if last_swing_low:
            low_level = last_swing_low[1]
            if lows[i] < (low_level - sweep_threshold) and closes[i] > low_level:
                return "BUY", low_level
        if last_swing_high:
            high_level = last_swing_high[1]
            if highs[i] > (high_level + sweep_threshold) and closes[i] < high_level:
                return "SELL", high_level
    return None

def find_order_block_buy(closes, opens, highs, lows, bos_index):
    for j in range(bos_index, max(0, bos_index - 15), -1):
        if closes[j] < opens[j]:
            return lows[j], highs[j]
    return lows[bos_index], highs[bos_index]

def find_order_block_sell(closes, opens, highs, lows, bos_index):
    for j in range(bos_index, max(0, bos_index - 15), -1):
        if closes[j] > opens[j]:
            return lows[j], highs[j]
    return lows[bos_index], highs[bos_index]

def find_recent_fvg_buy(highs, lows, bos_index):
    for j in range(bos_index, max(2, bos_index - 5), -1):
        if lows[j] > highs[j-2]:
            return highs[j-2], lows[j]
    return None

def find_recent_fvg_sell(highs, lows, bos_index):
    for j in range(bos_index, max(2, bos_index - 5), -1):
        if highs[j] < lows[j-2]:
            return highs[j], lows[j-2]
    return None

def analyze_timeframe(pair, interval):
    result = get_cached_data(pair, interval) or get_price_data(pair, interval)
    if not result:
        return None
    closes, highs, lows, opens = result
    atr = calc_atr(highs, lows, closes)
    if atr is None:
        return None
    swings = get_swing_points(highs, lows)
    if not swings:
        return None

    state_key = f"{pair}_{interval}"
    state = sequence_state.get(state_key, {"stage": "waiting_sweep"})
    current_price = closes[-1]
    current_close = closes[-1]
    sweep_threshold = atr * SWEEP_ATR_MULTIPLIER

    # ---------- المرحلة 1: Liquidity Sweep ----------
    if state["stage"] == "waiting_sweep":
        sweep = check_recent_sweep(highs, lows, closes, swings, sweep_threshold)
        if sweep:
            direction, swing_level = sweep
            sequence_state[state_key] = {"stage": "waiting_bos", "direction": direction,
                                          "swing_level": swing_level, "candles_since_sweep": 0}
        return None

    # ---------- المرحلة 2: BOS بـ Displacement حقيقي (ماشي close>level بالكاد) ----------
    if state["stage"] == "waiting_bos":
        direction = state["direction"]
        bos_found = False
        bos_level = None
        displacement_value = None
        bos_index = len(closes) - 1

        if direction == "BUY":
            last_swing_high = get_last_swing(swings, "high")
            if last_swing_high and has_displacement(opens, highs, lows, closes, bos_index, last_swing_high[1], atr, "BUY"):
                bos_found = True
                bos_level = last_swing_high[1]
                displacement_value = round(closes[bos_index] - bos_level, 4)
        else:
            last_swing_low = get_last_swing(swings, "low")
            if last_swing_low and has_displacement(opens, highs, lows, closes, bos_index, last_swing_low[1], atr, "SELL"):
                bos_found = True
                bos_level = last_swing_low[1]
                displacement_value = round(bos_level - closes[bos_index], 4)

        if bos_found:
            if direction == "BUY":
                ob_low, ob_high = find_order_block_buy(closes, opens, highs, lows, bos_index)
                fvg = find_recent_fvg_buy(highs, lows, bos_index)
            else:
                ob_low, ob_high = find_order_block_sell(closes, opens, highs, lows, bos_index)
                fvg = find_recent_fvg_sell(highs, lows, bos_index)
            fvg_low = fvg[0] if fvg else ob_low
            fvg_high = fvg[1] if fvg else ob_high
            state["stage"] = "waiting_pullback"
            state["bos_level"] = bos_level
            state["displacement_value"] = displacement_value
            state["ob_low"] = ob_low
            state["ob_high"] = ob_high
            state["fvg_low"] = fvg_low
            state["fvg_high"] = fvg_high
            state["candles_since_bos"] = 0
            state["touched_bos"] = False
            sequence_state[state_key] = state
            return None

        state["candles_since_sweep"] = state.get("candles_since_sweep", 0) + 1
        if state["candles_since_sweep"] > BOS_MAX_CANDLES:
            reset_state(state_key)
            return None
        sequence_state[state_key] = state
        return None

    # ---------- المرحلة 3: Pullback لـ OB/FVG ----------
    if state["stage"] == "waiting_pullback":
        direction = state["direction"]
        ob_low = state["ob_low"]
        ob_high = state["ob_high"]
        fvg_low = state["fvg_low"]
        fvg_high = state["fvg_high"]

        if direction == "BUY":
            if current_close < ob_low:
                reset_state(state_key)
                return None
            pullback_boundary = max(ob_high, fvg_high)
            if lows[-1] <= pullback_boundary + (atr * PULLBACK_TOUCH_ATR):
                state["touched_bos"] = True
            if state.get("touched_bos") and current_close > pullback_boundary:
                state["stage"] = "waiting_candle"
                sequence_state[state_key] = state
                return None
        else:
            if current_close > ob_high:
                reset_state(state_key)
                return None
            pullback_boundary = min(ob_low, fvg_low)
            if highs[-1] >= pullback_boundary - (atr * PULLBACK_TOUCH_ATR):
                state["touched_bos"] = True
            if state.get("touched_bos") and current_close < pullback_boundary:
                state["stage"] = "waiting_candle"
                sequence_state[state_key] = state
                return None

        state["candles_since_bos"] = state.get("candles_since_bos", 0) + 1
        if state["candles_since_bos"] > PULLBACK_MAX_CANDLES:
            reset_state(state_key)
            return None
        sequence_state[state_key] = state
        return None

    # ---------- المرحلة 4: Candle Confirmation ----------
    if state["stage"] == "waiting_candle":
        direction = state["direction"]
        bos_level = state["bos_level"]
        ob_low = state["ob_low"]
        ob_high = state["ob_high"]

        if direction == "BUY":
            if current_close < ob_low:
                reset_state(state_key)
                return None
        else:
            if current_close > ob_high:
                reset_state(state_key)
                return None

        confirmed = check_candlestick_confirmation(opens, highs, lows, closes, direction)
        if confirmed:
            reset_state(state_key)
            fvg_low = state.get("fvg_low")
            fvg_high = state.get("fvg_high")
            pullback_boundary = max(ob_high, fvg_high) if direction == "BUY" else min(ob_low, fvg_low)
            last_swing_high = get_last_swing(swings, "high")
            last_swing_low = get_last_swing(swings, "low")
            pattern_name = identify_candlestick_pattern(opens, highs, lows, closes, direction)
            return {
                "direction": direction, "atr": atr, "price": current_price, "bos_level": bos_level,
                "displacement_value": state.get("displacement_value"),
                "sweep_level": state.get("swing_level"),
                "candles_since_sweep": state.get("candles_since_sweep"),
                "candles_since_bos": state.get("candles_since_bos"),
                "ob_low": ob_low, "ob_high": ob_high, "fvg_low": fvg_low, "fvg_high": fvg_high,
                "pullback_boundary": pullback_boundary,
                "touched_ob_or_fvg": state.get("touched_bos"),
                "last_swing_high": last_swing_high[1] if last_swing_high else None,
                "last_swing_low": last_swing_low[1] if last_swing_low else None,
                "candle_pattern": pattern_name,
                "confirmation_open": opens[-1], "confirmation_high": highs[-1],
                "confirmation_low": lows[-1], "confirmation_close": closes[-1],
            }

        state["candles_since_bos"] = state.get("candles_since_bos", 0) + 1
        if state["candles_since_bos"] > PULLBACK_MAX_CANDLES + RECENT_CHECK_CANDLES:
            reset_state(state_key)
            return None
        sequence_state[state_key] = state
        return None

def get_major_swing_points(highs, lows, lookback=MAJOR_SWING_LOOKBACK):
    swings = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        window_highs = highs[i - lookback: i + lookback + 1]
        window_lows = lows[i - lookback: i + lookback + 1]
        if highs[i] == max(window_highs) and window_highs.count(highs[i]) == 1:
            swings.append((i, highs[i], "high"))
        if lows[i] == min(window_lows) and window_lows.count(lows[i]) == 1:
            swings.append((i, lows[i], "low"))
    swings.sort(key=lambda s: s[0])
    return swings

def get_smc_htf_bias(highs, lows, closes):
    swings = get_major_swing_points(highs, lows)
    if len(swings) < 2:
        return None
    trend = None
    structure_high = None
    structure_low = None
    choch_direction = None
    bias = None
    n = len(closes)
    for idx in range(len(swings)):
        i, level, kind = swings[idx]
        next_swing_index = swings[idx + 1][0] if idx + 1 < len(swings) else n
        confirm_start = i + 1
        confirm_end = min(next_swing_index, n)
        if kind == "high":
            if structure_high is None:
                structure_high = level
                continue
            if trend != "UP":
                confirmed = any(closes[j] > structure_high for j in range(confirm_start, confirm_end))
                if confirmed:
                    if trend is None or choch_direction == "UP":
                        trend = "UP"; bias = "BUY"; choch_direction = None; structure_high = level
                    else:
                        choch_direction = "UP"
        else:
            if structure_low is None:
                structure_low = level
                continue
            if trend != "DOWN":
                confirmed = any(closes[j] < structure_low for j in range(confirm_start, confirm_end))
                if confirmed:
                    if trend is None or choch_direction == "DOWN":
                        trend = "DOWN"; bias = "SELL"; choch_direction = None; structure_low = level
                    else:
                        choch_direction = "DOWN"
    return bias

def get_timeframe_bias(pair, interval):
    result = get_cached_data(pair, interval) or get_price_data(pair, interval)
    if not result:
        return None
    closes, highs, lows, opens = result
    return get_smc_htf_bias(highs, lows, closes)

def get_htf_diagnostic_info(highs, lows, closes):
    swings = get_major_swing_points(highs, lows)
    if len(swings) < 2:
        return {"bias": None, "choch_seen": False, "bos_seen": False, "major_swing_high": None, "major_swing_low": None}
    trend = None
    structure_high = None
    structure_low = None
    choch_direction = None
    bias = None
    choch_seen = False
    bos_seen = False
    n = len(closes)
    for idx in range(len(swings)):
        i, level, kind = swings[idx]
        next_swing_index = swings[idx + 1][0] if idx + 1 < len(swings) else n
        confirm_start = i + 1
        confirm_end = min(next_swing_index, n)
        if kind == "high":
            if structure_high is None:
                structure_high = level
                continue
            if trend != "UP":
                confirmed = any(closes[j] > structure_high for j in range(confirm_start, confirm_end))
                if confirmed:
                    if trend is None or choch_direction == "UP":
                        if choch_direction == "UP":
                            bos_seen = True
                        trend = "UP"; bias = "BUY"; choch_direction = None; structure_high = level
                    else:
                        choch_direction = "UP"; choch_seen = True
        else:
            if structure_low is None:
                structure_low = level
                continue
            if trend != "DOWN":
                confirmed = any(closes[j] < structure_low for j in range(confirm_start, confirm_end))
                if confirmed:
                    if trend is None or choch_direction == "DOWN":
                        if choch_direction == "DOWN":
                            bos_seen = True
                        trend = "DOWN"; bias = "SELL"; choch_direction = None; structure_low = level
                    else:
                        choch_direction = "DOWN"; choch_seen = True
    last_high = get_last_swing(swings, "high")
    last_low = get_last_swing(swings, "low")
    return {"bias": bias, "choch_seen": choch_seen, "bos_seen": bos_seen,
            "major_swing_high": last_high[1] if last_high else None,
            "major_swing_low": last_low[1] if last_low else None}

def reset_pair_states(pair):
    for tf in TIMEFRAMES:
        reset_state(f"{pair}_{tf}")

def calculate_structural_sl_tp(direction, price, atr, ob_low, ob_high,
                                last_swing_high_15m, last_swing_low_15m,
                                h1_major_swing_high, h1_major_swing_low,
                                h4_major_swing_high, h4_major_swing_low):
    """
    SL: وراء الـ OB الحقيقي (invalidation structure) + buffer بسيط — بلا ATR multiplier أعمى،
        وبلا تصغير SL باش نوصلو لـ RR معين (نفس غلطة Bot 3 اللي تفادينا هنا).
    TP: أقرب liquidity/swing هيكلي صالح فالاتجاه (15m أولاً، وإلا H1/H4 كـ fallback).
    RR: كيتحسب من بعد، ويتسجل كيفما طلع (بلا رفض فمرحلة التعلم).
    """
    buffer_ = max(atr * SL_STRUCTURE_BUFFER_ATR, SL_MIN_BUFFER_DOLLARS)

    if direction == "BUY":
        sl = round(ob_low - buffer_, PRICE_DECIMALS)
        sl_distance = price - sl

        candidates = [lv for lv in [last_swing_high_15m, h1_major_swing_high, h4_major_swing_high]
                      if lv is not None and lv > price]
        candidates.sort(key=lambda lv: lv - price)  # الأقرب أولاً
        target_source = "15m_swing"
        if candidates:
            tp = round(candidates[0], PRICE_DECIMALS)
            if len(candidates) > 0 and candidates[0] != (last_swing_high_15m or -1):
                target_source = "htf_fallback"
        else:
            tp = round(price + (atr * FALLBACK_TARGET_ATR_MULT), PRICE_DECIMALS)
            target_source = "atr_fallback_no_structure"
    else:
        sl = round(ob_high + buffer_, PRICE_DECIMALS)
        sl_distance = sl - price

        candidates = [lv for lv in [last_swing_low_15m, h1_major_swing_low, h4_major_swing_low]
                      if lv is not None and lv < price]
        candidates.sort(key=lambda lv: price - lv)
        target_source = "15m_swing"
        if candidates:
            tp = round(candidates[0], PRICE_DECIMALS)
            if len(candidates) > 0 and candidates[0] != (last_swing_low_15m or -1):
                target_source = "htf_fallback"
        else:
            tp = round(price - (atr * FALLBACK_TARGET_ATR_MULT), PRICE_DECIMALS)
            target_source = "atr_fallback_no_structure"

    tp_distance = abs(tp - price)
    rr = round(tp_distance / sl_distance, 2) if sl_distance > 0 else None

    return {
        "sl": sl, "tp": tp, "rr": rr,
        "sl_distance": round(sl_distance, 4), "tp_distance": round(tp_distance, 4),
        "target_source": target_source, "sl_buffer_used": round(buffer_, 4),
    }

def analyze_pair(pair):
    """مرحلة التعلم: بلا رفض على أساس RR أو أي فلتر — الهدف تسجيل snapshot كامل ودقيق."""
    m15_res = analyze_timeframe(pair, "15min")
    if not m15_res:
        return None

    direction = m15_res["direction"]
    price = m15_res["price"]
    atr = m15_res["atr"]

    h1_bias = get_timeframe_bias(pair, "1h")
    h4_bias = get_timeframe_bias(pair, "4h")

    confirmed_tfs = ["15min"]
    if h1_bias == direction:
        confirmed_tfs.append("1h")
    if h4_bias == direction:
        confirmed_tfs.append("4h")

    result_1h = get_cached_data(pair, "1h")
    result_4h = get_cached_data(pair, "4h")
    h1_diag = {"bias": h1_bias, "choch_seen": False, "bos_seen": False, "major_swing_high": None, "major_swing_low": None}
    h4_diag = {"bias": h4_bias, "choch_seen": False, "bos_seen": False, "major_swing_high": None, "major_swing_low": None}
    if result_1h:
        closes_1h, highs_1h, lows_1h, opens_1h = result_1h
        h1_diag = get_htf_diagnostic_info(highs_1h, lows_1h, closes_1h)
    if result_4h:
        closes_4h, highs_4h, lows_4h, opens_4h = result_4h
        h4_diag = get_htf_diagnostic_info(highs_4h, lows_4h, closes_4h)

    sltp = calculate_structural_sl_tp(
        direction, price, atr,
        m15_res.get("ob_low"), m15_res.get("ob_high"),
        m15_res.get("last_swing_high"), m15_res.get("last_swing_low"),
        h1_diag.get("major_swing_high"), h1_diag.get("major_swing_low"),
        h4_diag.get("major_swing_high"), h4_diag.get("major_swing_low"),
    )

    now_utc = datetime.now(timezone.utc)
    session = get_session(now_utc.hour)
    news_context, danger_news, warning_news = get_news_context()

    return {
        "pair": pair,
        "direction": "BUY 📈" if direction == "BUY" else "SELL 📉",
        "price": price, "tp": sltp["tp"], "sl": sltp["sl"], "rr": sltp["rr"],
        "target_source": sltp["target_source"], "sl_buffer_used": sltp["sl_buffer_used"],
        "strength": len(confirmed_tfs), "confirmed_tfs": confirmed_tfs,
        "details": {"15min": m15_res},
        "h1_diag": h1_diag, "h4_diag": h4_diag,
        "session": session,
        "news_context": news_context, "danger_news": danger_news, "warning_news": warning_news,
    }

def get_strength_label(strength):
    if strength == 3:
        return "⭐⭐⭐ Gold Alignment (15min + 1H + 4H)"
    elif strength == 2:
        return "⭐⭐ Silver Alignment (15min + HTF)"
    return "⭐ Bronze (15min Setup)"

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
    print("🔍 push_to_github: بدات", flush=True)
    if not GH_TOKEN or not GITHUB_REPO:
        print(f"🔍 push_to_github: GH_TOKEN={bool(GH_TOKEN)} GITHUB_REPO={bool(GITHUB_REPO)} — رجعت بلا كتابة", flush=True)
        return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{OPPORTUNITIES_FILE}"
    headers = {"Authorization": f"token {GH_TOKEN}"}
    r = requests.get(url, headers=headers)
    print(f"🔍 push_to_github: GET status={r.status_code}", flush=True)
    sha = r.json().get("sha", "") if r.status_code == 200 else ""
    content = json.dumps(opportunities, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(content.encode()).decode()
    payload = {"message": "update opportunities_gold", "content": encoded, "sha": sha}
    put_r = requests.put(url, headers=headers, json=payload)
    print(f"🔍 push_to_github: PUT status={put_r.status_code}", flush=True)
    if put_r.status_code not in (200, 201):
        print(f"❌ فشل push_to_github: {put_r.status_code} — {put_r.text}", flush=True)
    else:
        print("✅ push_to_github: نجحت الكتابة", flush=True)

def monitor_trade(trade):
    global waiting_confirmation, pending_trades
    pair = trade["pair"]
    for i in range(3):
        time.sleep(600)
        if not waiting_confirmation.get(pair):
            return
        result = get_price_data(pair)
        if not result:
            continue
        closes = result[0]
        current_price = closes[-1]
        if "BUY" in trade["direction"]:
            progress = "📈 السوق ماشي فالاتجاه الصح" if current_price > trade["price"] else "⚠️ السوق راجع شوية"
        else:
            progress = "📈 السوق ماشي فالاتجاه الصح" if current_price < trade["price"] else "⚠️ السوق راجع شوية"
        remaining = 20 - (i + 1) * 10
        send_telegram(
            f"🔄 <b>تحديث — {trade['pair']}</b>\n━━━━━━━━━━━━━━━━\n{progress}\n"
            f"💰 السعر دابا: <b>{current_price}</b>\n⏳ باقي: <b>{remaining} دقيقة</b>\n"
            f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
    if waiting_confirmation.get(pair):
        result = get_price_data(pair)
        current_price = result[0][-1] if result else trade["price"]
        send_telegram(
            f"🎯 <b>وقت الدخول — {pair}</b>\n━━━━━━━━━━━━━━━━\nالإشارة باقية قوية ✅\n"
            f"💰 السعر دابا: <b>{current_price}</b>\n🎯 TP: <b>{trade['tp']}</b>\n🛑 SL: <b>{trade['sl']}</b>\n"
            f"⚖️ R/R: <b>1:{trade['rr']}</b>\n\nواش واجد تدخل؟ 🚀\n🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )
    waiting_confirmation[pair] = False
    pending_trades.pop(pair, None)

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Gold Bot v2 is running!")

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
                if "_" in data:
                    action, pair_key = data.split("_", 1)
                    pair = next((p for p in pending_trades if p.replace("/", "") == pair_key), None)
                else:
                    action, pair = data, None
                if action == "yes" and pair and pair in pending_trades:
                    waiting_confirmation[pair] = True
                    trade = pending_trades[pair].copy()
                    send_telegram(
                        f"✅ <b>واخا! غادي نراقب التريد 30 دقيقة</b>\n━━━━━━━━━━━━━━━━\n"
                        f"غادي نبعت ليك تحديث كل 10 دقائق 👀\n🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
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

def main_loop():
    global pending_trades, waiting_confirmation
    time.sleep(5)
    set_webhook()

    opportunities = pull_from_github()
    last_signal = {}

    while True:
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%H:%M UTC")

        try:
            fetch_all_data()

            for pair in PAIRS:
                if waiting_confirmation.get(pair):
                    continue

                trade = analyze_pair(pair)
                current_direction = "BUY" if trade and "BUY" in trade["direction"] else ("SELL" if trade and "SELL" in trade["direction"] else None)

                if not current_direction:
                    continue

                if not is_killzone():
                    print(f"⏳ {pair}: فرصة جاهزة، تم تأجيلها لعدم دخول الـ Killzone بعد. (session: {trade.get('session')})")
                    continue

                current_bos_level = trade["details"]["15min"]["bos_level"]

                prev = last_signal.get(pair)
                if prev is not None:
                    same_direction = prev["direction"] == current_direction
                    same_bos = prev["bos_level"] == current_bos_level
                    if same_direction and same_bos:
                        continue

                m15_details = trade["details"]["15min"]
                h1_diag = trade.get("h1_diag", {})
                h4_diag = trade.get("h4_diag", {})
                danger_news = trade.get("danger_news", [])
                warning_news = trade.get("warning_news", [])

                op = {
                    "date": now.strftime("%Y-%m-%d %H:%M"), "time": now_str, "pair": pair,
                    "direction": trade["direction"], "price": trade["price"], "tp": trade["tp"],
                    "sl": trade["sl"], "rr": trade["rr"], "strength": trade["strength"],
                    "cancelled": bool(danger_news),
                    "session": trade.get("session"),
                    "target_source": trade.get("target_source"),
                    "sl_buffer_used": trade.get("sl_buffer_used"),
                    "news_context": trade.get("news_context", []),
                    "timestamp": now.timestamp(),
                    "market_info": {"atr": m15_details.get("atr"), "current_price": trade["price"]},
                    "htf_info": {
                        "h1_bias": h1_diag.get("bias"), "h1_choch_seen": h1_diag.get("choch_seen"),
                        "h1_bos_seen": h1_diag.get("bos_seen"),
                        "h1_major_swing_high": h1_diag.get("major_swing_high"),
                        "h1_major_swing_low": h1_diag.get("major_swing_low"),
                        "h4_bias": h4_diag.get("bias"), "h4_choch_seen": h4_diag.get("choch_seen"),
                        "h4_bos_seen": h4_diag.get("bos_seen"),
                        "h4_major_swing_high": h4_diag.get("major_swing_high"),
                        "h4_major_swing_low": h4_diag.get("major_swing_low"),
                    },
                    "smc_info": {
                        "sweep_level": m15_details.get("sweep_level"),
                        "candles_since_sweep": m15_details.get("candles_since_sweep"),
                        "bos_level": m15_details.get("bos_level"),
                        "displacement_value": m15_details.get("displacement_value"),
                        "candles_since_bos": m15_details.get("candles_since_bos"),
                        "ob_low": m15_details.get("ob_low"), "ob_high": m15_details.get("ob_high"),
                        "fvg_low": m15_details.get("fvg_low"), "fvg_high": m15_details.get("fvg_high"),
                    },
                    "entry_info": {"entry_price": trade["price"], "tp": trade["tp"], "sl": trade["sl"], "rr": trade["rr"]},
                    "pullback_info": {
                        "pullback_boundary": m15_details.get("pullback_boundary"),
                        "touched_ob_or_fvg": m15_details.get("touched_ob_or_fvg"),
                    },
                    "candle_confirmation_info": {
                        "pattern": m15_details.get("candle_pattern"),
                        "open": m15_details.get("confirmation_open"),
                        "high": m15_details.get("confirmation_high"),
                        "low": m15_details.get("confirmation_low"),
                        "close": m15_details.get("confirmation_close"),
                    },
                    "swing_info": {
                        "last_swing_high_15m": m15_details.get("last_swing_high"),
                        "last_swing_low_15m": m15_details.get("last_swing_low"),
                    },
                }
                opportunities.append(op)
                push_to_github(opportunities)

                if danger_news:
                    reset_pair_states(pair)
                    last_signal.pop(pair, None)
                    send_telegram(
                        f"⚠️ <b>تحذير — {pair}</b>\n━━━━━━━━━━━━━━━━\n"
                        f"كانت كاينة إشارة {trade['direction']} ولكن تم إلغاؤها:\n\n"
                        + "\n".join([f"🔴 {n}" for n in danger_news]) +
                        f"\n\n⏳ استنى تعدي الأخبار\n🕐 {now_str}"
                    )
                    continue

                tfs_text = " + ".join(trade["confirmed_tfs"])
                strength_text = get_strength_label(trade["strength"])

                news_warning = ""
                if warning_news:
                    news_warning = "\n⚠️ <b>أخبار قادمة:</b>\n" + "\n".join([f"🟡 {n}" for n in warning_news]) + "\n"

                msg = (
                    f"🥇 <b>فرصة تريد ذهب — {trade['pair']}</b>\n━━━━━━━━━━━━━━━━\n"
                    f"📊 الإشارة: <b>{trade['direction']}</b>\n"
                    f"💪 القوة: <b>{strength_text}</b>\n"
                    f"⏱ مؤكدة على: <b>{tfs_text}</b>\n"
                    f"🕐 Session: <b>{trade.get('session')}</b>\n"
                    f"📐 السلسلة: Sweep ✅ → Displacement BOS ✅ → Pullback (OB/FVG) ✅ → Confirmation ✅\n"
                    f"🎯 مصدر الهدف: <b>{trade.get('target_source')}</b>\n"
                    f"{news_warning}"
                    f"\n💰 السعر الحالي: <b>${trade['price']}</b>\n"
                    f"🎯 TP: <b>${trade['tp']}</b>\n🛑 SL: <b>${trade['sl']}</b>\n"
                    f"⚖️ R/R: <b>1:{trade['rr']}</b>\n\n"
                    f"━━━━━━━━━━━━━━━━\n🕐 {now_str}\n\n"
                    f"[مرحلة التعلم — بلا فلتر/رفض] واش بغيتي تدخل هاد التريد؟"
                )

                pending_trades[pair] = trade
                last_signal[pair] = {"direction": current_direction, "bos_level": current_bos_level}
                send_with_buttons(msg, trade)

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(60)

if __name__ == "__main__":
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()
    main_loop()
