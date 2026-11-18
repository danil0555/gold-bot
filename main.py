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
                current_data = await asyncio.to_thread(download_data, "GC=F", "1d", "1m")
                if not current_data.empty:
                    # Убираем часовой пояс из данных Yahoo
                    current_data.index = current_data.index.tz_localize(None)
                    
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
