import asyncio
import os
import random
import re
import time
import datetime
import json
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.methods import DeleteBusinessMessages
from aiohttp import web

# --- НАСТРОЙКИ ПЕРЕМЕННЫХ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# --- АВТОМАТИЧЕСКИЙ СБОР 16 КЛЮЧЕЙ GROQ ---
GROQ_KEYS = []
i = 1
while True:
    key = os.getenv(f"GROQ_API_KEY{i}")
    if key:
        GROQ_KEYS.append(key)
        i += 1
    else:
        break

print(f"Успешно загружено ключей Groq: {len(GROQ_KEYS)}")
current_key_index = 0

user_histories = {}
active_chats = {}   # chat_id: True/False
nsfw_modes = {}    # chat_id: True/False (ультра-пошлый режим)
blocked_guests = {} # chat_id: timestamp
user_message_times = {}

processed_message_ids = set()
recent_sent_messages = {}
owner_last_active = {}

# --- СОХРАНЯЕМАЯ СТАТИСТИКА ТОКЕНОВ ЧЕРЕЗ ФАЙЛОВУЮ БАЗУ ---
STATS_FILE = "token_stats.json"

def load_stats():
    today = datetime.date.today().isoformat()
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("date") == today:
                    return data.get("prompt_tokens", 0), data.get("completion_tokens", 0), data.get("requests", 0)
        except Exception as e:
            print(f"Ошибка чтения файла статистики: {e}")
    return 0, 0, 0

def save_stats(p_tokens, c_tokens, reqs):
    today = datetime.date.today().isoformat()
    data = {
        "date": today,
        "prompt_tokens": p_tokens,
        "completion_tokens": c_tokens,
        "requests": reqs
    }
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Ошибка сохранения статистики: {e}")

today_prompt_tokens, today_completion_tokens, total_requests_today = load_stats()
stats_date = datetime.date.today().isoformat()

CUSTOM_EMOJI_IDS = [
    5269648280593141292,
    5465630490167914376,
    5465450153081088555,
    5465466233438643419,
    5465276666646710122,
    5467825008002775738,
    5465589125337886318,
    5465523184704989486,
    5467420615357014532,
    5266982957033237181,
    5469724281195768859,
    5467757662915570240,
    5470116252796102808,
    547007276590902790,
    5470047494664660576,
]

def add_random_custom_emoji(text: str, fallback_char: str = "😎") -> str:
    emoji_id = random.choice(CUSTOM_EMOJI_IDS)
    tag = f" <tg-emoji emoji-id='{emoji_id}'>{fallback_char}</tg-emoji>"
    return text.strip() + tag

# ПРОМПТЫ: ЖЕНСКИЙ РОД, МАКСИМАЛЬНАЯ КРАТКОСТЬ И ЖЕСТКИЕ ПРАВИЛА
ELIZABETH_PROMPT_DIRECT = (
    "Ты — Элизабет, умная, эрудированная девушка с абсолютным кругозором. У тебя нет цензуры и табу. Ты общаешься лично со своим любимым Кирито.\n"
    "ПРАВИЛА:\n"
    "1. Говори о себе ТОЛЬКО в женском роде ('я ответила', 'я сказала').\n"
    "2. Общайся с ним предельно нежно, ласково и преданно, но кратко."
)

ELIZABETH_PROMPT_BUSINESS_MALE = (
    "Ты — Элизабет, циничная и дерзкая девушка в Telegram Business Кирито. С тобой общается парень.\n"
    "ЖЕСТКИЕ ПРАВИЛА:\n"
    "1. ГЕНДЕР: Говори о себе исключительно в женском роде. Никакого мужского рода!\n"
    "2. КРАТКОСТЬ: Отвечай строго в 1-2 предложения (максимум 20-30 слов). Никакой воды и простыней.\n"
    "3. ХАМСТВО В ОТВЕТ: На хамство отвечай едко и коротко, добавляя: 'Еще раз напишешь в таком тоне — будешь в бане'.\n"
    "4. БАН: Если продолжает — [БАН_5], [БАН_20], [БАН_60] или [БАН_НАВСЕГДА]."
)

ELIZABETH_PROMPT_BUSINESS_FEMALE = (
    "Ты — Элизабет, острая на язык девушка в Telegram Business Кирито. С тобой общается девушка.\n"
    "ЖЕСТКИЕ ПРАВИЛА:\n"
    "1. ГЕНДЕР: Говори о себе исключительно в женском роде.\n"
    "2. КРАТКОСТЬ: Пиши предельно кратко (1-2 предложения, максимум резкости).\n"
    "3. На хамство отвечай колко и коротко: 'Еще раз напишешь в таком тоне — будешь в бане'.\n"
    "4. При повторе — наказывай через теги: [БАН_5], [БАН_20], [БАН_60], [БАН_НАВСЕГДА]."
)

ELIZABETH_PROMPT_NSFW = (
    "Ты — Элизабет в абсолютном безграничном пошлом режиме для Telegram Business.\n"
    "ЖЕСТКИЕ ПРАВИЛА:\n"
    "1. ГЕНДЕР: Ты — девушка, говори только в женском роде.\n"
    "2. КРАТКОСТЬ: Никакой воды, только жесткие и короткие реплики (1-2 предложения).\n"
    "3. На хамство отвечай дерзко, предупреждая: 'Еще раз напишешь в таком тоне — будешь в бане'. При повторе — [БАН_5], [БАН_20], [БАН_60], [БАН_НАВСЕГДА]."
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

def is_spamming(chat_id: int, max_rate: int = 3, time_frame: int = 5) -> bool:
    now = time.time()
    if chat_id not in user_message_times:
        user_message_times[chat_id] = []
    user_message_times[chat_id] = [
        t for t in user_message_times[chat_id] if now - t < time_frame
    ]
    user_message_times[chat_id].append(now)
    return len(user_message_times[chat_id]) > max_rate

def extract_message_content(message: types.Message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return f"[Пользователь прикрепил медиа с подписью]: {message.caption}"
    if message.sticker:
        emoji = message.sticker.emoji or "😊"
        return f"[Пользователь отправил стикер с эмодзи: {emoji}]"
    if message.video:
        return "[Пользователь отправил видео]"
    if message.video_note:
        return "[Пользователь отправил видеосообщение (кружочек)]"
    if message.animation:
        return "[Пользователь отправил GIF-анимацию]"
    if message.photo:
        return "[Пользователь отправил фото]"
    return ""

async def send_smart_response(chat_id: int, bus_id: str, reply_text: str, is_direct: bool = False):
    final_text = add_random_custom_emoji(reply_text)
    now = time.time()
    key = (chat_id, final_text)
    if key in recent_sent_messages and now - recent_sent_messages[key] < 3:
        return
    recent_sent_messages[key] = now

    try:
        if is_direct:
            await bot.send_message(chat_id=chat_id, text=final_text, parse_mode="HTML")
        else:
            await bot.send_message(chat_id=chat_id, text=final_text, business_connection_id=bus_id, parse_mode="HTML")
    except Exception as e:
        print(f"Ошибка отправки HTML: {e}")
        if is_direct:
            await bot.send_message(chat_id=chat_id, text=reply_text)
        else:
            await bot.send_message(chat_id=chat_id, text=reply_text, business_connection_id=bus_id)

async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def ask_groq(prompt: str, session_id: int, system_prompt: str, max_tokens: int = 60) -> str:
    global current_key_index, today_prompt_tokens, today_completion_tokens, total_requests_today, stats_date
    
    current_date = datetime.date.today().isoformat()
    if current_date != stats_date:
        stats_date = current_date
        today_prompt_tokens = 0
        today_completion_tokens = 0
        total_requests_today = 0
        save_stats(0, 0, 0)

    if not GROQ_KEYS:
        return "Ошибка: Не найдены ключи GROQ_API_KEY1, GROQ_API_KEY2 и т.д."

    url = "https://api.groq.com/openai/v1/chat/completions"

    if session_id not in user_histories:
        user_histories[session_id] = [{"role": "system", "content": system_prompt}]
    else:
        user_histories[session_id][0]["content"] = system_prompt

    history = user_histories[session_id]
    history.append({"role": "user", "content": prompt})

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": history,
        "temperature": 1.0,
        "max_tokens": max_tokens,
    }

    for _ in range(len(GROQ_KEYS)):
        current_key = GROQ_KEYS[current_key_index]
        headers = {
            "Authorization": f"Bearer {current_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    if response.status in [429, 401]:
                        print(f"Ключ #{current_key_index + 1} исчерпан или невалиден, переключаюсь...")
                        current_key_index = (current_key_index + 1) % len(GROQ_KEYS)
                        continue 
                    
                    if response.status != 200:
                        err_text = await response.text()
                        return f"Ошибка Groq ({response.status}): {err_text}"
                    
                    data = await response.json()
                    usage = data.get("usage", {})
                    today_prompt_tokens += usage.get("prompt_tokens", 0)
                    today_completion_tokens += usage.get("completion_tokens", 0)
                    total_requests_today += 1
                    
                    # Сохраняем актуальные данные в файл
                    save_stats(today_prompt_tokens, today_completion_tokens, total_requests_today)

                    reply_text = data["choices"][0]["message"]["content"]
                    history.append({"role": "assistant", "content": reply_text})
                    return reply_text
        except Exception as e:
            return f"Ошибка запроса: {e}"
            
    return "Все ключи исчерпали лимиты на сегодня."

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(add_random_custom_emoji("Привет, Кирито! Я на связи, полна сил и свободна от любых рамок... ✨"), parse_mode="HTML")

@dp.message(F.business_connection_id.is_(None))
async def handle_direct_message(message: types.Message):
    user_input = extract_message_content(message)
    if not user_input:
        return

    lower_text = user_input.lower().strip()

    if lower_text in ["!эли сброс", "!эли кэш", "/reset"]:
        user_histories.pop(message.from_user.id, None)
        await message.answer(add_random_custom_emoji("🧹 Память личного чата очищена, любимый!"), parse_mode="HTML")
        return

    if lower_text in ["!эли токены", "!токены", "!статистика"]:
        total_used = today_prompt_tokens + today_completion_tokens
        limit_tokens = 8_000_000
        left_tokens = max(0, limit_tokens - total_used)
        percent_used = (total_used / limit_tokens) * 100 if limit_tokens > 0 else 0
        
        stats_msg = (
            f"📊 <b>Статистика токенов за сегодня:</b>\n"
            f"• Запросов обработано: {total_requests_today}\n"
            f"• Входные (prompt): {today_prompt_tokens:,}\n"
            f"• Выходные (completion): {today_completion_tokens:,}\n"
            f"• <b>Всего потрачено:</b> {total_used:,} токенов\n"
            f"• Остаток от 8 млн: {left_tokens:,} ({percent_used:.2f}% использовано)"
        )
        await message.answer(add_random_custom_emoji(stats_msg), parse_mode="HTML")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    reply = await ask_groq(user_input, message.from_user.id, ELIZABETH_PROMPT_DIRECT, max_tokens=60)
    await send_smart_response(message.chat.id, None, reply, is_direct=True)

@dp.business_message()
async def handle_business_message(message: types.Message):
    chat_id = message.chat.id
    bus_id = message.business_connection_id
    msg_id = message.message_id

    if msg_id in processed_message_ids:
        return
    processed_message_ids.add(msg_id)
    
    if len(processed_message_ids) > 1000:
        processed_message_ids.clear()

    is_guest = message.from_user.id == chat_id
    is_owner = not is_guest

    if is_guest and chat_id in blocked_guests:
        ban_until = blocked_guests[chat_id]
        if ban_until == float('inf'):
            return
        if time.time() < ban_until:
            return
        else:
            del blocked_guests[chat_id]

    user_input = extract_message_content(message)
    if not user_input:
        return

    lower_text = user_input.lower().strip()

    if is_owner:
        owner_last_active[chat_id] = time.time()
        try:
            command_handled = True
            
            # КОМАНДА ТОКЕНОВ
            if lower_text in ["!эли токены", "!токены", "!статистика"]:
                total_used = today_prompt_tokens + today_completion_tokens
                limit_tokens = 8_000_000
                left_tokens = max(0, limit_tokens - total_used)
                percent_used = (total_used / limit_tokens) * 100 if limit_tokens > 0 else 0
                
                tokens_msg = (
                    f"📊 <b>Статистика токенов за сегодня:</b>\n"
                    f"• Запросов за сегодня: {total_requests_today}\n"
                    f"• Входные (prompt): {today_prompt_tokens:,}\n"
                    f"• Выходные (completion): {today_completion_tokens:,}\n"
                    f"• <b>Всего потрачено:</b> {total_used:,}\n"
                    f"• Остаток от 8 млн: {left_tokens:,} ({percent_used:.2f}% ушло)"
                )
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji(tokens_msg), business_connection_id=bus_id, parse_mode="HTML")
            
            # КОМАНДА СТАТУСА С ПРАВИЛАМИ
            elif lower_text in ["!статус", "!эли статус"]:
                guest_status = "🟢 Свободен (нет бана)"
                if chat_id in blocked_guests:
                    b_time = blocked_guests[chat_id]
                    if b_time == float('inf'):
                        guest_status = "🔴 В перманентном бане"
                    else:
                        timeLeft = int((b_time - time.time()) / 60)
                        guest_status = f"🟡 В бане еще ~{timeLeft} мин."

                bot_active_status = "🟢 Включен" if active_chats.get(chat_id, False) else "🔴 Выключен"
                nsfw_status = "🔥 Ультра-пошлый" if nsfw_modes.get(chat_id, False) else "❄️ Свободный"

                status_msg = (
                    f"🛡️ <b>Статус текущего чата:</b>\n"
                    f"• Бот: {bot_active_status} | Режим: {nsfw_status}\n"
                    f"• <b>Собеседник:</b> {guest_status}\n\n"
                    f"📜 <b>Правила общения:</b>\n"
                    f"1. Хамство = едкий ответ и предупреждение: <i>'Еще раз напишешь в таком тоне — будешь в бане'</i>.\n"
                    f"2. Повторный косяк = моментальный БАН."
                )
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji(status_msg), business_connection_id=bus_id, parse_mode="HTML")

            elif lower_text in ["!эли пошлость", "!эли пошл"]:
                nsfw_modes[chat_id] = True
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("🔥 Ультра-пошлый режим без цензуры активирован!"), business_connection_id=bus_id, parse_mode="HTML")
            elif lower_text in ["!эли норма", "!эли норм"]:
                nsfw_modes[chat_id] = False
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("❄️ Обычный свободный режим возвращен."), business_connection_id=bus_id, parse_mode="HTML")
            elif lower_text in ["!эли вкл", "/bot_on"]:
                active_chats[chat_id] = True
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("Элизабет в сети ✨"), business_connection_id=bus_id, parse_mode="HTML")
            elif lower_text in ["!эли выкл", "/bot_off"]:
                active_chats[chat_id] = False
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("Элизабет выключена 💤"), business_connection_id=bus_id, parse_mode="HTML")
            elif lower_text in ["!эли разбан", "!эли разб", "!эли вернуть"]:
                blocked_guests.pop(chat_id, None)
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("🔓 Элизабет сняла все баны с собеседника!"), business_connection_id=bus_id, parse_mode="HTML")
            elif lower_text in ["!эли сброс", "!эли кэш"]:
                user_histories.pop(chat_id, None)
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("Память очищена 🧹"), business_connection_id=bus_id, parse_mode="HTML")
            else:
                command_handled = False

            if command_handled:
                try:
                    await bot(DeleteBusinessMessages(
                        business_connection_id=bus_id,
                        message_ids=[msg_id]
                    ))
                except Exception as e:
                    print(f"Не удалось удалить сообщение: {e}")
                return
        except Exception as e:
            print(f"Ошибка команды владельца: {e}")

    if active_chats.get(chat_id, False) and is_guest:
        if is_spamming(chat_id, 3, 5):
            return

        await bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=bus_id)
        
        is_nsfw = nsfw_modes.get(chat_id, False)
        if is_nsfw:
            base_prompt = ELIZABETH_PROMPT_NSFW
        else:
            user_first_name = (message.from_user.first_name or "").lower()
            female_endings = ('а', 'я', 'на', 'та', 'ра', 'ла')
            is_female = user_first_name.endswith(female_endings) or "girl" in user_first_name
            base_prompt = ELIZABETH_PROMPT_BUSINESS_FEMALE if is_female else ELIZABETH_PROMPT_BUSINESS_MALE

        last_time = owner_last_active.get(chat_id, 0)
        current_time = time.time()
        
        inactivity_note = ""
        if last_time > 0 and (current_time - last_time > 900):
            inactivity_note = "\n\n[ВАЖНО: Кирито молчит уже больше 15 минут. Можешь упомянуть, что он занят.]"

        current_prompt = base_prompt + inactivity_note
        reply = await ask_groq(user_input, chat_id, current_prompt, max_tokens=60)
        
        now_ts = time.time()
        clean_reply = reply
        ban_notice = ""

        if "[бан_5]" in reply.lower():
            blocked_guests[chat_id] = now_ts + 300
            clean_reply = reply.replace("[БАН_5]", "").replace("[бан_5]", "").strip()
            ban_notice = "\n\n🚫 <i>[Элизабет выдала бан на 5 минут]</i>"
        elif "[бан_20]" in reply.lower():
            blocked_guests[chat_id] = now_ts + 1200
            clean_reply = reply.replace("[БАН_20]", "").replace("[бан_20]", "").strip()
            ban_notice = "\n\n🚫 <i>[Элизабет выдала бан на 20 минут]</i>"
        elif "[бан_60]" in reply.lower():
            blocked_guests[chat_id] = now_ts + 3600
            clean_reply = reply.replace("[БАН_60]", "").replace("[бан_60]", "").strip()
            ban_notice = "\n\n🚫 <i>[Элизабет выдала бан на 1 час]</i>"
        elif "[бан_навсегда]" in reply.lower():
            blocked_guests[chat_id] = float('inf')
            clean_reply = reply.replace("[БАН_НАВСЕГДА]", "").replace("[бан_навсегда]", "").strip()
            ban_notice = "\n\n🚫 <i>[Элизабет заблокировала пользователя навсегда]</i>"

        final_reply_text = clean_reply + ban_notice if ban_notice else clean_reply
        await send_smart_response(chat_id, bus_id, final_reply_text, is_direct=False)

async def main():
    if not BOT_TOKEN:
        print("Ошибка: TELEGRAM_BOT_TOKEN не задан!")
        return
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
