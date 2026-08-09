import asyncio
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from openai import OpenAI
from aiohttp import web

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Подключаемся к Gemini через бесплатный шлюз OpenRouter
ai_client = OpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url="https://openrouter.ai/api/v1"
)

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
    await message.answer("🧠 История нашего диалога очищена!")


@dp.message(F.text)
async def ai_reply(message: types.Message):
    user_id = message.from_user.id

    if user_id not in user_histories:
        user_histories[user_id] = []

    user_histories[user_id].append({"role": "user", "content": message.text})

    if len(user_histories[user_id]) > 10:
        user_histories[user_id] = user_histories[user_id][-10:]

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")

    try:
        messages = [{
            "role": "system",
            "content": "Ты умный и дружелюбный ассистент. Отвечай кратко, по делу и понятным языком."
        }] + user_histories[user_id]

        response = ai_client.chat.completions.create(
            model="google/gemini-2.0-flash-exp:free",
            messages=messages,
            max_tokens=300
        )

        reply_text = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": reply_text})

        await message.answer(reply_text)

    except Exception as e:
        await message.answer("Произошла ошибка при обработке запроса.")
        print(f"Ошибка API: {e}")


async def main():
    await start_web_server()
    print("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
