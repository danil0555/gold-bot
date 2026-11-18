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

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, sma50 REAL, sma200 REAL, 
                  forecast TEXT, target_price REAL, user_confirmed INTEGER,
                  check_time TEXT, result TEXT)''')
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
        # Для 5-минутного интервала берем 5m свечи
        data = yf.download("GC=F", period="5d", interval="5m", progress=False, multi_level_index=False)
        if data.empty:
            return None
        
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        data['RSI'] = 100 - (100 / (1 + rs))
        
        data['SMA50'] = data['Close'].rolling(window=50).mean()
        data['SMA200'] = data['Close'].rolling(window=200).mean()
        
        last_row = data.iloc[-1]
        price = float(last_row['Close'])
        rsi = float(last_row['RSI'])
        sma50 = float(last_row['SMA50'])
        sma200 = float(last_row['SMA200'])
        
        recent_data = data.tail(24)
        support = float(recent_data['Low'].min())
        resistance = float(recent_data['High'].max())
        
        # --- ЛОГИКА: ВСЕГДА ДАЕМ ПРОГНОЗ ---
        if rsi < 35:
            forecast, target = "🟢 ВВЕРХ (отскок)", price + 3.0
        elif rsi > 65:
            forecast, target = "🔴 ВНИЗ (коррекция)", price - 3.0
        elif price > sma50:
            forecast, target = "🟢 ВВЕРХ (по тренду)", price + 3.0
        else:
            forecast, target = "🔴 ВНИЗ (по тренду)", price - 3.0
            
        return price, rsi, sma50, sma200, forecast, target, support, resistance
    except Exception as e:
        print(f"Ошибка анализа: {e}")
        return None

# --- ОТПРАВКА СИГНАЛА ---
async def send_signal(chat_id, is_auto=False):
    await bot.send_chat_action(chat_id, 'typing')
    result = get_analysis()
    if not result:
        return
        
    price, rsi, sma50, sma200, forecast, target, support, resistance = result
    news = get_news()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M")
    # Меняем время проверки на +5 минут
    check_time_str = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("INSERT INTO history (timestamp, price, rsi, sma50, sma200, forecast, target_price, check_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (now_str, price, rsi, sma50, sma200, forecast, target, check_time_str))
    signal_id = c.lastrowid
    conn.commit()
    conn.close()

    text = (f"📊 **Анализ XAUUSD** {'(АВТО)' if is_auto else ''} (Сигнал #{signal_id})\n\n"
            f"Цена: **${price:.2f}**\n"
            f"RSI: {rsi:.2f} | Тренд: {'Вверх' if price > sma200 else 'Вниз'}\n\n"
            f"🛡️ **Уровни (2ч):**\n"
            f"Поддержка: ${support:.2f} | Сопротивление: ${resistance:.2f}\n\n"
            f"📰 **Новость:**\n_{news}_\n\n"
            f"Прогноз: **{forecast}**\n"
            f"Цель: **${target:.2f}**\n"
            f"⏳ _Проверка через 5 минут_")
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Подтверждаю", callback_data=f"confirm_{signal_id}"),
         types.InlineKeyboardButton(text="❌ Отклоняю", callback_data=f"reject_{signal_id}")]
    ])
    await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")

# --- ФОНОВЫЙ ПРОЦЕСС ---
async def background_worker():
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    while True:
        try:
            if AUTO_SIGNAL_ENABLED and USER_CHAT_ID:
                await send_signal(USER_CHAT_ID, is_auto=True)
                # Ждем 5 минут (300 секунд)
                await asyncio.sleep(5 * 60) 
            else:
                await asyncio.sleep(10)

            # Проверяем сигналы
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect('signals.db')
            c = conn.cursor()
            c.execute("SELECT id, price, forecast, check_time FROM history WHERE check_time <= ? AND result IS NULL", (now_str,))
            pending = c.fetchall()

            if pending:
                current_data = yf.download("GC=F", period="1d", interval="1m", progress=False, multi_level_index=False)
                if not current_data.empty:
                    current_price = float(current_data['Close'].iloc[-1])
                    
                    for sig_id, old_price, forecast, _ in pending:
                        result_text = ""
                        if "ВВЕРХ" in forecast:
                            result_text = "WIN ✅" if current_price > old_price else "LOSS ❌"
                        elif "ВНИЗ" in forecast:
                            result_text = "WIN ✅" if current_price < old_price else "LOSS ❌"
                        
                        c.execute("UPDATE history SET result = ? WHERE id = ?", (result_text, sig_id))
                        conn.commit()
                        
                        if USER_CHAT_ID:
                            await bot.send_message(
                                USER_CHAT_ID, 
                                f"🔔 **Проверка #{sig_id}**\n"
                                f"Было: {forecast}\n"
                                f"Вход: ${old_price:.2f} → Сейчас: ${current_price:.2f}\n"
                                f"Результат: **{result_text}**"
                            )
            conn.close()
        except Exception as e:
            print(f"Ошибка: {e}")
        await asyncio.sleep(60)

# --- КОМАНДЫ ---
@dp.message(CommandStart())
async def start_command(message: types.Message):
    global USER_CHAT_ID
    USER_CHAT_ID = message.chat.id
    await message.answer(f"Привет! Бот готов.\n/signal — ручной прогноз\n/autostart — включить прогнозы каждые 5 минут\n/autostop — выключить\n/history — история")

@dp.message(Command("autostart"))
async def autostart_command(message: types.Message):
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    AUTO_SIGNAL_ENABLED = True
    USER_CHAT_ID = message.chat.id
    await message.answer("🚀 Авто-режим ВКЛЮЧЕН. Прогнозы и проверки каждые 5 минут.")

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
