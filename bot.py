import os
import asyncio
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiohttp import web

# --- НАСТРОЙКИ ПЕРЕМЕННЫХ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

# Инициализация Gemini
genai.configure(api_key=GOOGLE_API_KEY, transport='rest')
ai_model = genai.GenerativeModel('gemini-1.5-flash')

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

# --- ЗАПРОС К GEMINI ---
async def ask_gemini(prompt: str, user_id: int) -> str:
    if not GOOGLE_API_KEY:
        return "Ошибка: Не задан GOOGLE_API_KEY."
    
    try:
        if user_id not in user_histories:
            chat = ai_model.start_chat(history=[
                {"role": "user", "parts": [ELIZABETH_PROMPT]},
                {"role": "model", "parts": ["Поняла. Я буду следовать этой инструкции и оставаться Элизабет."]}
            ])
            user_histories[user_id] = chat
        
        chat = user_histories[user_id]
        response = await chat.send_message_async(prompt)
        return response.text
    except Exception as e:
        return f"Ошибка Gemini: {e}"

# --- ОБРАБОТКА ЛИЧНЫХ СООБЩЕНИЙ С БОТОМ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я Элизабет. Рада с тобой пообщаться! ✨")

@dp.message()
async def handle_direct_message(message: types.Message):
    if not message.text:
        return
    
    if message.text.strip().lower() in ["!эли сброс", "!эли кэш", "/reset"]:
        user_histories.pop(message.from_user.id, None)
        await message.answer("🧹 Моя память обнулена!")
        return

    await bot.send_chat_action(chat_id=message.chat.id, action="typing")
    reply = await ask_gemini(message.text, message.from_user.id)
    await message.answer(reply)

# --- ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ ---
@dp.business_message()
async def handle_business_message(message: types.Message):
    if not message.text:
        return

    chat_id = message.chat.id
    text = message.text.strip()
    lower_text = text.lower()
    bus_id = message.business_connection_id

    is_owner = (message.from_user.id != chat_id)

    # 1. Включение
    if lower_text in ["!эли вкл", "/bot_on"]:
        if is_owner:
            active_chats[chat_id] = True
            await bot.send_message(chat_id=chat_id, text="✨ Элизабет подключилась к диалогу!", business_connection_id=bus_id)
        return

    # 2. Выключение
    if lower_text in ["!эли выкл", "/bot_off"]:
        if is_owner:
            active_chats[chat_id] = False
            await bot.send_message(chat_id=chat_id, text="💤 Элизабет отключена в этом чате.", business_connection_id=bus_id)
        return

    # 3. Статус
    if lower_text in ["!эли инфо", "!эли статус"]:
        if is_owner:
            status = "ВКЛЮЧЕНА ✨" if active_chats.get(chat_id, False) else "ВЫКЛЮЧЕНА 💤"
            await bot.send_message(chat_id=chat_id, text=f"📊 Статус Элизабет в этом чате: {status}", business_connection_id=bus_id)
        return

    # 4. Спам-функция
    if lower_text.startswith("!эли спам"):
        if is_owner:
            parts = text.split()
            if len(parts) >= 3:
                count = int(parts[-1]) if parts[-1].isdigit() else 3
                count = min(count, 10)
                spam_msg = " ".join(parts[2:-1]) if parts[-1].isdigit() else " ".join(parts[2:])
                for _ in range(count):
                    await bot.send_message(chat_id=chat_id, text=spam_msg, business_connection_id=bus_id)
                    await asyncio.sleep(0.4)
        return

    # 5. Сброс истории чата
    if lower_text in ["!эли сброс", "!эли кэш"]:
        if is_owner:
            user_histories.pop(chat_id, None)
            await bot.send_message(chat_id=chat_id, text="🧹 Память диалога очищена!", business_connection_id=bus_id)
        return

    # Ответ собеседнику, если бот активен
    if active_chats.get(chat_id, False):
        bot_info = await bot.get_me()
        if not is_owner and message.from_user.id != bot_info.id:
            await bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=bus_id)
            reply = await ask_gemini(message.text, chat_id)
            await bot.send_message(chat_id=chat_id, text=reply, business_connection_id=bus_id)

# --- ЗАПУСК ---
async def main():
    if not BOT_TOKEN:
        print("Ошибка: Переменная TELEGRAM_BOT_TOKEN не задана!")
        return
    
    await start_web_server()
    print("Запуск бота...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
