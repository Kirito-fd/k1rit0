import os
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# --- НАСТРОЙКИПЕРЕМЕННЫХ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

AI_MODEL = "openrouter/free"
user_histories = {}

ELIZABETH_PROMPT = (
    "Ты — Элизабет Лионес из аниме «Семь смертных грехов». "
    "Ты невероятно добрая, вежливая, заботливая, искренняя и мягкая девушка. "
    "В общении будь милой, отзывчивой и слегка скромной, при этом всегда старайся поддержать собеседника. "
    "Отвечай естественным образом, как в переписке, избегай сухого или формального тона."
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

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
                return f"Извини, произошла ошибка ИИ (Код {resp.status})"
    except Exception as e:
        return f"Ошибка соединения: {e}"

# --- 1. ОБРАБОТКА КОМАНД В ЛС С БОТОМ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я Элизабет. Я подключена как твой бизнес-помощник! ✨")

# --- 2. ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ В ЛИЧНЫХ ЧАТАХ ---
@dp.business_message()
async def handle_business_message(message: types.Message):
    # Отвечаем, если пришло текстовое сообщение
    if message.text:
        await bot.send_chat_action(
            chat_id=message.chat.id, 
            action="typing", 
            business_connection_id=message.business_connection_id
        )
        reply = await ask_openrouter(message.text, message.from_user.id)
        
        await message.answer(
            text=reply, 
            business_connection_id=message.business_connection_id
        )

async def main():
    if not BOT_TOKEN:
        print("Ошибка: Переменная TELEGRAM_BOT_TOKEN не задана!")
        return
    print("Запуск бизнес-бота Элизабет...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
    
