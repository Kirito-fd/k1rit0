import os
import asyncio
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiohttp import web

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# Инициализация с принудительным использованием REST-транспорта, 
# что часто исправляет ошибки 404 на облачных серверах
genai.configure(api_key=GOOGLE_API_KEY, transport='rest')
ai_model = genai.GenerativeModel('models/gemini-1.5-flash')

user_histories = {}
active_chats = {}

ELIZABETH_PROMPT = (
    "Ты — Элизабет Лионес из аниме «Семь смертных грехов». "
    "Будь доброй, милой и заботливой по умолчанию. "
    "Если собеседник хамит или оскорбляет — включай режим жесткой защиты, "
    "используй дерзость, колкости и мат, чтобы поставить хама на место. "
    "Пиши естественно, как в реальном мессенджере."
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# --- ВЕБ-СЕРВЕР ---
async def start_web_server():
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start()

# --- ЛОГИКА GEMINI ---
async def ask_gemini(prompt: str, user_id: int) -> str:
    try:
        if user_id not in user_histories:
            user_histories[user_id] = ai_model.start_chat(history=[
                {"role": "user", "parts": [ELIZABETH_PROMPT]},
                {"role": "model", "parts": ["Поняла. Буду действовать согласно инструкции."]}
            ])
        response = await user_histories[user_id].send_message_async(prompt)
        return response.text
    except Exception as e:
        return f"Ошибка Gemini: {str(e)}"

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Элизабет на связи! ✨")

@dp.business_message()
async def handle_business(message: types.Message):
    chat_id = message.chat.id
    bus_id = message.business_connection_id
    
    if message.text and message.text.lower() == "!эли вкл":
        active_chats[chat_id] = True
        await bot.send_message(chat_id=chat_id, text="✨ Режим Элизабет ВКЛ", business_connection_id=bus_id)
        return

    if active_chats.get(chat_id):
        await bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=bus_id)
        reply = await ask_gemini(message.text, chat_id)
        await bot.send_message(chat_id=chat_id, text=reply, business_connection_id=bus_id)

async def main():
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
