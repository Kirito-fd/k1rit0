import os
import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import aiohttp
from pyrogram import Client, filters, types
from pyrogram.enums import ChatAction

# --- НАСТРОЙКИ ЮЗЕРБОТА ---
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
AI_AUTO_REPLY_GLOBAL = False  # По умолчанию выключено

AI_MODEL = "openrouter/free" 

app = Client(
    "eli_userbot",
    api_id=API_ID,
    api_hash=API_HASH
)

user_histories = {}

# Системный промпт роли Элизабет
ELIZABETH_PROMPT = (
    "Ты — Элизабет Лионес из аниме «Семь смертных грехов». "
    "Ты невероятно добрая, вежливая, заботливая, искренняя и мягкая девушка. "
    "В общении будь милой, отзывчивой и слегка скромной, при этом всегда старайся поддержать собеседника. "
    "Отвечай естественным образом, как в переписке, избегай сухого или формального тона."
)


# Вспомогательная функция для запроса к ИИ
async def get_ai_response(text, user_id):
    if user_id not in user_histories:
        user_histories[user_id] = []
    
    user_histories[user_id].append({"role": "user", "content": text})
    if len(user_histories[user_id]) > 10:
        user_histories[user_id] = user_histories[user_id][-10:]

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": AI_MODEL,
        "messages": [{
            "role": "system",
            "content": ELIZABETH_PROMPT
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
                    return reply_text
                else:
                    print(f"Ошибка OpenRouter: {data}")
                    return None
        except Exception as e:
            print(f"Ошибка соединения с ИИ: {e}")
            return None


# --- БЛОК 1: КОМАНДЫ (УПРАВЛЕНИЕ) ---

# .spam <количество> <текст>
@app.on_message(filters.me & filters.command("spam", prefixes="."))
async def spam_command(client, message: types.Message):
    await message.delete()

    if len(message.command) < 3:
        return

    try:
        count = int(message.command[1])
        text = " ".join(message.command[2:])
        
        for _ in range(count):
            await client.send_message(message.chat.id, text)
            await asyncio.sleep(0.3)
    except ValueError:
        pass


# .rev - перевернуть текст сообщения, на которое ответили
@app.on_message(filters.me & filters.command("rev", prefixes="."))
async def reverse_command(client, message: types.Message):
    if not message.reply_to_message:
        await message.edit_text("⚠️ Ответь на сообщение.")
        return

    text = message.reply_to_message.text
    if text:
        await message.edit_text(text[::-1])


# --- БЛОК 2: УПРАВЛЕНИЕ НЕЙРОСЕТЬЮ (ИИ) ---

# .ai on/off/текст
@app.on_message(filters.me & filters.command("ai", prefixes="."))
async def ai_control_command(client, message: types.Message):
    global AI_AUTO_REPLY_GLOBAL

    if len(message.command) < 2:
        await message.edit_text("ℹ️ Используй: .ai on, .ai off или `.ai <текст>`")
        return

    cmd = message.command[1].lower()

    if cmd == "on":
        AI_AUTO_REPLY_GLOBAL = True
        await message.edit_text("✨ Элизабет теперь ответит на входящие сообщения!")
    elif cmd == "off":
        AI_AUTO_REPLY_GLOBAL = False
        await message.edit_text("❌ Автоответ Элизабет выключен.")
    else:
        prompt = " ".join(message.command[1:])
        await message.edit_text("💭 *Элизабет думает...*")
        
        response = await get_ai_response(prompt, message.chat.id)
        if response:
            await message.edit_text(response)
        else:
            await message.edit_text("⚠️ Ошибка подключения.")


# --- БЛОК 3: ОБРАБОТКА ВСЕХ СООБЩЕНИЙ (АВТООТВЕТ) ---

@app.on_message(filters.incoming & filters.private & ~filters.bot)
async def auto_ai_reply(client, message: types.Message):
    global AI_AUTO_REPLY_GLOBAL

    if AI_AUTO_REPLY_GLOBAL:
        await client.send_chat_action(message.chat.id, ChatAction.TYPING)
        
        response = await get_ai_response(message.text, message.from_user.id)
        
        if response:
            await message.reply_text(response)


if __name__ == "__main__":
    print("Юзербот запущен от твоего имени!")
    app.run()
            
