import asyncio
import sqlite3
import pandas as pd
import feedparser
import requests
from openai import OpenAI
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command

# ВАШИ КЛЮЧИ
BOT_TOKEN = "8990920545:AAFbVp_UNRg3a-_9xIFEaLdTc5J4Oyru0NM"
OPENROUTER_KEY = "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"

ai_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

AUTO_SIGNAL_ENABLED = False
USER_CHAT_ID = None

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    # Таблица истории сигналов
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, forecast TEXT, 
                  target_price REAL, stop_loss REAL, result TEXT)''')
    # Таблица для самообучения (веса факторов)
    c.execute('''CREATE TABLE IF NOT EXISTS weights
                 (factor TEXT PRIMARY KEY, weight REAL)''')
    # Инициализация начальных весов
    for factor in ['RSI', 'TREND', 'AI_NEWS']:
        c.execute("INSERT OR IGNORE INTO weights (factor, weight) VALUES (?, ?)", (factor, 1.0))
    conn.commit()
    conn.close()

# --- ПОЛУЧЕНИЕ ТОЧНОЙ ЦЕНЫ (REAL-TIME) ---
def get_live_price_and_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    # Берем минутные данные, чтобы цена была максимально точной
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m"
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        result = data['chart']['result'][0]
        timestamps = result['timestamp']
        quotes = result['indicators']['quote'][0]
        
        # Собираем в DataFrame
        df = pd.DataFrame({
            'Open': quotes['open'],
            'High': quotes['high'],
            'Low': quotes['low'],
            'Close': quotes['close']
        }, index=pd.to_datetime(timestamps, unit='s'))
        
        df = df.dropna()
        return df
    except Exception as e:
        print(f"Ошибка получения цены: {e}")
        return pd.DataFrame()

# --- УМНЫЙ АНАЛИЗ С УЧЕТОМ САМООБУЧЕНИЯ ---
def get_analysis():
    try:
        df = get_live_price_and_data()
        if df.empty: return None

        # Расчет индикаторов на минутных данных
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['RSI'] = 100 - (100 / (1 + (gain / loss)))
        
        df['TR'] = pd.concat([df['High'] - df['Low'],
                              (df['High'] - df['Close'].shift()).abs(),
                              (df['Low'] - df['Close'].shift()).abs()], axis=1).max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()

        current_price = float(df.iloc[-1]['Close'])
        current_rsi = float(df.iloc[-1]['RSI'])
        current_atr = float(df.iloc[-1]['ATR'])

        # Получаем текущие веса из базы (самообучение)
        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("SELECT factor, weight FROM weights")
        weights = dict(c.fetchall())
        conn.close()

        score = 0.0
        reasons = []

        # Фактор 1: RSI
        if current_rsi < 30:
            score += 2 * weights.get('RSI', 1.0)
            reasons.append(f"RSI<30 (+{2 * weights['RSI']:.1f})")
        elif current_rsi > 70:
            score -= 2 * weights.get('RSI', 1.0)
            reasons.append(f"RSI>70 (-{2 * weights['RSI']:.1f})")

        # Фактор 2: Тренд (по 50-минутной средней)
        trend = "BULL" if df['Close'].iloc[-1] > df['Close'].rolling(50).mean().iloc[-1] else "BEAR"
        if trend == "BULL":
            score += 1 * weights.get('TREND', 1.0)
            reasons.append(f"Тренд↑ (+{1 * weights['TREND']:.1f})")
        else:
            score -= 1 * weights.get('TREND', 1.0)
            reasons.append(f"Тренд↓ (-{1 * weights['TREND']:.1f})")

        # Фактор 3: Новости от ИИ
        # (Для экономии токенов вызываем реже, но для примера оставим)
        news = "Новостей нет." 
        try:
            feed = feedparser.parse("https://finance.yahoo.com/rss/2.0/headline?s=GC=F")
            if feed.entries: news = feed.entries[0].title
        except: pass
        
        if news != "Новостей нет.":
            response = ai_client.chat.completions.create(
                model="meta-llama/llama-3-8b-instruct",
                messages=[{"role": "user", "content": f"Оцени новость для золота от -5 до +5. '{news}'. Ответь только цифрой."}],
                temperature=0.1, max_tokens=5
            )
            import re
            match = re.search(r'[-+]?\d+', response.choices[0].message.content)
            ai_score = int(match.group()) if match else 0
            if ai_score != 0:
                score += ai_score * weights.get('AI_NEWS', 1.0)
                reasons.append(f"ИИ Новости ({ai_score * weights['AI_NEWS']:+.1f})")

        # Итоговый прогноз
        if score >= 0:
            forecast = "🟢 ВВЕРХ"
            target = current_price + (1.5 * current_atr)
            stop_loss = current_price - (1.0 * current_atr)
        else:
            forecast = "🔴 ВНИЗ"
            target = current_price - (1.5 * current_atr)
            stop_loss = current_price + (1.0 * current_atr)

        support = float(df['Low'].tail(60).min())
        resistance = float(df['High'].tail(60).max())

        return current_price, current_rsi, forecast, target, stop_loss, support, resistance, " | ".join(reasons), news
    except Exception as e:
        print(f"Ошибка анализа: {e}")
        return None

# --- ОТПРАВКА СИГНАЛА ---
async def send_signal(chat_id, is_auto=False):
    try:
        await bot.send_chat_action(chat_id, 'typing')
        result = await asyncio.wait_for(asyncio.to_thread(get_analysis), timeout=30.0)
        if not result:
            if not is_auto: await bot.send_message(chat_id, "⚠️ Ошибка данных. Попробуйте позже.")
            return

        price, rsi, forecast, target, sl, support, resistance, reasons, news = result
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("INSERT INTO history (timestamp, price, rsi, forecast, target_price, stop_loss) VALUES (?, ?, ?, ?, ?, ?)",
                  (now_str, price, rsi, forecast, target, sl))
        signal_id = c.lastrowid
        conn.commit()
        conn.close()

        text = (f"📊 **Анализ XAUUSD** {'(АВТО)' if is_auto else ''} #{signal_id}\n\n"
                f"Текущая цена: **${price:.2f}** | RSI: {rsi:.1f}\n"
                f"🎯 TP: **${target:.2f}** | 🛑 SL: **${sl:.2f}**\n\n"
                f"🧠 **Факторы:**\n{reasons}\n\n"
                f"🛡️ **Ликвидность:**\nПоддержка: ${support:.2f} | Сопротивление: ${resistance:.2f}\n\n"
                f"📰 **Новость:**\n_{news}_\n\n"
                f"Прогноз: **{forecast}**\n"
                f"⏳ _Сделка активна (следим за TP/SL)_")

        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ Подтверждаю", callback_data=f"confirm_{signal_id}"),
             types.InlineKeyboardButton(text="❌ Отклоняю", callback_data=f"reject_{signal_id}")]
        ])
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        print(f"Ошибка отправки: {e}")

# --- ФОНОВАЯ ПРОВЕРКА (САМООБУЧЕНИЕ) ---
async def background_worker():
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    while True:
        try:
            # 1. Отправка нового сигнала (если включен авто-режим)
            if AUTO_SIGNAL_ENABLED and USER_CHAT_ID:
                await send_signal(USER_CHAT_ID, is_auto=True)
                await asyncio.sleep(5 * 60) # Ждем 5 минут
            else:
                await asyncio.sleep(10)

            # 2. Проверка старых прогнозов (Самообучение)
            conn = sqlite3.connect('signals.db')
            c = conn.cursor()
            # Ищем прогнозы, которые еще не проверены
            c.execute("SELECT id, timestamp, price, target_price, stop_loss, forecast FROM history WHERE result IS NULL")
            pending = c.fetchall()

            if pending:
                current_data = await asyncio.to_thread(get_live_price_and_data)
                if not current_data.empty:
                    current_data.index = current_data.index.tz_localize(None)
                    
                    for sig_id, sig_time, old_price, tp, sl, forecast in pending:
                        sig_dt = pd.to_datetime(sig_time)
                        recent_data = current_data[current_data.index >= sig_dt]
                        
                        if recent_data.empty: continue
                        high_price = recent_data['High'].max()
                        low_price = recent_data['Low'].min()
                        current_close = recent_data['Close'].iloc[-1]
                        
                        result_text = ""
                        
                        # Проверка на WIN или LOSS
                        if "ВВЕРХ" in forecast:
                            if high_price >= tp: result_text = f"WIN ✅ (TP ${tp:.2f})"
                            elif low_price <= sl: result_text = f"LOSS ❌ (SL ${sl:.2f})"
                            # Умная проверка: если прошло 5 минут, а цена пошла против нас
                            elif (datetime.now() - sig_dt).total_seconds() > 300 and current_close < old_price:
                                result_text = f"LOSS ❌ (Цена пошла против нас)"
                        elif "ВНИЗ" in forecast:
                            if low_price <= tp: result_text = f"WIN ✅ (TP ${tp:.2f})"
                            elif high_price >= sl: result_text = f"LOSS ❌ (SL ${sl:.2f})"
                            elif (datetime.now() - sig_dt).total_seconds() > 300 and current_close > old_price:
                                result_text = f"LOSS ❌ (Цена пошла против нас)"

                        # Если сигнал закрылся — обновляем базу и учимся
                        if result_text:
                            c.execute("UPDATE history SET result = ? WHERE id = ?", (result_text, sig_id))
                            conn.commit()
                            
                            # --- ЛОГИКА САМООБУЧЕНИЯ ---
                            if "LOSS" in result_text:
                                # Если бот ошибся, он снижает вес фактора, который привел к ошибке
                                # (В упрощенном виде: снижаем вес тренда или RSI, если они совпали с прогнозом)
                                if "Тренд" in str(reasons): 
                                    c.execute("UPDATE weights SET weight = MAX(0.2, weight - 0.1) WHERE factor = 'TREND'")
                                if "RSI" in str(reasons): 
                                    c.execute("UPDATE weights SET weight = MAX(0.2, weight - 0.1) WHERE factor = 'RSI'")
                            else:
                                # Если WIN — повышаем уверенность в этих факторах
                                if "Тренд" in str(reasons): 
                                    c.execute("UPDATE weights SET weight = MIN(2.0, weight + 0.1) WHERE factor = 'TREND'")
                                if "RSI" in str(reasons): 
                                    c.execute("UPDATE weights SET weight = MIN(2.0, weight + 0.1) WHERE factor = 'RSI'")
                            conn.commit()

                            if USER_CHAT_ID:
                                await bot.send_message(USER_CHAT_ID, f"🏁 **Сделка #{sig_id} ЗАКРЫТА**\nРезультат: **{result_text}**\n\n🧠 *Бот проанализировал ошибку и скорректировал веса.*")
            conn.close()
        except Exception as e:
            print(f"Ошибка фона: {e}")
        await asyncio.sleep(30)

# --- КОМАНДЫ БОТА ---
@dp.message(CommandStart())
async def start_command(message: types.Message):
    global USER_CHAT_ID
    USER_CHAT_ID = message.chat.id
    await message.answer(f"Привет! Бот v5.0 (Самообучающийся) готов.\n/signal - точный прогноз\n/autostart - авто-режим\n/history - история\n/weights - показать текущие веса факторов")

@dp.message(Command("autostart"))
async def autostart_command(message: types.Message):
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    AUTO_SIGNAL_ENABLED = True
    USER_CHAT_ID = message.chat.id
    await message.answer("🚀 Авто-режим ВКЛЮЧЕН. Бот будет присылать сигналы каждые 5 минут и учиться на ошибках.")

@dp.message(Command("autostop"))
async def autostop_command(message: types.Message):
    global AUTO_SIGNAL_ENABLED
    AUTO_SIGNAL_ENABLED = False
    await message.answer("🛑 Авто-режим ВЫКЛЮЧЕН.")

@dp.message(Command("signal"))
async def signal_command(message: types.Message):
    await send_signal(message.chat.id, is_auto=False)

@dp.message(Command("weights"))
async def weights_command(message: types.Message):
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("SELECT factor, weight FROM weights")
    weights = c.fetchall()
    conn.close()
    text = "🧠 **Текущие веса факторов (мозг бота):**\n\n"
    for factor, weight in weights:
        text += f"• {factor}: **{weight:.1f}**\n"
    text += "\n_Чем выше вес, тем больше бот доверяет этому фактору._"
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(lambda c: c.data.startswith("confirm_") or c.data.startswith("reject_"))
async def process_callback(callback_query: types.CallbackQuery):
    action, sig_id = callback_query.data.split("_")
    confirmed = 1 if action == "confirm" else 0
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("UPDATE history SET user_confirmed = ? WHERE id = ?", (confirmed, sig_id))
    conn.commit()
    conn.close()
    await callback_query.message.answer("✅ Принято!" if confirmed else "❌ Отклонено.")
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.answer()

async def main():
    init_db()
    asyncio.create_task(background_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
