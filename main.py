import asyncio
import sqlite3
import pandas as pd
import feedparser
import requests
from openai import OpenAI
from datetime import datetime
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

def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, forecast TEXT, 
                  target_price REAL, stop_loss REAL, user_confirmed INTEGER, result TEXT)''')
    conn.commit()
    conn.close()

def get_ai_news_score(news_text):
    if not OPENROUTER_KEY or not news_text or news_text == "Новостей нет.":
        return 0, news_text
    prompt = f"Ты трейдер по золоту. Оцени новость от -5 (плохо) до +5 (хорошо). Новость: '{news_text}'. Ответь только цифрой."
    try:
        response = ai_client.chat.completions.create(
            model="meta-llama/llama-3-8b-instruct",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=5
        )
        import re
        match = re.search(r'[-+]?\d+', response.choices[0].message.content)
        return (max(-5, min(5, int(match.group()))) if match else 0), news_text
    except:
        return 0, news_text

def get_news():
    try:
        feed = feedparser.parse("https://finance.yahoo.com/rss/2.0/headline?s=GC=F&region=US&lang=en-US")
        if feed.entries:
            return feed.entries[0].title
    except:
        pass
    return "Новостей нет."

# ПРЯМОЙ ЗАПРОС К YAHOO БЕЗ БИБЛИОТЕКИ YFINANCE
def get_gold_data():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    }
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1h"
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        data = response.json()
        
        if 'chart' in data and 'result' in data['chart'] and data['chart']['result'] is not None:
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            closes = result['indicators']['quote'][0]['close']
            
            df = pd.DataFrame({'Close': closes}, index=pd.to_datetime(timestamps, unit='s'))
            df = df.dropna()
            return df
    except Exception as e:
        print(f"Ошибка Yahoo API: {e}")
    return pd.DataFrame()

def get_analysis():
    try:
        df = get_gold_data()
        if df.empty:
            print("Нет данных от Yahoo API")
            return None

        # RSI
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df['RSI'] = 100 - (100 / (1 + (gain / loss)))
        
        # Имитация High/Low для расчета ATR (чтобы не усложнять)
        df['High'] = df['Close'] * 1.002
        df['Low'] = df['Close'] * 0.998
        df['TR'] = pd.concat([df['High'] - df['Low'],
                              (df['High'] - df['Close'].shift()).abs(),
                              (df['Low'] - df['Close'].shift()).abs()], axis=1).max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()

        current_price = float(df.iloc[-1]['Close'])
        current_rsi = float(df.iloc[-1]['RSI'])
        current_atr = float(df.iloc[-1]['ATR'])

        score = 0
        reasons = []

        if current_rsi < 30:
            score += 2
            reasons.append("RSI<30 (+2)")
        elif current_rsi > 70:
            score -= 2
            reasons.append("RSI>70 (-2)")

        trend = "BULL" if df['Close'].iloc[-1] > df['Close'].rolling(50).mean().iloc[-1] else "BEAR"
        if trend == "BULL":
            score += 1
            reasons.append("Тренд↑ (+1)")
        else:
            score -= 1
            reasons.append("Тренд↓ (-1)")

        news = get_news()
        ai_score, news = get_ai_news_score(news)
        if ai_score != 0:
            score += ai_score
            reasons.append(f"ИИ ({ai_score:+})")

        if score >= 0:
            forecast = "🟢 ВВЕРХ"
            target = current_price + (1.5 * current_atr)
            stop_loss = current_price - (1.0 * current_atr)
        else:
            forecast = "🔴 ВНИЗ"
            target = current_price - (1.5 * current_atr)
            stop_loss = current_price + (1.0 * current_atr)

        support = float(df['Low'].tail(48).min())
        resistance = float(df['High'].tail(48).max())

        return current_price, current_rsi, forecast, target, stop_loss, support, resistance, " | ".join(reasons), news
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
                f"Цена: **${price:.2f}** | RSI: {rsi:.1f}\n"
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

async def background_worker():
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    while True:
        try:
            if AUTO_SIGNAL_ENABLED and USER_CHAT_ID:
                await send_signal(USER_CHAT_ID, is_auto=True)
                await asyncio.sleep(5 * 60)
            else:
                await asyncio.sleep(10)
        except Exception as e:
            print(f"Ошибка фона: {e}")
        await asyncio.sleep(30)

@dp.message(CommandStart())
async def start_command(message: types.Message):
    global USER_CHAT_ID
    USER_CHAT_ID = message.chat.id
    await message.answer(f"Привет! Бот v4.0 готов.\n/signal - прогноз\n/autostart - авто-режим\n/history - история")

@dp.message(Command("autostart"))
async def autostart_command(message: types.Message):
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    AUTO_SIGNAL_ENABLED = True
    USER_CHAT_ID = message.chat.id
    await message.answer("🚀 Авто-режим ВКЛЮЧЕН.")

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
    await callback_query.message.answer("✅ Принято!" if confirmed else "❌ Отклонено.")
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.answer()

async def main():
    init_db()
    asyncio.create_task(background_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
