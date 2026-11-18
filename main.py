import os
import asyncio
import sqlite3
import yfinance as yf
import pandas as pd
import feedparser
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

AUTO_SIGNAL_ENABLED = False
USER_CHAT_ID = None

# --- БАЗА ДАННЫХ (ОБНОВЛЕННАЯ) ---
def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    # Создаем таблицу с новыми колонками
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, sma50 REAL, sma200 REAL, 
                  forecast TEXT, target_price REAL, user_confirmed INTEGER,
                  check_time TEXT, result TEXT)''')
    
    # Проверяем, есть ли колонка check_time. Если база старая - добавляем её.
    try:
        c.execute("ALTER TABLE history ADD COLUMN check_time TEXT")
    except sqlite3.OperationalError:
        pass # Колонка уже существует
        
    # Проверяем, есть ли колонка result. Если базы старая - добавляем её.
    try:
        c.execute("ALTER TABLE history ADD COLUMN result TEXT")
    except sqlite3.OperationalError:
        pass # Колонка уже существует
        
    conn.commit()
    conn.close()

# --- НОВОСТИ ---
def get_news():
    try:
        feed = feedparser.parse(
            "https://finance.yahoo.com/rss/2.0/headline?s=GC=F&region=US&lang=en-US",
            request_headers={'User-Agent': 'Mozilla/5.0'}
        )
        if feed.entries:
            return feed.entries[0].title
    except Exception:
        pass
    return "Новостей нет."

# --- АНАЛИЗ ---
def get_analysis():
    try:
        df_15m = yf.download("GC=F", period="5d", interval="15m", progress=False, multi_level_index=False, timeout=10)
        if df_15m.empty:
            return None

        try:
            df_1h = yf.download("GC=F", period="1mo", interval="1h", progress=False, multi_level_index=False, timeout=10)
        except:
            df_1h = pd.DataFrame()

        try:
            df_dxy = yf.download("DX=F", period="1mo", interval="1h", progress=False, multi_level_index=False, timeout=10)
        except:
            df_dxy = pd.DataFrame()

        delta = df_15m['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df_15m['RSI'] = 100 - (100 / (1 + rs))
        
        df_15m['TR'] = pd.concat([df_15m['High'] - df_15m['Low'], 
                                  (df_15m['High'] - df_15m['Close'].shift()).abs(), 
                                  (df_15m['Low'] - df_15m['Close'].shift()).abs()], axis=1).max(axis=1)
        df_15m['ATR'] = df_15m['TR'].rolling(window=14).mean()
        
        current_price = float(df_15m.iloc[-1]['Close'])
        current_rsi = float(df_15m.iloc[-1]['RSI'])
        current_atr = float(df_15m.iloc[-1]['ATR']) if not pd.isna(df_15m.iloc[-1]['ATR']) else 3.0
        
        trend_1h = "NEUTRAL"
        if not df_1h.empty and len(df_1h) > 50:
            df_1h['SMA50'] = df_1h['Close'].rolling(window=50).mean()
            trend_1h = "BULL" if df_1h.iloc[-1]['Close'] > df_1h.iloc[-1]['SMA50'] else "BEAR"
        
        trend_dxy = "NEUTRAL"
        if not df_dxy.empty and len(df_dxy) > 50:
            df_dxy['SMA50'] = df_dxy['Close'].rolling(window=50).mean()
            trend_dxy = "BULL" if df_dxy.iloc[-1]['Close'] > df_dxy.iloc[-1]['SMA50'] else "BEAR"

        score = 0
        reasons = []

        if current_rsi < 30:
            score += 2
            reasons.append("RSI<30 (+2)")
        elif current_rsi > 70:
            score -= 2
            reasons.append("RSI>70 (-2)")

        if trend_1h == "BULL":
            score += 1
            reasons.append("Тренд 1Ч↑ (+1)")
        elif trend_1h == "BEAR":
            score -= 1
            reasons.append("Тренд 1Ч↓ (-1)")

        if trend_dxy == "BULL":
            score -= 1
            reasons.append("Доллар↑ (-1)")
        elif trend_dxy == "BEAR":
            score += 1
            reasons.append("Доллар↓ (+1)")

        news = get_news()
        news_lower = news.lower()
        if any(w in news_lower for w in ["war", "conflict", "crisis", "inflation", "recession"]):
            score += 1
            reasons.append("Новости↑ (+1)")
        elif any(w in news_lower for w in ["hike", "strong dollar", "growth", "surge"]):
            score -= 1
            reasons.append("Новости↓ (-1)")

        if score > 0:
            forecast = "🟢 ВВЕРХ"
            target = current_price + (1.5 * current_atr)
        elif score < 0:
            forecast = "🔴 ВНИЗ"
            target = current_price - (1.5 * current_atr)
        else:
            forecast = "⚪️ НЕЙТРАЛЬНО"
            target = current_price

        recent_data = df_15m.tail(48 * 4) 
        support = float(recent_data['Low'].min())
        resistance = float(recent_data['High'].max())

        return current_price, current_rsi, forecast, target, support, resistance, " | ".join(reasons), news
    except Exception as e:
        print(f"Ошибка анализа: {e}")
        return None

# --- ОТПРАВКА СИГНАЛА ---
async def send_signal(chat_id, is_auto=False):
    try:
        await bot.send_chat_action(chat_id, 'typing')
        result = await asyncio.wait_for(asyncio.to_thread(get_analysis), timeout=20.0)
        
        if not result:
            if not is_auto:
                await bot.send_message(chat_id, "⚠️ Ошибка получения данных. Попробуйте позже.")
            return
            
        price, rsi, forecast, target, support, resistance, reasons, news = result
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M")
        check_time_str = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        
        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("INSERT INTO history (timestamp, price, rsi, forecast, target_price, check_time) VALUES (?, ?, ?, ?, ?, ?)",
                  (now_str, price, rsi, forecast, target, check_time_str))
        signal_id = c.lastrowid
        conn.commit()
        conn.close()

        text = (f"📊 **Анализ XAUUSD** {'(АВТО)' if is_auto else ''} #{signal_id}\n\n"
                f"Цена: **${price:.2f}** | RSI: {rsi:.1f}\n"
                f"🎯 Цель: **${target:.2f}**\n\n"
                f"🧠 **Факторы:**\n{reasons if reasons else 'Нет явных сигналов'}\n\n"
                f"🛡️ **Ликвидность:**\nПоддержка: ${support:.2f}\nСопротивление: ${resistance:.2f}\n\n"
                f"📰 **Новость:**\n_{news}_\n\n"
                f"Прогноз: **{forecast}**\n"
                f"⏳ _Проверка через 5 минут_")
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ Подтверждаю", callback_data=f"confirm_{signal_id}"),
             types.InlineKeyboardButton(text="❌ Отклоняю", callback_data=f"reject_{signal_id}")]
        ])
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
        
    except asyncio.TimeoutError:
        if not is_auto:
            await bot.send_message(chat_id, "⚠️ Тайм-аут: сервер данных не ответил. Попробуйте еще раз.")
    except Exception as e:
        print(f"Ошибка в send_signal: {e}")
        if not is_auto:
            await bot.send_message(chat_id, f"⚠️ Произошла ошибка: {e}")

# --- ФОНОВЫЙ ПРОЦЕСС ---
async def background_worker():
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    while True:
        try:
            if AUTO_SIGNAL_ENABLED and USER_CHAT_ID:
                await send_signal(USER_CHAT_ID, is_auto=True)
                await asyncio.sleep(5 * 60) 
            else:
                await asyncio.sleep(10)

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect('signals.db')
            c = conn.cursor()
            c.execute("SELECT id, price, forecast, check_time FROM history WHERE check_time <= ? AND result IS NULL", (now_str,))
            pending = c.fetchall()

            if pending:
                current_data = await asyncio.to_thread(yf.download, "GC=F", period="1d", interval="1m", progress=False, multi_level_index=False)
                if not current_data.empty:
                    current_price = float(current_data['Close'].iloc[-1])
                    
                    for sig_id, old_price, forecast, _ in pending:
                        result_text = ""
                        if "ВВЕРХ" in forecast:
                            result_text = "WIN ✅" if current_price > old_price else "LOSS ❌"
                        elif "ВНИЗ" in forecast:
                            result_text = "WIN ✅" if current_price < old_price else "LOSS ❌"
                        else:
                            result_text = "FLAT ⚪"
                        
                        c.execute("UPDATE history SET result = ? WHERE id = ?", (result_text, sig_id))
                        conn.commit()
                        
                        if USER_CHAT_ID:
                            await bot.send_message(
                                USER_CHAT_ID, 
                                f"🔔 **Проверка #{sig_id}**\n"
                                f"Прогноз: {forecast}\n"
                                f"Вход: ${old_price:.2f} → Сейчас: ${current_price:.2f}\n"
                                f"Результат: **{result_text}**"
                            )
            conn.close()
        except Exception as e:
            print(f"Ошибка фонового процесса: {e}")
        await asyncio.sleep(60)

# --- КОМАНДЫ ---
@dp.message(CommandStart())
async def start_command(message: types.Message):
    global USER_CHAT_ID
    USER_CHAT_ID = message.chat.id
    await message.answer(f"Привет! Я — продвинутая версия бота.\n"
                         f"/signal — ручной прогноз\n"
                         f"/autostart — включить авто-прогнозы\n"
                         f"/autostop — выключить\n"
                         f"/history — история")

@dp.message(Command("autostart"))
async def autostart_command(message: types.Message):
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    AUTO_SIGNAL_ENABLED = True
    USER_CHAT_ID = message.chat.id
    await message.answer("🚀 Авто-режим ВКЛЮЧЕН. Анализ каждые 5 минут.")

@dp.message(Command("autostop"))
async def autostop_command(message: types.Message):
    global AUTO_SIGNAL_ENABLED
    AUTO_SIGNAL_ENABLED = False
    await message.answer("🛑 Авто-режим ВЫКЛЮЧЕН.")

@dp.message(Command("signal"))
async def signal_command(message: types.Message):
    await send_signal(message.chat.id, is_auto=False)

@dp.callback_query(lambda c: c.data.startswith("confirm_") or c.data.startswith("reject_"))
async def process_callback(callback_query: types.CallbackQuery):
    action, sig_id = callback_query.data.split("_")
    confirmed = 1 if action == "confirm" else 0
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("UPDATE history SET user_confirmed = ? WHERE id = ?", (confirmed, sig_id))
    conn.commit()
    conn.close()
    msg = "✅ Принято!" if confirmed else "❌ Отклонено."
    await callback_query.message.answer(msg)
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.answer()

@dp.message(Command("history"))
async def history_command(message: types.Message):
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("SELECT id, timestamp, price, forecast, user_confirmed, result FROM history ORDER BY id DESC LIMIT 5")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await message.answer("История пуста.")
        return
        
    text = "📚 **Последние 5 сигналов:**\n\n"
    for row in rows:
        sig_id, ts, price, forecast, conf, res = row
        reaction = "⏳" if conf is None else ("✅" if conf == 1 else "❌")
        res_text = f" | {res}" if res else " | ⏳ Ждем..."
        text += f"#{sig_id} · {ts}\nЦена: ${price:.2f}\nПрогноз: {forecast} {res_text}\n\n"
    await message.answer(text, parse_mode="Markdown")

# --- ЗАПУСК ---
async def main():
    init_db()
    asyncio.create_task(background_worker())
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
