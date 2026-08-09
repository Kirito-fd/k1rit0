import asyncio
import os
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from openai import OpenAI

# Считываем ключи из переменных окружения сервера
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

ai_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

user_histories = {}


@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("Привет! Я ИИ-помощник на базе DeepSeek. Задавай любой вопрос!")


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
            model="deepseek-chat",
            messages=messages,
            max_tokens=300
        )

        reply_text = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": reply_text})

        await message.answer(reply_text)

    except Exception as e:
        await message.answer("Произошла ошибка при обработке запроса к DeepSeek.")
        print(f"Ошибка API: {e}")


async def main():
    print("Бот на базе DeepSeek запущен!")
    await dp.start_polling(bot)


if name == "__main__":
    asyncio.run(main())
