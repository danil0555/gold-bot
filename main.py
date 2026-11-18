import asyncio
import sqlite3
import pandas as pd
import feedparser
import requests
from openai import OpenAI
from datetime import datetime
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
                  target_price REAL, stop_loss REAL, close_price REAL, result TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS weights
                 (factor TEXT PRIMARY KEY, weight REAL)''')
    for factor in ['RSI', 'TREND', 'AI_NEWS']:
        c.execute("INSERT OR IGNORE INTO weights (factor, weight) VALUES (?, ?)", (factor, 1.0))
    conn.commit()
    conn.close()

def get_live_price_and_data():
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=1d&interval=1m"
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
        return df
    except Exception as e:
        print(f"Ошибка получения цены: {e}")
        return pd.DataFrame()

def get_analysis():
    try:
        df = get_live_price_and_data()
        if df.empty: return None

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

        conn = sqlite3.connect('signals.db')
        c = conn.cursor()
        c.execute("SELECT factor, weight FROM weights")
        weights = dict(c.fetchall())
        conn.close()

        score = 0.0
        reasons = []

        if current_rsi < 30:
            score += 2 * weights.get('RSI', 1.0)
            reasons.append(f"RSI<30 (+{2 * weights['RSI']:.1f})")
        elif current_rsi > 70:
            score -= 2 * weights.get('RSI', 1.0)
            reasons.append(f"RSI>70 (-{2 * weights['RSI']:.1f})")

        trend = "BULL" if df['Close'].iloc[-1] > df['Close'].rolling(50).mean().iloc[-1] else "BEAR"
        if trend == "BULL":
            score += 1 * weights.get('TREND', 1.0)
            reasons.append(f"Тренд↑ (+{1 * weights['TREND']:.1f})")
        else:
            score -= 1 * weights.get('TREND', 1.0)
            reasons.append(f"Тренд↓ (-{1 * weights['TREND']:.1f})")

        news = "Новостей нет." 
        try:
            feed = feedparser.parse("https://finance.yahoo.com/rss/2.0/headline?s=GC=F")
            if feed.entries: news = feed.entries[0].title
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
                reasons.append(f"ИИ Новости ({ai_score * weights['AI_NEWS']:+.1f})")

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

        safe_news = news.replace("_", " ").replace("*", " ").replace("[", " ").replace("`", " ")

        text = (f"📊 <b>Анализ XAUUSD</b> {'(АВТО)' if is_auto else ''} #{signal_id}\n\n"
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
                    current_data.index = current_data.index.tz_localize(None)
                    
                    for sig_id, sig_time, old_price, tp, sl, forecast in pending:
                        sig_dt = pd.to_datetime(sig_time)
                        recent_data = current_data[current_data.index >= sig_dt]
                        
                        if recent_data.empty: continue
                        high_price = recent_data['High'].max()
                        low_price = recent_data['Low'].min()
                        current_close = recent_data['Close'].iloc[-1]
                        
                        result_text = ""
                        close_price = 0.0
                        
                        # Логика закрытия
                        if "ВВЕРХ" in forecast:
                            if high_price >= tp: 
                                result_text = "WIN ✅"
                                close_price = tp
                            elif low_price <= sl: 
                                result_text = "LOSS ❌"
                                close_price = sl
                            elif (datetime.now() - sig_dt).total_seconds() > 300 and current_close < old_price:
                                result_text = "LOSS ❌ (По времени)"
                                close_price = current_close
                        elif "ВНИЗ" in forecast:
                            if low_price <= tp: 
                                result_text = "WIN ✅"
                                close_price = tp
                            elif high_price >= sl: 
                                result_text = "LOSS ❌"
                                close_price = sl
                            elif (datetime.now() - sig_dt).total_seconds() > 300 and current_close > old_price:
                                result_text = "LOSS ❌ (По времени)"
                                close_price = current_close

                        if result_text:
                            c.execute("UPDATE history SET result = ?, close_price = ? WHERE id = ?", (result_text, close_price, sig_id))
                            conn.commit()
                            
                            if "LOSS" in result_text:
                                c.execute("UPDATE weights SET weight = MAX(0.2, weight - 0.1) WHERE factor = 'TREND'")
                                c.execute("UPDATE weights SET weight = MAX(0.2, weight - 0.1) WHERE factor = 'RSI'")
                            else:
                                c.execute("UPDATE weights SET weight = MIN(2.0, weight + 0.1) WHERE factor = 'TREND'")
                                c.execute("UPDATE weights SET weight = MIN(2.0, weight + 0.1) WHERE factor = 'RSI'")
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
            [KeyboardButton(text="🧠 Веса"), KeyboardButton(text="📜 История сделок")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )
    return keyboard

@dp.message(CommandStart())
async def start_command(message: types.Message):
    global USER_CHAT_ID
    USER_CHAT_ID = message.chat.id
    await message.answer("Привет! Бот готов к работе. Используй меню ниже для управления.", reply_markup=get_main_keyboard())

@dp.message(lambda message: message.text == "📈 Прогноз" or message.text == "/signal")
async def signal_handler(message: types.Message):
    await send_signal(message.chat.id, is_auto=False)

@dp.message(lambda message: message.text == "🚀 Вкл Авто" or message.text == "/autostart")
async def autostart_handler(message: types.Message):
    global AUTO_SIGNAL_ENABLED, USER_CHAT_ID
    AUTO_SIGNAL_ENABLED = True
    USER_CHAT_ID = message.chat.id
    await message.answer("🚀 Авто-режим ВКЛЮЧЕН.", reply_markup=get_main_keyboard())

@dp.message(lambda message: message.text == "🛑 Выкл Авто" or message.text == "/autostop")
async def autostop_handler(message: types.Message):
    global AUTO_SIGNAL_ENABLED
    AUTO_SIGNAL_ENABLED = False
    await message.answer("🛑 Авто-режим ВЫКЛЮЧЕН.", reply_markup=get_main_keyboard())

@dp.message(lambda message: message.text == "🧠 Веса" or message.text == "/weights")
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

# --- НОВАЯ ФУНКЦИЯ СТАТИСТИКИ ---
@dp.message(lambda message: message.text == "📜 История сделок" or message.text == "/history")
async def history_handler(message: types.Message):
    conn = sqlite3.connect('signals.db')
    c = conn.cursor()
    c.execute("SELECT id, timestamp, price, close_price, forecast, result FROM history WHERE result IS NOT NULL ORDER BY id DESC")
    trades = c.fetchall()
    conn.close()

    if not trades:
        await message.answer("📭 История пуста. Пока нет закрытых сделок.", reply_markup=get_main_keyboard())
        return

    total = len(trades)
    wins = sum(1 for t in trades if "WIN" in t[5])
    losses = total - wins
    winrate = (wins / total) * 100 if total > 0 else 0

    # Симуляция торговли (фиксированный лот)
    # Если WIN: зарабатываем разницу между TP и входом, но мы берем фиксированную прибыль за сделку +1%
    # Если LOSS: теряем -0.5% (соотношение 1 к 2)
    # (Упрощенная математика, чтобы показать относительную доходность)
    balance_usd = 0.0
    for t in trades:
        if "WIN" in t[5]:
            # Прибыль = (Цена выхода - Цена входа) / Цена входа
            profit_pct = abs(t[3] - t[2]) / t[2]
            balance_usd += profit_pct # Симуляция в "процентах от 1 доллара"
        else:
            loss_pct = abs(t[3] - t[2]) / t[2]
            balance_usd -= loss_pct

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
        text += f"{icon} #{t[0]} | Вход: ${t[2]:.2f} -> Выход: ${t[3]:.2f}\n"

    await message.answer(text, parse_mode="HTML", reply_markup=get_main_keyboard())

async def main():
    init_db()
    asyncio.create_task(background_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
