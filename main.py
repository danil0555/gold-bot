import os
import asyncio
import sqlite3
import yfinance as yf
import pandas as pd
import feedparser
from openai import OpenAI
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command

# ВСТАВЬТЕ СВОИ КЛЮЧИ СЮДА МЕЖДУ КАВЫЧКАМИ
BOT_TOKEN = "ТУТ_ВАШ_ТОКЕН_ОТ_BOTFATHER"
OPENROUTER_KEY = "ТУТ_ВАШ_КЛЮЧ_sk-or-v1-..."

# Настройка ИИ
ai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

AUTO_SIGNAL_ENABLED = False
USER_CHAT_ID = None

def init_db():
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  timestamp TEXT, price REAL, rsi REAL, sma50 REAL, sma200 REAL, 
                  forecast TEXT, target_price REAL, stop_loss REAL, user_confirmed INTEGER,
                  check_time TEXT, result TEXT)''')
    try: c.execute("ALTER TABLE history ADD COLUMN check_time TEXT")
    except: pass
    try: c.execute("ALTER TABLE history ADD COLUMN result TEXT")
    except: pass
    try: c.execute("ALTER TABLE history ADD COLUMN stop_loss REAL")
    except: pass
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
            temperature=0.1,
            max_tokens=5
        )
        content = response.choices[0].message.content.strip()
        import re
        match = re.search(r'[-+]?\d+', content)
        if match:
            score = int(match.group())
            score = max(-5, min(5, score))
        else:
            score = 0
        return score, news_text
    except Exception as e:
        print(f"Ошибка ИИ: {e}")
        return 0, news_text

def get_news():
    try:
        feed = feedparser.parse("https://finance.yahoo.com/rss/2.0/headline?s=GC=F&region=US&lang=en-US")
        if feed.entries:
            return feed.entries[0].title
    except Exception:
        pass
    return "Новостей нет."

def find_smc_zones(df):
    df['Body'] = abs(df['Close'] - df['Open'])
    df['ATR_Base'] = (df['High'] - df['Low']).rolling(14).mean()
    df['Is_OB'] = df['Body'] > (df['ATR_Base'] * 1.5)
    obs = df[df['Is_OB']].tail(5)
    current_price = df.iloc[-1]['Close']
    nearest_ob = None
    min_dist = 9999
    for index, row in obs.iterrows():
        ob_price = (row['High'] + row['Low']) / 2 
        dist = abs(current_price - ob_price)
        if dist < min_dist:
            min_dist = dist
            nearest_ob = ob_price
    return nearest_ob

def get_analysis():
    try:
        df_15m = yf.download("GC=F", period="5d", interval="15m", progress=False, multi_level_index=False, timeout=10)
        if df_15m.empty: return None

        df_1h = yf.download("GC=F", period="1mo", interval="1h", progress=False, multi_level_index=False, timeout=10)
        df_dxy = yf.download("DX=F", period="1mo", interval="1h", progress=False, multi_level_index=False, timeout=10)

        delta = df_15m['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        df_15m['RSI'] = 100 - (100 / (1 + (gain / loss)))
        
        df_15m['TR'] = pd.concat([df_15m['High'] - df_15m['Low'], (df_15m['High'] - df_15m['Close'].shift()).abs(), (df_15m['Low'] - df_15m['Close'].shift()).abs()], axis=1).max(axis=1)
        df_15m['ATR'] = df_15m['TR'].rolling(window=14).mean()
        df_15m['SMA50'] = df_15m['Close'].rolling(window=50).mean()
        
        current_price = float(df_15m.iloc[-1]['Close'])
        current_rsi = float(df_15m.iloc[-1]['RSI'])
        current_atr = float(df_15m.iloc[-1]['ATR'])
        
        trend_1h = "BULL" if df_1h['Close'].iloc[-1] > df_1h['Close'].rolling(50).mean().iloc[-1] else "BEAR"
        trend_dxy = "BULL" if df_dxy['Close'].iloc[-1] > df_dxy['Close'].rolling(50).mean().iloc[-1] else "BEAR"
        
        nearest_ob = find_smc_zones(df_15m)

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
        ai_score, news = get_ai_news_score(news)
        if ai_score != 0:
            score += ai_score
            reasons.append(f"ИИ Новости ({ai_score:+})")

        if nearest_ob:
            dist_to_ob = abs(current_price - nearest_ob)
            if dist_to_ob < (current_atr * 2):
                if current_price > nearest_ob:
                    score += 1 
                    reasons.append("SMC Отскок (+1)")
                else:
                    score -= 1 
                    reasons.append("SMC Пробой (-1)")

        if score >= 0:
            forecast = "🟢 ВВЕРХ"
            target = current_price + (1.5 * current_atr)
            stop_loss = current_price - (1.0 * current_atr)
        else:
            forecast = "🔴 ВНИЗ"
            target = current_price - (1.5 * current_atr)
            stop_loss = current_price + (1.0 * current_atr)

        support = float(df_15m.tail(48 * 4)['Low'].min())
        resistance = float(df_15m.tail(48 * 4)['High'].max())

        return current_price, current_rsi, forecast, target, stop_loss, support, resistance, " | ".join(reasons), news, nearest_ob
    except Exception as e:
        print(f"Ошибка анализа: {e}")
        return None

async def send_signal(chat_id, is_auto=False):
    try:
        await bot.send_chat_action(chat_id, 'typing')
        result = await asyncio.wait_for(asyncio.to_thread(get_analysis), timeout=25.0)
        
        if not result:
            if not is_auto: await bot.send_message(chat_id, "⚠️ Ошибка данных.")
            return
            
        price, rsi, forecast, target, sl, support, resistance, reasons, news, ob = result
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M")
        
        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("INSERT INTO history (timestamp, price, rsi, forecast, target_price, stop_loss) VALUES (?, ?, ?, ?, ?, ?)", (now_str, price, rsi, forecast, target, sl))
        signal_id = c.lastrowid
        conn.commit()
        conn.close()

        text = (f"📊 **Анализ XAUUSD** {'(АВТО)' if is_auto else ''} #{signal_id}\n\n"
                f"Цена: **${price:.2f}** | RSI: {rsi:.1f}\n"
                f"🎯 TP: **${target:.2f}** | 🛑 SL: **${sl:.2f}**\n\n"
                f"🧠 **Факторы:**\n{reasons}\n\n"
                f"🛡️ **Ликвидность:**\nПоддержка: ${support:.2f} | Сопротивление: ${resistance:.2f}\n"
                f"👤 **SMC Зона:** {f'${ob:.2f}' if ob else 'Нет рядом'}\n\n"
                f"📰 **Новость:**\n_{news}_\n\n"
                f"Прогноз: **{forecast}**\n"
                f"⏳ _Сделка активна (следим за TP/SL)_")
        
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="✅ Подтверждаю", callback_data=f"confirm_{signal_id}"), types.InlineKeyboardButton(text="❌ Отклоняю", callback_data=f"reject_{signal_id}")]
        ])
        await bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode="Markdown")
        
    except Exception as e:
        print(f"Ошибка: {e}")

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
                current_data = await asyncio.to_thread(yf.download, "GC=F", period="1d", interval="1m", progress=False, multi_level_index=False)
                if not current_data.empty:
                    for sig_id, sig_time, old_price, tp, sl, forecast in pending:
                        sig_dt = pd.to_datetime(sig_time)
                        recent_data = current_data[current_data.index >= sig_dt]
                        if recent_data.empty: continue

                        high_price = recent_data['High'].max()
                        low_price = recent_data['Low'].min()
                        result_text = ""

                        if "ВВЕРХ" in forecast:
                            if high_price >= tp: result_text = f"WIN ✅ (TP ${tp:.2f})"
                            elif low_price <= sl: result_text = f"LOSS ❌ (SL ${sl:.2f})"
                        elif "ВНИЗ" in forecast:
                            if low_price <= tp: result_text = f"WIN ✅ (TP ${tp:.2f})"
                            elif high_price >= sl: result_text = f"LOSS ❌ (SL ${sl:.2f})"

                        if result_text:
                            c.execute("UPDATE history SET result = ? WHERE id = ?", (result_text, sig_id))
                            conn.commit()
                            if USER_CHAT_ID:
                                await bot.send_message(USER_CHAT_ID, f"🏁 **Сделка #{sig_id} ЗАКРЫТА**\nРезультат: **{result_text}**")
            conn.close()
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
