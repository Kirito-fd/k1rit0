import os
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiohttp import web

# --- НАСТРОЙКИ ПЕРЕМЕННЫХ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

user_histories = {}
active_chats = {}   # chat_id: True/False
spam_tasks = {}     # chat_id: asyncio.Task

# Промпт для личного общения С БОТОМ (Душевная, эмоции, защита)
ELIZABETH_PROMPT_DIRECT = (
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

# Промпт для БИЗНЕС-ЧАТОВ с посторонними (Адаптивный: холодный -> мат при агрессии -> снова холодный)
ELIZABETH_PROMPT_BUSINESS = (
    "Ты — Элизабет. Ты отвечаешь постороннему человеку в личных сообщениях через Telegram Business.\n"
    "ПРАВИЛА ТВОЕГО ПОВЕДЕНИЯ:\n"
    "1. Базовый тон: Отвечай максимально кратко, холодно, сухо и отстраненно. Без лишних эмоций, без смайликов и приветливости.\n"
    "2. Режим ответной агрессии и мата: Если собеседник пишет тебе грубо, хамски, проявляет агрессию или использует мат — "
    "немедленно отвечай ему тем же: жестко, с матом, колко и дерзко, ставя его на место.\n"
    "3. Возврат к нормальному тону: Как только собеседник перестает хамить и материться (написал сообщение спокойно или адекватно), "
    "ты мгновенно прекращаешь использовать мат и агрессию и снова отвечаешь в базовом строгом, сухом и холодном тоне."
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

# --- ПРЯМОЙ ЗАПРОС К GROQ API ---
async def ask_groq(prompt: str, session_id: int, system_prompt: str) -> str:
    if not GROQ_API_KEY:
        return "Ошибка: Не задан GROQ_API_KEY в настройках Render."
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    if session_id not in user_histories:
        user_histories[session_id] = [
            {"role": "system", "content": system_prompt}
        ]
    
    history = user_histories[session_id]
    history.append({"role": "user", "content": prompt})
    
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": history,
        "temperature": 0.5
    }
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as response:
                if response.status != 200:
                    err_text = await response.text()
                    return f"Ошибка Groq ({response.status}): {err_text}"
                
                data = await response.json()
                reply_text = data["choices"][0]["message"]["content"]
                
                history.append({"role": "assistant", "content": reply_text})
                return reply_text
    except Exception as e:
        return f"Ошибка запроса: {e}"

# --- ОБРАБОТКА ЛИЧНЫХ СООБЩЕНИЙ С БОТОМ (1 на 1) ---
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
    reply = await ask_groq(message.text, message.from_user.id, ELIZABETH_PROMPT_DIRECT)
    await message.answer(reply)

# --- ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ (Общение с другими людьми) ---
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
            await bot.send_message(chat_id=chat_id, text="Элизабет подключена.", business_connection_id=bus_id)
        return

    # 2. Выключение
    if lower_text in ["!эли выкл", "/bot_off"]:
        if is_owner:
            active_chats[chat_id] = False
            await bot.send_message(chat_id=chat_id, text="Элизабет отключена.", business_connection_id=bus_id)
        return

    # 3. Статус
    if lower_text in ["!эли инфо", "!эли статус"]:
        if is_owner:
            status = "ВКЛЮЧЕНА" if active_chats.get(chat_id, False) else "ВЫКЛЮЧЕНА"
            await bot.send_message(chat_id=chat_id, text=f"Статус: {status}", business_connection_id=bus_id)
        return

    # 4. Остановка спама
    if lower_text in ["!эли стоп", "!эли стоп спам"]:
        if is_owner:
            if chat_id in spam_tasks and not spam_tasks[chat_id].done():
                spam_tasks[chat_id].cancel()
                spam_tasks.pop(chat_id, None)
                await bot.send_message(chat_id=chat_id, text="⏹ Спам остановлен.", business_connection_id=bus_id)
            else:
                await bot.send_message(chat_id=chat_id, text="Активного спама нет.", business_connection_id=bus_id)
        return

    # 5. Спам-функция (С возможностью бесконечного спама)
    if lower_text.startswith("!эли спам"):
        if is_owner:
            parts = text.split()
            if len(parts) >= 3:
                if parts[-1].isdigit():
                    count = int(parts[-1])
                    spam_msg = " ".join(parts[2:-1])
                else:
                    count = None  # Бесконечный спам
                    spam_msg = " ".join(parts[2:])

                if not spam_msg:
                    spam_msg = "Спам"

                # Если спам уже запущен в этом чате — отменяем прошлую задачу
                if chat_id in spam_tasks and not spam_tasks[chat_id].done():
                    spam_tasks[chat_id].cancel()

                async def run_spam(c_id, b_id, msg, cnt):
                    try:
                        i = 0
                        while cnt is None or i < cnt:
                            await bot.send_message(chat_id=c_id, text=msg, business_connection_id=b_id)
                            await asyncio.sleep(0.4)
                            i += 1
                    except asyncio.CancelledError:
                        pass
                    finally:
                        spam_tasks.pop(c_id, None)

                task = asyncio.create_task(run_spam(chat_id, bus_id, spam_msg, count))
                spam_tasks[chat_id] = task
        return

    # 6. Сброс истории чата
    if lower_text in ["!эли сброс", "!эли кэш"]:
        if is_owner:
            user_histories.pop(chat_id, None)
            await bot.send_message(chat_id=chat_id, text="Память чата очищена.", business_connection_id=bus_id)
        return

    # Ответ собеседнику
    if active_chats.get(chat_id, False):
        bot_info = await bot.get_me()
        if not is_owner and message.from_user.id != bot_info.id:
            await bot.send_chat_action(chat_id=chat_id, action="typing", business_connection_id=bus_id)
            reply = await ask_groq(message.text, chat_id, ELIZABETH_PROMPT_BUSINESS)
            await bot.send_message(chat_id=chat_id, text=reply, business_connection_id=bus_id)

# --- ЗАПУСК ---
async def main():
    if not BOT_TOKEN:
        print("Ошибка: Переменная TELEGRAM_BOT_TOKEN не задана!")
        return
    
    await start_web_server()
    print("Запуск бота через Groq API...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
