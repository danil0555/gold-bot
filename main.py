import os
import asyncio
import aiohttp
import sqlite3
import yfinance as yf
import pandas as pd
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiohttp import web

# Токен берется из настроек Koyeb (Environment Variables)
BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, sma50 REAL, sma200 REAL, 
                  forecast TEXT, target_price REAL, user_confirmed INTEGER)''')
    conn.commit()
    conn.close()

# --- АНАЛИЗ ---
def get_analysis():
    try:
        # Скачиваем данные золота за 1 месяц с интервалом 1 час
        data = yf.download("GC=F", period="1mo", interval="1h", progress=False)
        if data.empty:
            return None
        
        # Считаем RSI (14)
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        data['RSI'] = 100 - (100 / (1 + rs))
        
        # Считаем SMA (50 и 200)
        data['SMA50'] = data['Close'].rolling(window=50).mean()
        data['SMA200'] = data['Close'].rolling(window=200).mean()
        
        last_row = data.iloc[-1]
        price = float(last_row['Close'].iloc[0]) if isinstance(last_row['Close'], pd.Series) else float(last_row['Close'])
        rsi = float(last_row['RSI'].iloc[0]) if isinstance(last_row['RSI'], pd.Series) else float(last_row['RSI'])
        sma50 = float(last_row['SMA50'].iloc[0]) if isinstance(last_row['SMA50'], pd.Series) else float(last_row['SMA50'])
        sma200 = float(last_row['SMA200'].iloc[0]) if isinstance(last_row['SMA200'], pd.Series) else float(last_row['SMA200'])
        
        # Логика сигнала
        trend = "BULL" if sma50 > sma200 else "BEAR"
        
        if trend == "BULL" and rsi < 40:
            forecast, target = "🟢 СИЛЬНЫЙ ВВЕРХ (покупать)", price + 10.0
        elif trend == "BEAR" and rsi > 60:
            forecast, target = "🔴 СИЛЬНЫЙ ВНИЗ (продавать)", price - 10.0
        elif rsi < 30 and trend == "BEAR":
            forecast, target = "🟡 ОСТОРОЖНО ВВЕРХ (отскок)", price + 5.0
        else:
            forecast, target = "⚪️ НЕЙТРАЛЬНО (ждать)", price
            
        return price, rsi, sma50, sma200, forecast, target
    except Exception as e:
        print(f"Ошибка анализа: {e}")
        return None

# --- КОМАНДЫ БОТА ---
@dp.message(CommandStart())
async def start_command(message: types.Message):
    await message.answer(f"Привет, {message.from_user.first_name}! Я готов к анализу. Отправь /signal.")

@dp.message(Command("signal"))
async def signal_command(message: types.Message):
    await bot.send_chat_action(message.chat.id, 'typing')
    result = get_analysis()
    if not result:
        await message.answer("Ошибка получения данных. Попробуйте позже.")
        return
        
    price, rsi, sma50, sma200, forecast, target = result
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    # Сохраняем в БД
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("INSERT INTO history (timestamp, price, rsi, sma50, sma200, forecast, target_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (now, price, rsi, sma50, sma200, forecast, target))
    signal_id = c.lastrowid
    conn.commit()
    conn.close()

    text = (f"📊 **Анализ XAUUSD** (Сигнал #{signal_id})\n\n"
            f"Цена: **${price:.2f}**\n"
            f"RSI: {rsi:.2f} | Тренд: {'Вверх' if sma50 > sma200 else 'Вниз'}\n"
            f"Прогноз: **{forecast}**\n"
            f"Цель: **${target:.2f}**")
    
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Подтверждаю", callback_data=f"confirm_{signal_id}"),
         types.InlineKeyboardButton(text="❌ Отклоняю", callback_data=f"reject_{signal_id}")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

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
    c.execute("SELECT id, timestamp, price, forecast, user_confirmed FROM history ORDER BY id DESC LIMIT 5")
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await message.answer("История пуста.")
        return
        
    text = "📚 **Последние сигналы:**\n\n"
    for row in rows:
        sig_id, ts, price, forecast, conf = row
        reaction = "⏳ Ожидает" if conf is None else ("✅ Подтверждено" if conf == 1 else "❌ Отклонено")
        text += f"#{sig_id} · {ts}\nЦена: ${price:.2f}\nПрогноз: {forecast}\nРеакция: {reaction}\n\n"
    await message.answer(text, parse_mode="Markdown")

# --- ВЕБ-СЕРВЕР ДЛЯ KOYEB (чтобы сервер не засыпал) ---
async def handle(request):
    return web.Response(text="Bot is running!")

async def main():
    init_db()
    # Запускаем веб-сервер
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    # Запускаем бота
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
