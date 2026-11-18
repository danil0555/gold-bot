import asyncio
import sqlite3
import pandas as pd
import feedparser
import requests
from openai import OpenAI
from datetime import datetime
import pytz
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ВАШИ КЛЮЧИ
BOT_TOKEN = "8990920545:AAFbVp_UNRg3a-_9xIFEaLdTc5J4Oyru0NM"
OPENROUTER_KEY = "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"

ai_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Глобальные настройки (в реальном проекте лучше хранить в БД, но для одного пользователя сойдет)
AUTO_SIGNAL_ENABLED = False
USER_CHAT_ID = None
CURRENT_TICKER = "GC=F"
CURRENT_NAME = "Золото (XAUUSD)"
USER_BALANCE = 1000.0
USER_RISK_PCT = 1.0

# Машина состояний для настроек
class UserSettings(StatesGroup):
    waiting_for_balance = State()
    waiting_for_risk = State()

ASSETS = {
    "GC=F": {"name": "Золото (XAUUSD)", "bq": "XAUUSD"},
    "GBPJPY=X": {"name": "Фунт/Йена (GBP/JPY)", "bq": "GBPJPY"},
    "AUDJPY=X": {"name": "Австрал/Йена (AUD/JPY)", "bq": "AUDJPY"}
}

def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, forecast TEXT, 
                  target_price REAL, stop_loss REAL, close_price REAL, result TEXT,
                  confidence INTEGER, ticker TEXT, lot_size REAL)''')
    for col in ["close_price REAL", "confidence INTEGER", "ticker TEXT", "lot_size REAL"]:
        try: c.execute(f"ALTER TABLE history ADD COLUMN {col}")
        except sqlite3.OperationalError: pass
    
    c.execute('''CREATE TABLE IF NOT EXISTS weights
                 (factor TEXT PRIMARY KEY, weight REAL)''')
    factors = ['RSI', 'TREND', 'AI_NEWS', 'MACD', 'DIVERGENCE', 'SMC_SWEEP', 'BREAKER_BLOCK', 'ICT_P3']
    for factor in factors:
        c.execute("INSERT OR IGNORE INTO weights (factor, weight) VALUES (?, ?)", (factor, 1.0))
    conn.commit()
    conn.close()

def get_biquote_data(bq_symbol, interval="1h", limit=200):
    try:
        url = f"https://biquote.io/api/{bq_symbol}/ohlc"
        params = {"interval": interval, "limit": limit}
        resp = requests.get(url, params=params, timeout=10)
        env = resp.json()
        if "bars" not in env or not env["bars"]: return pd.DataFrame()
        df = pd.DataFrame(env["bars"])
        df["openTime"] = pd.to_datetime(df["openTime"])
        df = df.set_index("openTime").sort_index()
        df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
        return df
    except Exception as e:
        print(f"Ошибка biquote {bq_symbol}: {e}")
        return pd.DataFrame()

def detect_rsi_divergence(df):
    if len(df) < 20: return 0
    recent = df.tail(20)
    if recent['Low'].iloc[-1] < recent['Low'].iloc[-5] and recent['RSI'].iloc[-1] > recent['RSI'].iloc[-5]: return 1
    elif recent['High'].iloc[-1] > recent['High'].iloc[-5] and recent['RSI'].iloc[-1] < recent['RSI'].iloc[-5]: return -1
    return 0

def check_liquidity_sweep(df):
    if len(df) < 10: return 0
    last_candle = df.iloc[-1]
    prev_candles = df.iloc[-10:-1]
    local_low = prev_candles['Low'].min()
    local_high = prev_candles['High'].max()
    if last_candle['Low'] < local_low and last_candle['Close'] > local_low: return 1
    elif last_candle['High'] > local_high and last_candle['Close'] < local_high: return -1
    return 0

def check_breaker_block(df):
    if len(df) < 5: return 0
    last_candle = df.iloc[-1]
    for i in range(2, 6):
        candle = df.iloc[-i]
        body = abs(candle['Close'] - candle['Open'])
        atr = df['ATR'].iloc[-i] if 'ATR' in df else 1.0
        if candle['Close'] > candle['Open'] and body > atr * 1.5:
            if last_candle['Low'] <= candle['Close'] and last_candle['Close'] > candle['Open']: return 1
        elif candle['Close'] < candle['Open'] and body > atr * 1.5:
            if last_candle['High'] >= candle['Close'] and last_candle['Close'] < candle['Open']: return -1
    return 0

def get_ict_session_score():
    ny_tz = pytz.timezone('America/New_York')
    now_ny = datetime.now(ny_tz)
    hour = now_ny.hour
    if 20 <= hour or hour < 2: return 0, "Азия"
    elif 2 <= hour < 8: return -0.5, "Лондон"
    elif 8 <= hour < 17: return 1.0, "Нью-Йорк"
    else: return 0, "Закрытие"

def calculate_lot_size(balance, risk_pct, entry, stop, ticker):
    risk_amount = balance * (risk_pct / 100)
    distance = abs(entry - stop)
    if distance == 0: return 0.01
    if "XAU" in ticker:
        pip_value = 1.0
        pip_dist = distance / 0.01
        lot = risk_amount / (pip_dist * pip_value)
    else:
        pip_value = 0.67
        pip_dist = distance / 0.01
        lot = risk_amount / (pip_dist * pip_value)
    return round(max(0.01, min(lot, 10.0)), 2)

def get_analysis(ticker):
    try:
        asset_info = ASSETS.get(ticker, ASSETS["GC=F"])
        bq_sym = asset_info["bq"]
        
        df_1h = get_biquote_data(bq_sym, "1h", 200)
        df_4h = get_biquote_data(bq_sym, "4h", 100)
        if df_1h.empty: return None

        delta = df_1h['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df_1h['RSI'] = 100 - (100 / (1 + (gain / loss)))
        df_1h['EMA50'] = df_1h['Close'].ewm(span=50, adjust=False).mean()
        df_1h['EMA200'] = df_1h['Close'].ewm(span=200, adjust=False).mean()
        
        exp12 = df_1h['Close'].ewm(span=12, adjust=False).mean()
        exp26 = df_1h['Close'].ewm(span=26, adjust=False).mean()
        df_1h['MACD'] = exp12 - exp26
        df_1h['MACD_Signal'] = df_1h['MACD'].ewm(span=9, adjust=False).mean()
        
        df_1h['TR'] = pd.concat([df_1h['High'] - df_1h['Low'],
                              (df_1h['High'] - df_1h['Close'].shift()).abs(),
                              (df_1h['Low'] - df_1h['Close'].shift()).abs()], axis=1).max(axis=1)
        df_1h['ATR'] = df_1h['TR'].rolling(window=14).mean()

        current_price = float(df_1h.iloc[-1]['Close'])
        current_rsi = float(df_1h.iloc[-1]['RSI'])
        current_atr = float(df_1h.iloc[-1]['ATR'])
        current_ema200 = float(df_1h.iloc[-1]['EMA200'])
        current_ema50 = float(df_1h.iloc[-1]['EMA50'])
        current_macd = float(df_1h.iloc[-1]['MACD'])
        current_macd_signal = float(df_1h.iloc[-1]['MACD_Signal'])
        prev_macd = float(df_1h.iloc[-2]['MACD'])
        prev_macd_signal = float(df_1h.iloc[-2]['MACD_Signal'])
        
        divergence = detect_rsi_divergence(df_1h)
        sweep_signal = check_liquidity_sweep(df_1h)
        breaker_signal = check_breaker_block(df_1h)
        ict_score, ict_name = get_ict_session_score()

        trend_4h = 0
        if not df_4h.empty:
            df_4h['EMA50'] = df_4h['Close'].ewm(span=50, adjust=False).mean()
            if df_4h['Close'].iloc[-1] > df_4h['EMA50'].iloc[-1]: trend_4h = 1
            else: trend_4h = -1

        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("SELECT factor, weight FROM weights")
        weights = dict(c.fetchall())
        conn.close()

        score = 0.0
        max_possible_score = 0.0
        reasons = [f"Сессия: {ict_name}", f"4H Тренд: {'↑' if trend_4h > 0 else '↓' if trend_4h < 0 else 'Нет'}"]

        max_possible_score += 1 * weights.get('TREND', 1.0)
        if current_price > current_ema200:
            score += 1 * weights.get('TREND', 1.0); reasons.append(f"Тренд↑ (+{1 * weights['TREND']:.1f})")
        else:
            score -= 1 * weights.get('TREND', 1.0); reasons.append(f"Тренд↓ (-{1 * weights['TREND']:.1f})")

        max_possible_score += 0.5 * weights.get('TREND', 1.0)
        if current_price > current_ema50: score += 0.5 * weights.get('TREND', 1.0)
        else: score -= 0.5 * weights.get('TREND', 1.0)

        max_possible_score += 1.5 * weights.get('MACD', 1.0)
        if prev_macd < prev_macd_signal and current_macd > current_macd_signal:
            score += 1.5 * weights.get('MACD', 1.0); reasons.append(f"MACD↑ (+{1.5 * weights['MACD']:.1f})")
        elif prev_macd > prev_macd_signal and current_macd < current_macd_signal:
            score -= 1.5 * weights.get('MACD', 1.0); reasons.append(f"MACD↓ (-{1.5 * weights['MACD']:.1f})")

        max_possible_score += 2 * weights.get('RSI', 1.0)
        if current_rsi < 30:
            score += 2 * weights.get('RSI', 1.0); reasons.append(f"RSI<30 (+{2 * weights['RSI']:.1f})")
        elif current_rsi > 70:
            score -= 2 * weights.get('RSI', 1.0); reasons.append(f"RSI>70 (-{2 * weights['RSI']:.1f})")

        max_possible_score += 2.5 * weights.get('DIVERGENCE', 1.0)
        if divergence == 1:
            score += 2.5 * weights.get('DIVERGENCE', 1.0); reasons.append(f"Дивергенция↑ (+{2.5 * weights['DIVERGENCE']:.1f})")
        elif divergence == -1:
            score -= 2.5 * weights.get('DIVERGENCE', 1.0); reasons.append(f"Дивергенция↓ (-{2.5 * weights['DIVERGENCE']:.1f})")

        max_possible_score += 3.0 * weights.get('SMC_SWEEP', 1.0)
        if sweep_signal == 1:
            score += 3.0 * weights.get('SMC_SWEEP', 1.0); reasons.append(f"SMC Sweep↑ (+{3.0 * weights['SMC_SWEEP']:.1f})")
        elif sweep_signal == -1:
            score -= 3.0 * weights.get('SMC_SWEEP', 1.0); reasons.append(f"SMC Sweep↓ (-{3.0 * weights['SMC_SWEEP']:.1f})")

        max_possible_score += 2.0 * weights.get('BREAKER_BLOCK', 1.0)
        if breaker_signal == 1:
            score += 2.0 * weights.get('BREAKER_BLOCK', 1.0); reasons.append(f"Breaker Block↑ (+{2.0 * weights['BREAKER_BLOCK']:.1f})")
        elif breaker_signal == -1:
            score -= 2.0 * weights.get('BREAKER_BLOCK', 1.0); reasons.append(f"Breaker Block↓ (-{2.0 * weights['BREAKER_BLOCK']:.1f})")

        max_possible_score += 1.0 * weights.get('ICT_P3', 1.0)
        score += ict_score * weights.get('ICT_P3', 1.0)

        max_possible_score += 5.0 * weights.get('AI_NEWS', 1.0)
        news = "Новостей нет." 
        try:
            feed = feedparser.parse("https://finance.yahoo.com/rss/2.0/headline?s=GC=F")
            if feed.entries: 
                raw_news = feed.entries[0].title
                keywords = ["gold", "xau", "fed", "interest rate", "inflation", "dollar", "fomc", "powell", "gbp", "jpy", "yen", "pound", "boj", "boe"]
                if any(kw in raw_news.lower() for kw in keywords): news = raw_news
        except: pass
        
        if news != "Новостей нет.":
            response = ai_client.chat.completions.create(
                model="meta-llama/llama-3.1-8b-instruct",
                messages=[{"role": "user", "content": f"Оцени новость для {ticker} от -5 до +5. '{news}'. Ответь только цифрой."}],
                temperature=0.1, max_tokens=5
            )
            import re
            match = re.search(r'[-+]?\d+', response.choices[0].message.content)
            ai_score = int(match.group()) if match else 0
            if ai_score != 0:
                score += ai_score * weights.get('AI_NEWS', 1.0)
                reasons.append(f"ИИ ({ai_score * weights['AI_NEWS']:+.1f})")

        if trend_4h == 1 and score < 0:
            score *= 0.5; reasons.append("⚠️ 4H против")
        elif trend_4h == -1 and score > 0:
            score *= 0.5; reasons.append("⚠️ 4H против")

        confidence = int((abs(score) / max_possible_score) * 100)
        if confidence > 100: confidence = 100

        if score >= 0:
            forecast = "🟢 ВВЕРХ"
            target = current_price + (1.5 * current_atr)
            stop_loss = current_price - (1.0 * current_atr)
        else:
            forecast = "🔴 ВНИЗ"
            target = current_price - (1.5 * current_atr)
            stop_loss = current_price + (1.0 * current_atr)

        lot = calculate_lot_size(USER_BALANCE, USER_RISK_PCT, current_price, stop_loss, ticker)
        support = float(df_1h['Low'].tail(24).min())
        resistance = float(df_1h['High'].tail(24).max())

        return current_price, current_rsi, forecast, target, stop_loss, support, resistance, " | ".join(reasons), news, confidence, lot
    except Exception as e:
        print(f"Ошибка анализа {ticker}: {e}")
        return None

async def send_signal(chat_id, ticker, is_auto=False):
    try:
        await bot.send_chat_action(chat_id, 'typing')
        result = await asyncio.wait_for(asyncio.to_thread(get_analysis, ticker), timeout=30.0)
        if not result:
            if not is_auto: await bot.send_message(chat_id, "⚠️ Ошибка данных. Попробуйте позже.")
            return

        price, rsi, forecast, target, sl, support, resistance, reasons, news, confidence, lot = result
        asset_name = ASSETS.get(ticker, {}).get("name", ticker)
        
        if is_auto:
            if confidence < 70: return
            else:
                status_text = "🎯 <b>СНАЙПЕР (Высокая уверенность)</b>" if confidence >= 85 else "⚡️ <b>Сильный сигнал</b>"
                await bot.send_message(chat_id, status_text)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("INSERT INTO history (timestamp, price, rsi, forecast, target_price, stop_loss, confidence, ticker, lot_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (now_str, price, rsi, forecast, target, sl, confidence, ticker, lot))
        signal_id = c.lastrowid
        conn.commit()
        conn.close()

        safe_news = news.replace("_", " ").replace("*", " ").replace("[", " ").replace("`", " ")
        conf_bar = "🟩" * (confidence // 20) + "⬜️" * (5 - (confidence // 20))

        text = (f"📊 <b>Анализ {asset_name}</b> {'(АВТО)' if is_auto else ''} #{signal_id}\n"
                f"🔮 Уверенность: <b>{confidence}%</b> {conf_bar}\n\n"
                f"Цена входа: <b>{price:.5f}</b> | RSI: {rsi:.1f}\n"
                f"🎯 TP: <b>{target:.5f}</b> | 🛑 SL: <b>{sl:.5f}</b>\n"
                f"📦 Лот (при риске {USER_RISK_PCT}%): <b>{lot}</b>\n\n"
                f"🧠 <b>Факторы:</b>\n{reasons}\n\n"
                f"🛡️ <b>Ликвидность:</b>\nПоддержка: {support:.5f} | Сопротивление: {resistance:.5f}\n\n"
                f"📰 <b>Новость:</b>\n<i>{safe_news}</i>\n\n"
                f"Прогноз: <b>{forecast}</b>\n"
                f"⏳ <i>Сделка активна (следим за TP/SL)</i>")

        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка отправки: {e}")

async def background_worker():
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    while True:
        try:
            if AUTO_SIGNAL_ENABLED and USER_CHAT_ID:
                for ticker in ASSETS.keys():
                    await send_signal(USER_CHAT_ID, ticker, is_auto=True)
                    await asyncio.sleep(2)
                await asyncio.sleep(5 * 60) 
            else:
                await asyncio.sleep(10)

            conn = sqlite3.connect('signals.db')
            c = conn.cursor()
            c.execute("SELECT id, timestamp, price, target_price, stop_loss, forecast, ticker FROM history WHERE result IS NULL")
            pending = c.fetchall()

            if pending:
                for sig_id, sig_time, old_price, tp, sl, forecast, ticker in pending:
                    bq_sym = ASSETS.get(ticker, {}).get("bq", "XAUUSD")
                    current_data = await asyncio.to_thread(get_biquote_data, bq_sym, "1m", 60)
                    if current_data.empty: continue
                    
                    sig_dt = pd.to_datetime(sig_time)
                    recent_data = current_data[current_data.index >= sig_dt]
                    if recent_data.empty: continue
                    
                    high_price = recent_data['High'].max()
                    low_price = recent_data['Low'].min()
                    
                    result_text = ""
                    close_price = 0.0
                    
                    if "ВВЕРХ" in forecast:
                        if high_price >= tp: result_text, close_price = "WIN ✅", tp
                        elif low_price <= sl: result_text, close_price = "LOSS ❌", sl
                        elif high_price >= (old_price + (tp - old_price) * 0.5):
                            new_sl = max(sl, old_price)
                            if new_sl > sl:
                                c.execute("UPDATE history SET stop_loss = ? WHERE id = ?", (new_sl, sig_id))
                                conn.commit()
                                sl = new_sl
                    elif "ВНИЗ" in forecast:
                        if low_price <= tp: result_text, close_price = "WIN ✅", tp
                        elif high_price >= sl: result_text, close_price = "LOSS ❌", sl
                        elif low_price <= (old_price - (old_price - tp) * 0.5):
                            new_sl = min(sl, old_price)
                            if new_sl < sl:
                                c.execute("UPDATE history SET stop_loss = ? WHERE id = ?", (new_sl, sig_id))
                                conn.commit()
                                sl = new_sl

                    if result_text:
                        c.execute("UPDATE history SET result = ?, close_price = ? WHERE id = ?", (result_text, close_price, sig_id))
                        conn.commit()
                        
                        all_factors = ['TREND', 'RSI', 'MACD', 'DIVERGENCE', 'SMC_SWEEP', 'BREAKER_BLOCK', 'ICT_P3']
                        if "LOSS" in result_text:
                            for factor in all_factors:
                                c.execute("UPDATE weights SET weight = MAX(0.2, weight - 0.1) WHERE factor = ?", (factor,))
                        else:
                            for factor in all_factors:
                                c.execute("UPDATE weights SET weight = MIN(2.0, weight + 0.1) WHERE factor = ?", (factor,))
                        conn.commit()

                        if USER_CHAT_ID:
                            msg = (f"🏁 <b>Сделка #{sig_id} ({ticker}) ЗАКРЫТА</b>\n"
                                   f"Вход: <b>{old_price:.5f}</b> → Выход: <b>{close_price:.5f}</b>\n"
                                   f"Результат: <b>{result_text}</b>\n\n"
                                   f"🧠 <i>Бот обновил веса.</i>")
                            await bot.send_message(USER_CHAT_ID, msg, parse_mode="HTML")
            conn.close()
        except Exception as e:
            print(f"Ошибка фона: {e}")
        await asyncio.sleep(30)

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📈 Прогноз")],
            [KeyboardButton(text="🚀 Вкл Авто"), KeyboardButton(text="🛑 Выкл Авто")],
            [KeyboardButton(text="🧠 Веса"), KeyboardButton(text="📜 История")],
            [KeyboardButton(text="⚙️ Актив"), KeyboardButton(text="💰 Настройки риска")]
        ],
        resize_keyboard=True
    )

def get_asset_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🥇 Золото")],
            [KeyboardButton(text="🇬🇧 Фунт/Йена")],
            [KeyboardButton(text="🇦🇺 Австрал/Йена")],
            [KeyboardButton(text="⬅️ Назад")]
        ],
        resize_keyboard=True
    )

# --- ОБРАБОТЧИКИ ---
@dp.message(CommandStart())
async def start_command(message: types.Message, state: FSMContext):
    global USER_CHAT_ID
    USER_CHAT_ID = message.chat.id
    await state.clear()
    await message.answer(
        f"Привет! Бот готов.\nДепозит: ${USER_BALANCE:.2f} | Риск: {USER_RISK_PCT}%", 
        reply_markup=get_main_keyboard()
    )

@dp.message(lambda message: "Актив" in message.text)
async def asset_menu_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Выберите актив:", reply_markup=get_asset_keyboard())

@dp.message(lambda message: "Назад" in message.text)
async def back_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Возврат.", reply_markup=get_main_keyboard())

@dp.message(lambda message: "Золото" in message.text)
async def set_gold(message: types.Message, state: FSMContext):
    global CURRENT_TICKER
    CURRENT_TICKER = "GC=F"
    await state.clear()
    await message.answer("Актив: <b>Золото</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(lambda message: "Фунт/Йена" in message.text)
async def set_gbpjpy(message: types.Message, state: FSMContext):
    global CURRENT_TICKER
    CURRENT_TICKER = "GBPJPY=X"
    await state.clear()
    await message.answer("Актив: <b>Фунт/Йена</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(lambda message: "Австрал/Йена" in message.text)
async def set_audjpy(message: types.Message, state: FSMContext):
    global CURRENT_TICKER
    CURRENT_TICKER = "AUDJPY=X"
    await state.clear()
    await message.answer("Актив: <b>Австрал/Йена</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")

# --- НАСТРОЙКИ РИСКА (FSM) ---
@dp.message(lambda message: "Настройки риска" in message.text)
async def risk_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Текущий депозит: <b>${USER_BALANCE:.2f}</b>\nТекущий риск: <b>{USER_RISK_PCT}%</b>\n\n"
        f"Что хотите изменить?\n"
        f"Напишите <code>депозит</code> или <code>риск</code>.",
        parse_mode="HTML"
    )

@dp.message(lambda message: message.text.lower() == "депозит")
async def ask_balance(message: types.Message, state: FSMContext):
    await state.set_state(UserSettings.waiting_for_balance)
    await message.answer("Введите новый размер депозита в USD (например: 1500):")

@dp.message(lambda message: message.text.lower() == "риск")
async def ask_risk(message: types.Message, state: FSMContext):
    await state.set_state(UserSettings.waiting_for_risk)
    await message.answer("Введите процент риска на сделку (например: 2):")

@dp.message(UserSettings.waiting_for_balance)
async def process_balance(message: types.Message, state: FSMContext):
    global USER_BALANCE
    try:
        new_bal = float(message.text)
        USER_BALANCE = new_bal
        await state.clear()
        await message.answer(f"✅ Депозит обновлен: <b>${USER_BALANCE:.2f}</b>", parse_mode="HTML", reply_markup=get_main_keyboard())
    except ValueError:
        await message.answer("Ошибка. Введите число (например: 1000).")

@dp.message(UserSettings.waiting_for_risk)
async def process_risk(message: types.Message, state: FSMContext):
    global USER_RISK_PCT
    try:
        new_risk = float(message.text)
        if 0 < new_risk <= 10:
            USER_RISK_PCT = new_risk
            await state.clear()
            await message.answer(f"✅ Риск обновлен: <b>{USER_RISK_PCT}%</b>", parse_mode="HTML", reply_markup=get_main_keyboard())
        else:
            await message.answer("Риск должен быть от 0.1 до 10 (процентов). Попробуйте снова.")
    except ValueError:
        await message.answer("Ошибка. Введите число (например: 1.5).")

# --- ОСТАЛЬНЫЕ КОМАНДЫ ---
@dp.message(lambda message: "Прогноз" in message.text or message.text == "/signal")
async def signal_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await send_signal(message.chat.id, CURRENT_TICKER, is_auto=False)

@dp.message(lambda message: "Вкл Авто" in message.text or message.text == "/autostart")
async def autostart_handler(message: types.Message, state: FSMContext):
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    AUTO_SIGNAL_ENABLED = True
    USER_CHAT_ID = message.chat.id
    await state.clear()
    await message.answer("🚀 Авто-режим ВКЛЮЧЕН.", reply_markup=get_main_keyboard())

@dp.message(lambda message: "Выкл Авто" in message.text or message.text == "/autostop")
async def autostop_handler(message: types.Message, state: FSMContext):
    global AUTO_SIGNAL_ENABLED
    AUTO_SIGNAL_ENABLED = False
    await state.clear()
    await message.answer("🛑 Авто-режим ВЫКЛЮЧЕН.", reply_markup=get_main_keyboard())

@dp.message(lambda message: "Веса" in message.text or message.text == "/weights")
async def weights_handler(message: types.Message, state: FSMContext):
    await state.clear()
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("SELECT factor, weight FROM weights")
    weights = c.fetchall()
    conn.close()
    text = "🧠 <b>Веса:</b>\n\n"
    for factor, weight in weights:
        text += f"• {factor}: <b>{weight:.1f}</b>\n"
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(lambda message: "История" in message.text or message.text == "/history")
async def history_handler(message: types.Message, state: FSMContext):
    await state.clear()
    try:
        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("SELECT id, timestamp, price, close_price, forecast, result, confidence, ticker, lot_size FROM history WHERE result IS NOT NULL ORDER BY id DESC")
        trades = c.fetchall()
        conn.close()

        if not trades:
            await message.answer("📭 История пуста.", reply_markup=get_main_keyboard())
            return

        total = len(trades)
        wins = sum(1 for t in trades if "WIN" in t[5])
        losses = total - wins
        winrate = (wins / total) * 100 if total > 0 else 0

        balance_pct = 0.0
        for t in trades:
            entry_price = t[2] if t[2] is not None else 0.0
            exit_price = t[3] if t[3] is not None else entry_price
            if entry_price == 0.0: continue
            if "WIN" in t[5]: balance_pct += abs(exit_price - entry_price) / entry_price
            else: balance_pct -= abs(exit_price - entry_price) / entry_price

        text = (
            f"📜 <b>Статистика</b>\n\n"
            f"Всего: <b>{total}</b> | 🟢 {wins} | 🔴 {losses}\n"
            f"🎯 Винрейт: <b>{winrate:.1f}%</b>\n\n"
            f"💰 Симуляция: <b>{balance_pct*100:+.2f}%</b>\n\n"
            f"<i>Последние 5:</i>\n"
        )

        for t in trades[:5]:
            icon = "✅" if "WIN" in t[5] else "❌"
            cp = t[3] if t[3] is not None else t[2]
            conf = t[6] if t[6] else 0
            tick = t[7] if t[7] else "GC=F"
            lot = t[8] if t[8] else 0.01
            text += f"{icon} {tick} ({conf}%) | {t[2]:.3f}→{cp:.3f} | Лот: {lot}\n"

        await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}", reply_markup=get_main_keyboard())

async def main():
    init_db()
    asyncio.create_task(background_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
