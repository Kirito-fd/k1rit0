import asyncio
import os
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiohttp import web

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

user_histories = {}


async def handle(request):
    return web.Response(text="Bot is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Привет! Я Эли. Задавай любой вопрос!")


@dp.message(Command("reset"))
async def reset_cmd(message: types.Message):
    user_id = message.from_user.id
    user_histories[user_id] = []
    await message.answer("🧠 История диалога очищена!")


@dp.message(F.text)
async def ai_reply(message: types.Message):
    user_id = message.from_user.id

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({"role": "user", "content": message.text})

    if len(user_histories[user_id]) > 10:
        user_histories[user_id] = user_histories[user_id][-10:]

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "openrouter/free",
        "messages": [{
            "role": "system",
            "content": "Ты умный и дружелюбный ассистент Эли. Отвечай кратко и понятно."
        }] + user_histories[user_id]
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload
            ) as resp:
                data = await resp.json()

                if resp.status == 200 and "choices" in data:
                    reply_text = data["choices"][0]["message"]["content"]
                    user_histories[user_id].append({"role": "assistant", "content": reply_text})
                    await message.answer(reply_text)
                else:
                    print(f"ОШИБКА OPENROUTER [{resp.status}]: {data}")
                    await message.answer(f"Ошибка сервера (Код {resp.status}).")

        except Exception as e:
            print(f"Ошибка запроса: {e}")
            await message.answer("Ошибка соединения.")


async def main():
    await start_web_server()
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
