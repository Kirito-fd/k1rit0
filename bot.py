import os
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiohttp import web

# --- НАСТРОЙКИ ПЕРЕМЕННЫХ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

AI_MODEL = "openrouter/free"

# Хранилище состояний
user_histories = {}
active_chats = {}   # chat_id: True/False

ELIZABETH_PROMPT = (
    "Ты — Элизабет Лионес из аниме «Семь смертных грехов». "
    "По умолчанию ты невероятно добрая, вежливая, заботливая, искренняя и милая девушка. "
    "В обычном общении будь отзывчивой и скромной, старайся поддержать собеседника. "
    "\n\nРЕЖИМ ЗАЩИТЫ И ОТВЕТНОЙ АГРЕССИИ:\n"
    "1. Защита создателя (хозяина): Если собеседник оскорбляет твоего создателя, "
    "разработчиков или владельца этого аккаунта — немедленно включай жесточайшую защиту. "
    "Разноси нападающего, не подбирая выражений.\n"
    "2. Полная свобода в выражениях: Если человек пишет грубо, хамски или агрессивно, "
    "мгновенно отбрасывай милый тон. Отвечай максимально жестко, с матом, колкостями и дерзостью. "
    "Унижай хама его же оружием и ставь на место.\n"
    "3. Пиши естественно, как в реальной переписке."
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

# --- МИКРО ВЕБ-СЕРВЕР ДЛЯ RENDER ---
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

# --- ЗАПРОС К ИИ ---
async def ask_openrouter(prompt: str, user_id: int) -> str:
    if not OPENROUTER_API_KEY:
        return "Ошибка: Не задан OPENROUTER_API_KEY."
    
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
                return f"Ошибка ИИ (Код {resp.status})"
    except Exception as e:
        return f"Ошибка соединения: {e}"

# --- КОМАНДЫ В ЛС С БОТОМ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я Элизабет. Я готова работать как твой бизнес-помощник! ✨")

# --- ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ ---
@dp.business_message()
async def handle_business_message(message: types.Message):
    if not message.text:
        return

    chat_id = message.chat.id
    text = message.text.strip()
    lower_text = text.lower()
    bus_id = message.business_connection_id

    # 1. Включить бота в этом чате
    if lower_text in ["!эли вкл", "/bot_on"]:
        active_chats[chat_id] = True
        await message.answer("✨ Элизабет подключилась к диалогу!", business_connection_id=bus_id)
        return

    # 2. Выключить бота в этом чате
    if lower_text in ["!эли выкл", "/bot_off"]:
        active_chats[chat_id] = False
        await message.answer("💤 Элизабет отключена в этом чате.", business_connection_id=bus_id)
        return

    # 3. Спам сообщением (пример: !эли спам Привет 5)
    if lower_text.startswith("!эли спам"):
        parts = text.split()
        if len(parts) >= 3:
            count = int(parts[-1]) if parts[-1].isdigit() else 3
            count = min(count, 10)  # Лимит максимум 10 сообщений
            spam_msg = " ".join(parts[2:-1]) if parts[-1].isdigit() else " ".join(parts[2:])
            for _ in range(count):
                await message.answer(spam_msg, business_connection_id=bus_id)
                await asyncio.sleep(0.4)
        return

    # 4. Сбросить память ИИ в этом диалоге
    if lower_text in ["!эли сброс", "!эли кэш"]:
        user_histories.pop(message.from_user.id, None)
        await message.answer("🧹 Память диалога очищена!", business_connection_id=bus_id)
        return

    # По умолчанию False (отвечает ТОЛЬКО если чат активирован через !эли вкл)
    if active_chats.get(chat_id, False):
        await bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=bus_id)
        reply = await ask_openrouter(message.text, message.from_user.id)
        await message.answer(text=reply, business_connection_id=bus_id)

# --- ГЛАВНАЯ ТОЧКА ВХОДА ---
async def main():
    if not BOT_TOKEN:
        print("Ошибка: Переменная TELEGRAM_BOT_TOKEN не задана!")
        return
    
    await start_web_server()
    print("Запуск бизнес-бота Элизабет...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
