import os
import asyncio

# Исправление для поддержки asyncio в Python 3.14 (Pyrogram)
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import aiohttp
from aiogram import Bot, Dispatcher, types as aiogram_types
from aiogram.filters import Command
from pyrogram import Client, filters as pyro_filters, types as pyro_types
from pyrogram.enums import ChatAction

# --- НАСТРОЙКИ ПЕРЕМЕННЫХ ---
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Преобразование API_ID в int, если переменная существует
API_ID = int(API_ID) if API_ID and API_ID.isdigit() else None

AI_MODEL = "openrouter/free"
AI_AUTO_REPLY_GLOBAL = False
user_histories = {}

ELIZABETH_PROMPT = (
    "Ты — Элизабет Лионес из аниме «Семь смертных грехов». "
    "Ты невероятно добрая, вежливая, заботливая, искренняя и мягкая девушка. "
    "В общении будь милой, отзывчивой и слегка скромной, при этом всегда старайся поддержать собеседника. "
    "Отвечай естественным образом, как в переписке, избегай сухого или формального тона."
)

# Инициализация клиентов
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
userbot = Client("eli_userbot", api_id=API_ID, api_hash=API_HASH) if (API_ID and API_HASH) else None


# --- ЗАПРОС К ИИ ---
async def ask_openrouter(prompt: str, user_id: int) -> str:
    if not OPENROUTER_API_KEY:
        return "Ошибка: Не задан OPENROUTER_API_KEY в переменные окружения."
    
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": ELIZABETH_PROMPT}]
    
    user_histories[user_id].append({"role": "user", "content": prompt})
    if len(user_histories[user_id]) > 11:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": AI_MODEL,
        "messages": user_histories[user_id]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    reply = data["choices"][0]["message"]["content"]
                    user_histories[user_id].append({"role": "assistant", "content": reply})
                    return reply
                return f"Ошибка сервера ИИ (Код {resp.status})"
    except Exception as e:
        return f"Ошибка запроса: {e}"


# --- 1. ЛОГИКА ОБЫЧНОГО БОТА (aiogram) ---
if dp:
    @dp.message(Command("start"))
    async def cmd_start(message: aiogram_types.Message):
        await message.answer("Привет! Я Элизабет. Напиши мне что-нибудь, и я с радостью отвечу! ✨")

    @dp.message()
    async def bot_ai_reply(message: aiogram_types.Message):
        if message.text:
            await bot.send_chat_action(message.chat.id, action="typing")
            reply = await ask_openrouter(message.text, message.from_user.id)
            await message.answer(reply)


# --- 2. ЛОГИКА ЮЗЕРБОТА-СЕКРЕТАРЯ (Pyrogram) ---
if userbot:
    @userbot.on_message(pyro_filters.me & pyro_filters.command("ai", prefixes="."))
    async def userbot_ai_cmd(client: Client, message: pyro_types.Message):
        text = message.text.split(maxsplit=1)
        if len(text) > 1:
            prompt = text[1]
            await message.edit_text("Думаю...")
            reply = await ask_openrouter(prompt, message.from_user.id)
            await message.edit_text(reply)
        else:
            global AI_AUTO_REPLY_GLOBAL
            AI_AUTO_REPLY_GLOBAL = not AI_AUTO_REPLY_GLOBAL
            state = "ВКЛЮЧЕН" if AI_AUTO_REPLY_GLOBAL else "ВЫКЛЮЧЕН"
            await message.edit_text(f"Автоответчик Элизабет: **{state}**")

    @userbot.on_message(pyro_filters.private & ~pyro_filters.me)
     async def userbot_auto_reply(client: Client, message: pyro_types.Message):
        if AI_AUTO_REPLY_GLOBAL and message.text:
            await client.send_chat_action(message.chat.id, ChatAction.TYPING)
            reply = await ask_openrouter(message.text, message.from_user.id)
            await message.reply_text(reply)


# --- ОДНОВРЕМЕННЫЙ ЗАПУСК ---
async def main():
    tasks = []
    if bot:
        print("Запуск обычного бота...")
        tasks.append(dp.start_polling(bot))
    if userbot:
        print("Запуск юзербота...")
        tasks.append(userbot.start())
        
    if not tasks:
        print("Ошибка: Не заданы ключи авторизации в Environment!")
        return

    await asyncio.gather(*tasks)

if name == "__main__":
    asyncio.run(main())
