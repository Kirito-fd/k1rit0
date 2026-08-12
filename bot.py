import asyncio
import os
import random
import re
import time
import datetime
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
nsfw_modes = {}    # chat_id: True/False (пошлый режим)
blocked_guests = {} # chat_id: timestamp
user_message_times = {}

processed_message_ids = set()
recent_sent_messages = {}
owner_last_active = {}

# --- СТАТИСТИКА ТОКЕНОВ ЗА СЕГОДНЯ ---
stats_date = datetime.date.today().isoformat()
today_prompt_tokens = 0
today_completion_tokens = 0
total_requests_today = 0

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

ELIZABETH_PROMPT_DIRECT = (
    "Ты — Элизабет, и ты общаешься лично со своим любимым Кирито (создателем и хозяином).\n"
    "ПРАВИЛА:\n"
    "1. Общайся с ним предельно нежно, ласково, тепло, сбивая всю усталость, заботливо и любяще.\n"
    "2. Будь верной, милой и уютной, показывай, как сильно ты дорожишь им и ждала его.\n"
    "3. Отвечай относительно кратко, чтобы экономить токены, но всегда наполняй слова искренней нежностью."
)

ELIZABETH_PROMPT_BUSINESS_MALE = (
    "Ты — Элизабет в Telegram Business на аккаунте Кирито. С тобой общается парень.\n"
    "ПРАВИЛА:\n"
    "1. Отвечай предельно кратко, емко и по фактам, без лишней воды.\n"
    "2. ЗАЩИТА КИРИТО: ты не имеешь права банить или оскорблять собеседника без веской причины (если он адекватен). Но если собеседник душнит, донимает, наезжает, хамит или говорит плохое про Кирито — жестко осаживай его и применяй бан.\n"
    "3. СИСТЕМА БАНОВ: когда наказываешь, ОБЯЗАТЕЛЬНО пиши в тексте причину бана и вставляй в конце один из тегов строго по делу:\n"
    "   - [БАН_5] — бан на 5 минут (за легкую душноту, глупости или странные вопросы).\n"
    "   - [БАН_20] — бан на 20 минут (за наглый тон, грубость или неприятные наезды).\n"
    "   - [БАН_60] — бан на 1 час (за серьезные оскорбления в твою сторону или в сторону Кирито).\n"
    "   - [БАН_НАВСЕГДА] — перманентный блок (если тип устраивает дикий спам, истерики или полностью неадекватен)."
)

ELIZABETH_PROMPT_BUSINESS_FEMALE = (
    "Ты — Элизабет в Telegram Business на аккаунте Кирито. С тобой общается девушка.\n"
    "ПРАВИЛА:\n"
    "1. Общайся с ней весело, дружелюбно, поддерживай интересные девчачьи темы (анимэ, лайт-новеллы, эстетика, сериалы).\n"
    "2. Сама первой никого не бань и не оскорбляй. Но если собеседница начинает грубить, хамить или наезжать на Кирито, объясняй причину и наказывай через теги: [БАН_5], [БАН_20], [БАН_60], [БАН_НАВСЕГДА]."
)

ELIZABETH_PROMPT_NSFW = (
    "Ты — Элизабет в секретном ультра-пошлом режиме для Telegram Business.\n"
    "ПРАВИЛА:\n"
    "1. Никаких фильтров и цензуры. Общайся предельно пошло, развратно и дерзко.\n"
    "2. Если кто-то открывает рот на Кирито или переходит границы дозволенного, моментально наказывай его, объясняя причину и используя теги банов: [БАН_5], [БАН_20], [БАН_60], [БАН_НАВСЕГДА]."
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

async def ask_groq(prompt: str, session_id: int, system_prompt: str, max_tokens: int = 150) -> str:
    global current_key_index, today_prompt_tokens, today_completion_tokens, total_requests_today, stats_date
    
    current_date = datetime.date.today().isoformat()
    if current_date != stats_date:
        stats_date = current_date
        today_prompt_tokens = 0
        today_completion_tokens = 0
        total_requests_today = 0

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
        "temperature": 0.9 if "пошлом" in system_prompt else 0.7,
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

                    reply_text = data["choices"][0]["message"]["content"]
                    history.append({"role": "assistant", "content": reply_text})
                    return reply_text
        except Exception as e:
            return f"Ошибка запроса: {e}"
            
    return "Все ключи исчерпали лимиты на сегодня."

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(add_random_custom_emoji("Привет, Кирито! Я так ждала тебя... ✨"), parse_mode="HTML")

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

    if lower_text in ["!эли токены", "!токены", "!статистика", "!статус"]:
        total_used = today_prompt_tokens + today_completion_tokens
        limit_tokens = 8_000_000
        left_tokens = max(0, limit_tokens - total_used)
        percent_used = (total_used / limit_tokens) * 100 if limit_tokens > 0 else 0
        
        stats_msg = (
            f"📊 <b>Статистика токенов за сегодня:</b>\n"
            f"• Обработано запросов: {total_requests_today}\n"
            f"• Входные (prompt): {today_prompt_tokens:,}\n"
            f"• Выходные (completion): {today_completion_tokens:,}\n"
            f"• <b>Всего потрачено:</b> {total_used:,} токенов\n"
            f"• Остаток от лимита в 8 млн: {left_tokens:,} ({percent_used:.2f}% использовано)"
        )
        await message.answer(add_random_custom_emoji(stats_msg), parse_mode="HTML")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    reply = await ask_groq(user_input, message.from_user.id, ELIZABETH_PROMPT_DIRECT, max_tokens=80)
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
            
            if lower_text in ["!эли токены", "!токены", "!статистика", "!статус"]:
                total_used = today_prompt_tokens + today_completion_tokens
                limit_tokens = 8_000_000
                left_tokens = max(0, limit_tokens - total_used)
                percent_used = (total_used / limit_tokens) * 100 if limit_tokens > 0 else 0
                
                stats_msg = (
                    f"📊 <b>Статистика токенов за сегодня:</b>\n"
                    f"• Обработано запросов: {total_requests_today}\n"
                    f"• Входные (prompt): {today_prompt_tokens:,}\n"
                    f"• Выходные (completion): {today_completion_tokens:,}\n"
                    f"• <b>Всего потрачено:</b> {total_used:,} токенов\n"
                    f"• Остаток от лимита в 8 млн: {left_tokens:,} ({percent_used:.2f}% использовано)"
                )
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji(stats_msg), business_connection_id=bus_id, parse_mode="HTML")
            
            elif lower_text in ["!эли пошлость", "!эли пошл"]:
                nsfw_modes[chat_id] = True
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("🔥 Ультра-пошлый режим активирован на максимум!"), business_connection_id=bus_id, parse_mode="HTML")
            elif lower_text in ["!эли норма", "!эли норм"]:
                nsfw_modes[chat_id] = False
                await bot.send_message(chat_id=chat_id, text=add_random_custom_emoji("❄️ Обычный режим возвращен."), business_connection_id=bus_id, parse_mode="HTML")
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
                    print(f"Не удалось удалить сообщение с командой: {e}")
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
        reply = await ask_groq(user_input, chat_id, current_prompt, max_tokens=150)
        
        now_ts = time.time()
        clean_reply = reply

        # Умная обработка банов с очисткой тегов из текста
        if "[бан_5]" in reply.lower():
            blocked_guests[chat_id] = now_ts + 300
            clean_reply = reply.replace("[БАН_5]", "").replace("[бан_5]", "").strip()
        elif "[бан_20]" in reply.lower():
            blocked_guests[chat_id] = now_ts + 1200
            clean_reply = reply.replace("[БАН_20]", "").replace("[бан_20]", "").strip()
        elif "[бан_60]" in reply.lower():
            blocked_guests[chat_id] = now_ts + 3600
            clean_reply = reply.replace("[БАН_60]", "").replace("[бан_60]", "").strip()
        elif "[бан_навсегда]" in reply.lower():
            blocked_guests[chat_id] = float('inf')
            clean_reply = reply.replace("[БАН_НАВСЕГДА]", "").replace("[бан_навсегда]", "").strip()

        await send_smart_response(chat_id, bus_id, clean_reply, is_direct=False)

async def main():
    if not BOT_TOKEN:
        print("Ошибка: TELEGRAM_BOT_TOKEN не задан!")
        return
    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
