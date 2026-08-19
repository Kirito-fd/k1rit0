import asyncio
import os
import json
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command

# --- ИНИЦИАЛИЗАЦИЯ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Получаем ключи напрямую из переменных окружения GROQ_API_KEY1, 2 и т.д.
GROQ_KEYS = [os.getenv(f"GROQ_API_KEY{i}") for i in range(1, 10) if os.getenv(f"GROQ_API_KEY{i}")]

print(f"DEBUG: Загружено ключей: {len(GROQ_KEYS)}")
if not GROQ_KEYS:
    print("ВНИМАНИЕ: Ключи GROQ_API_KEY не найдены в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def ask_groq_diagnostic(prompt: str) -> str:
    if not GROQ_KEYS:
        return "Ошибка: Ключи не настроены."
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_KEYS[0]}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 50
    }

    print(f"DEBUG: Отправляю запрос в Groq с ключом: {GROQ_KEYS[0][:10]}...")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                status = response.status
                text = await response.text()
                print(f"DEBUG: Ответ Groq (код {status}): {text}")
                
                if status == 200:
                    data = await response.json()
                    return data["choices"][0]["message"]["content"]
                else:
                    return f"Ошибка API {status}: {text[:50]}"
    except Exception as e:
        print(f"DEBUG: Критическая ошибка запроса: {e}")
        return str(e)

@dp.message()
async def echo_handler(message: types.Message):
    # Тестовый хендлер: отвечает через Groq на любое сообщение
    response = await ask_groq_diagnostic(message.text)
    await message.answer(response)

async def main():
    if not BOT_TOKEN:
        print("Ошибка: Токен Telegram не задан!")
        return
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
