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

# ВАШИ КЛЮЧИ
BOT_TOKEN = "8990920545:AAFbVp_UNRg3a-_9xIFEaLdTc5J4Oyru0NM"
OPENROUTER_KEY = "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"

ai_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

AUTO_SIGNAL_ENABLED = False
USER_CHAT_ID = None

def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, forecast TEXT, 
                  target_price REAL, stop_loss REAL, close_price REAL, result TEXT,
                  confidence INTEGER)''')
    try:
        c.execute("ALTER TABLE history ADD COLUMN close_price REAL")
    except sqlite3.OperationalError: pass
    try:
        c.execute("ALTER TABLE history ADD COLUMN confidence INTEGER")
    except sqlite3.OperationalError: pass
    
    c.execute('''CREATE TABLE IF NOT EXISTS weights
                 (factor TEXT PRIMARY KEY, weight REAL)''')
    factors = ['RSI', 'TREND', 'AI_NEWS', 'MACD', 'DIVERGENCE', 'SMC_SWEEP', 'BREAKER_BLOCK', 'ICT_P3']
    for factor in factors:
        c.execute("INSERT OR IGNORE INTO weights (factor, weight) VALUES (?, ?)", (factor, 1.0))
    conn.commit()
    conn.close()

def get_live_price_and_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1h"
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quotes = result['indicators']['quote'][0]
        
        df = pd.DataFrame({
            'Open': quotes['open'],
            'High': quotes['high'],
            'Low': quotes['low'],
            'Close': quotes['close']
        }, index=pd.to_datetime(timestamps, unit='s'))
        df = df.dropna()
        df.index = df.index.tz_localize(None)
        return df
    except Exception as e:
        print(f"Ошибка получения цены: {e}")
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

def get_analysis():
    try:
        df = get_live_price_and_data()
        if df.empty: return None

        # Индикаторы
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['RSI'] = 100 - (100 / (1 + (gain / loss)))
        df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
        df['EMA200'] = df['Close'].ewm(span=200, adjust=False).mean()
        
        exp12 = df['Close'].ewm(span=12, adjust=False).mean()
        exp26 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp12 - exp26
        df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        df['TR'] = pd.concat([df['High'] - df['Low'],
                              (df['High'] - df['Close'].shift()).abs(),
                              (df['Low'] - df['Close'].shift()).abs()], axis=1).max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()

        current_price = float(df.iloc[-1]['Close'])
        current_rsi = float(df.iloc[-1]['RSI'])
        current_atr = float(df.iloc[-1]['ATR'])
        current_ema200 = float(df.iloc[-1]['EMA200'])
        current_ema50 = float(df.iloc[-1]['EMA50'])
        current_macd = float(df.iloc[-1]['MACD'])
        current_macd_signal = float(df.iloc[-1]['MACD_Signal'])
        prev_macd = float(df.iloc[-2]['MACD'])
        prev_macd_signal = float(df.iloc[-2]['MACD_Signal'])
        
        divergence = detect_rsi_divergence(df)
        sweep_signal = check_liquidity_sweep(df)
        breaker_signal = check_breaker_block(df)
        ict_score, ict_name = get_ict_session_score()

        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("SELECT factor, weight FROM weights")
        weights = dict(c.fetchall())
        conn.close()

        score = 0.0
        max_possible_score = 0.0 # Для расчета процента уверенности
        reasons = [f"Сессия: {ict_name}"]

        # 1. Глобальный тренд (EMA 200) - Макс 1
        max_possible_score += 1 * weights.get('TREND', 1.0)
        if current_price > current_ema200:
            score += 1 * weights.get('TREND', 1.0)
            reasons.append(f"Тренд↑ (+{1 * weights['TREND']:.1f})")
        else:
            score -= 1 * weights.get('TREND', 1.0)
            reasons.append(f"Тренд↓ (-{1 * weights['TREND']:.1f})")

        # 2. Трендовый канал EMA 50 - Макс 0.5
        max_possible_score += 0.5 * weights.get('TREND', 1.0)
        if current_price > current_ema50:
            score += 0.5 * weights.get('TREND', 1.0)
        else:
            score -= 0.5 * weights.get('TREND', 1.0)

        # 3. MACD - Макс 1.5
        max_possible_score += 1.5 * weights.get('MACD', 1.0)
        if prev_macd < prev_macd_signal and current_macd > current_macd_signal:
            score += 1.5 * weights.get('MACD', 1.0)
            reasons.append(f"MACD↑ (+{1.5 * weights['MACD']:.1f})")
        elif prev_macd > prev_macd_signal and current_macd < current_macd_signal:
            score -= 1.5 * weights.get('MACD', 1.0)
            reasons.append(f"MACD↓ (-{1.5 * weights['MACD']:.1f})")

        # 4. RSI - Макс 2
        max_possible_score += 2 * weights.get('RSI', 1.0)
        if current_rsi < 30:
            score += 2 * weights.get('RSI', 1.0)
            reasons.append(f"RSI<30 (+{2 * weights['RSI']:.1f})")
        elif current_rsi > 70:
            score -= 2 * weights.get('RSI', 1.0)
            reasons.append(f"RSI>70 (-{2 * weights['RSI']:.1f})")

        # 5. Дивергенция - Макс 2.5
        max_possible_score += 2.5 * weights.get('DIVERGENCE', 1.0)
        if divergence == 1:
            score += 2.5 * weights.get('DIVERGENCE', 1.0)
            reasons.append(f"Дивергенция↑ (+{2.5 * weights['DIVERGENCE']:.1f})")
        elif divergence == -1:
            score -= 2.5 * weights.get('DIVERGENCE', 1.0)
            reasons.append(f"Дивергенция↓ (-{2.5 * weights['DIVERGENCE']:.1f})")

        # 6. SMC Liquidity Sweep - Макс 3
        max_possible_score += 3.0 * weights.get('SMC_SWEEP', 1.0)
        if sweep_signal == 1:
            score += 3.0 * weights.get('SMC_SWEEP', 1.0)
            reasons.append(f"SMC Sweep↑ (+{3.0 * weights['SMC_SWEEP']:.1f})")
        elif sweep_signal == -1:
            score -= 3.0 * weights.get('SMC_SWEEP', 1.0)
            reasons.append(f"SMC Sweep↓ (-{3.0 * weights['SMC_SWEEP']:.1f})")

        # 7. SMC Breaker Block - Макс 2
        max_possible_score += 2.0 * weights.get('BREAKER_BLOCK', 1.0)
        if breaker_signal == 1:
            score += 2.0 * weights.get('BREAKER_BLOCK', 1.0)
            reasons.append(f"Breaker Block↑ (+{2.0 * weights['BREAKER_BLOCK']:.1f})")
        elif breaker_signal == -1:
            score -= 2.0 * weights.get('BREAKER_BLOCK', 1.0)
            reasons.append(f"Breaker Block↓ (-{2.0 * weights['BREAKER_BLOCK']:.1f})")

        # 8. ICT Power of 3 (Макс 1.0)
        max_possible_score += 1.0 * weights.get('ICT_P3', 1.0)
        score += ict_score * weights.get('ICT_P3', 1.0)

        # 9. ИИ Новости (Макс 5.0)
        max_possible_score += 5.0 * weights.get('AI_NEWS', 1.0)
        news = "Новостей нет." 
        try:
            feed = feedparser.parse("https://finance.yahoo.com/rss/2.0/headline?s=GC=F")
            if feed.entries: 
                raw_news = feed.entries[0].title
                keywords = ["gold", "xau", "fed", "interest rate", "inflation", "dollar", "fomc", "powell"]
                if any(kw in raw_news.lower() for kw in keywords):
                    news = raw_news
        except: pass
        
        if news != "Новостей нет.":
            response = ai_client.chat.completions.create(
                model="meta-llama/llama-3.1-8b-instruct",
                messages=[{"role": "user", "content": f"Оцени новость для золота от -5 до +5. '{news}'. Ответь только цифрой."}],
                temperature=0.1, max_tokens=5
            )
            import re
            match = re.search(r'[-+]?\d+', response.choices[0].message.content)
            ai_score = int(match.group()) if match else 0
            if ai_score != 0:
                score += ai_score * weights.get('AI_NEWS', 1.0)
                reasons.append(f"ИИ ({ai_score * weights['AI_NEWS']:+.1f})")

        # --- РАСЧЕТ ПРОЦЕНТА УВЕРЕННОСТИ ---
        # Берем абсолютное значение набранных баллов и делим на максимум
        confidence = int((abs(score) / max_possible_score) * 100)
        if confidence > 100: confidence = 100 # Ограничитель

        # Итоговый прогноз
        if score >= 0:
            forecast = "🟢 ВВЕРХ"
            target = current_price + (1.5 * current_atr)
            stop_loss = current_price - (1.0 * current_atr)
        else:
            forecast = "🔴 ВНИЗ"
            target = current_price - (1.5 * current_atr)
            stop_loss = current_price + (1.0 * current_atr)

        support = float(df['Low'].tail(24).min())
        resistance = float(df['High'].tail(24).max())

        return current_price, current_rsi, forecast, target, stop_loss, support, resistance, " | ".join(reasons), news, confidence
    except Exception as e:
        print(f"Ошибка анализа: {e}")
        return None

async def send_signal(chat_id, is_auto=False):
    try:
        await bot.send_chat_action(chat_id, 'typing')
        result = await asyncio.wait_for(asyncio.to_thread(get_analysis), timeout=30.0)
        if not result:
            if not is_auto: await bot.send_message(chat_id, "⚠️ Ошибка данных. Попробуйте позже.")
            return

        price, rsi, forecast, target, sl, support, resistance, reasons, news, confidence = result
        
        # --- ЛОГИКА ДЛЯ АВТО-РЕЖИМА (СНАЙПЕР) ---
        if is_auto:
            if confidence < 70:
                print(f"Авто-режим: Сигнал пропущен, уверенность {confidence}% < 70%")
                return # Не отправляем сообщение, если уверенность низкая
            else:
                status_text = "🎯 <b>СНАЙПЕР (Высокая уверенность)</b>" if confidence >= 85 else "⚡️ <b>Сильный сигнал</b>"
                await bot.send_message(chat_id, status_text)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("INSERT INTO history (timestamp, price, rsi, forecast, target_price, stop_loss, confidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (now_str, price, rsi, forecast, target, sl, confidence))
        signal_id = c.lastrowid
        conn.commit()
        conn.close()

        safe_news = news.replace("_", " ").replace("*", " ").replace("[", " ").replace("`", " ")
        
        # Красивое отображение уверенности
        conf_bar = "🟩" * (confidence // 20) + "⬜️" * (5 - (confidence // 20))

        text = (f"📊 <b>Анализ XAUUSD</b> {'(АВТО)' if is_auto else ''} #{signal_id}\n"
                f"🔮 Уверенность: <b>{confidence}%</b> {conf_bar}\n\n"
                f"Цена входа: <b>${price:.2f}</b> | RSI: {rsi:.1f}\n"
                f"🎯 TP: <b>${target:.2f}</b> | 🛑 SL: <b>${sl:.2f}</b>\n\n"
                f"🧠 <b>Факторы:</b>\n{reasons}\n\n"
                f"🛡️ <b>Ликвидность:</b>\nПоддержка: ${support:.2f} | Сопротивление: ${resistance:.2f}\n\n"
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
                await send_signal(USER_CHAT_ID, is_auto=True)
                await asyncio.sleep(5 * 60) 
            else:
                await asyncio.sleep(10)

            conn = sqlite3.connect('signals.db')
            c = conn.cursor()
            c.execute("SELECT id, timestamp, price, target_price, stop_loss, forecast FROM history WHERE result IS NULL")
            pending = c.fetchall()

            if pending:
                current_data = await asyncio.to_thread(get_live_price_and_data)
                if not current_data.empty:
                    for sig_id, sig_time, old_price, tp, sl, forecast in pending:
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
                        elif "ВНИЗ" in forecast:
                            if low_price <= tp: result_text, close_price = "WIN ✅", tp
                            elif high_price >= sl: result_text, close_price = "LOSS ❌", sl

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
                                msg = (f"🏁 <b>Сделка #{sig_id} ЗАКРЫТА</b>\n"
                                       f"Цена входа: <b>${old_price:.2f}</b>\n"
                                       f"Цена выхода: <b>${close_price:.2f}</b>\n"
                                       f"Результат: <b>{result_text}</b>\n\n"
                                       f"🧠 <i>Бот обновил свои веса.</i>")
                                await bot.send_message(USER_CHAT_ID, msg, parse_mode="HTML")
            conn.close()
        except Exception as e:
            print(f"Ошибка фона: {e}")
        await asyncio.sleep(30)

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📈 Прогноз")],
            [KeyboardButton(text="🚀 Вкл Авто"), KeyboardButton(text="🛑 Выкл Авто")],
            [KeyboardButton(text="🧠 Веса"), KeyboardButton(text="📜 История")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )
    return keyboard

@dp.message(CommandStart())
async def start_command(message: types.Message):
    global USER_CHAT_ID
    USER_CHAT_ID = message.chat.id
    await message.answer("Привет! Бот (Снайпер) готов к работе. В авто-режиме он пришлет сигнал только если уверен на 70% и выше.", reply_markup=get_main_keyboard())

@dp.message(lambda message: "Прогноз" in message.text or message.text == "/signal")
async def signal_handler(message: types.Message):
    await send_signal(message.chat.id, is_auto=False)

@dp.message(lambda message: "Вкл Авто" in message.text or message.text == "/autostart")
async def autostart_handler(message: types.Message):
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    AUTO_SIGNAL_ENABLED = True
    USER_CHAT_ID = message.chat.id
    await message.answer("🚀 Авто-режим ВКЛЮЧЕН. Бот будет присылать только сигналы с уверенностью >70%.", reply_markup=get_main_keyboard())

@dp.message(lambda message: "Выкл Авто" in message.text or message.text == "/autostop")
async def autostop_handler(message: types.Message):
    global AUTO_SIGNAL_ENABLED
    AUTO_SIGNAL_ENABLED = False
    await message.answer("🛑 Авто-режим ВЫКЛЮЧЕН.", reply_markup=get_main_keyboard())

@dp.message(lambda message: "Веса" in message.text or message.text == "/weights")
async def weights_handler(message: types.Message):
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("SELECT factor, weight FROM weights")
    weights = c.fetchall()
    conn.close()
    text = "🧠 <b>Текущие веса факторов:</b>\n\n"
    for factor, weight in weights:
        text += f"• {factor}: <b>{weight:.1f}</b>\n"
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

@dp.message(lambda message: "История" in message.text or "history" in message.text.lower() or message.text == "/history")
async def history_handler(message: types.Message):
    try:
        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("SELECT id, timestamp, price, close_price, forecast, result, confidence FROM history WHERE result IS NOT NULL ORDER BY id DESC")
        trades = c.fetchall()
        conn.close()

        if not trades:
            await message.answer("📭 История пуста. Пока нет закрытых сделок.", reply_markup=get_main_keyboard())
            return

        total = len(trades)
        wins = sum(1 for t in trades if "WIN" in t[5])
        losses = total - wins
        winrate = (wins / total) * 100 if total > 0 else 0

        balance_usd = 0.0
        for t in trades:
            entry_price = t[2] if t[2] is not None else 0.0
            exit_price = t[3] if t[3] is not None else entry_price
            if entry_price == 0.0: continue
            if "WIN" in t[5]:
                balance_usd += abs(exit_price - entry_price) / entry_price
            else:
                balance_usd -= abs(exit_price - entry_price) / entry_price

        text = (
            f"📜 <b>Статистика торговли</b>\n\n"
            f"Всего сделок: <b>{total}</b>\n"
            f"🟢 Прибыльных: <b>{wins}</b>\n"
            f"🔴 Убыточных: <b>{losses}</b>\n"
            f"🎯 Винрейт: <b>{winrate:.1f}%</b>\n\n"
            f"💰 <b>Симуляция ($1 на сделку):</b>\n"
            f"Чистый результат: <b>${balance_usd:.4f}</b>\n\n"
            f"<i>Последние 5 сделок:</i>\n"
        )

        for t in trades[:5]:
            icon = "✅" if "WIN" in t[5] else "❌"
            cp = t[3] if t[3] is not None else t[2]
            conf = t[6] if t[6] else 0
            text += f"{icon} #{t[0]} ({conf}%) | ${t[2]:.2f} -> ${cp:.2f}\n"

        await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())
    except Exception as e:
        await message.answer(f"⚠️ Ошибка истории: {e}", reply_markup=get_main_keyboard())

async def main():
    init_db()
    asyncio.create_task(background_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
