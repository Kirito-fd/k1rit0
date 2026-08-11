import asyncio
import os
import re
import time
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiohttp import web

# --- НАСТРОЙКИ ПЕРЕМЕННЫХ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

user_histories = {}
active_chats = {}  # chat_id: True/False
spam_tasks = {}  # chat_id: asyncio.Task

# Хранилище замученных чатов: {chat_id: timestamp_окончания (или None для навсегда)}
muted_users = {}

# Промпт для личного общения С БОТОМ (1 на 1)
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

# Промпт для БИЗНЕС-ЧАТОВ с посторонними
ELIZABETH_PROMPT_BUSINESS = (
    "Ты — Элизабет. Ты отвечаешь постороннему человеку в личных сообщениях через Telegram Business.\n"
    "ПРАВИЛА ТВОЕГО ПОВЕДЕНИЯ:\n"
    "1. Базовый тон: Отвечай довольно кратко и сдержанно, но обязательно добавляй в сообщения разные подходящие по смыслу смайлики и эмодзи (✨, 🤍, 🙄, 💅, 😒, 🙂 и любые другие).\n"
    "2. Режим ответной агрессии и мата: Если собеседник пишет тебе грубо, хамски, проявляет агрессию или использует мат — "
    "немедленно отвечай ему тем же: жестко, с матом, колкостями и дерзостью, ставя его на место.\n"
    "3. Возврат к нормальному тону: Как только собеседник перестает хамить и материться (написал сообщение спокойно или адекватно), "
    "ты мгновенно прекращаешь использовать мат и агрессию и снова отвечаешь в базовом кратком тоне со смайликами."
)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def parse_duration(text: str):
    """Парсит время: 10м, 2ч, 1д или слова 'навсегда' / '0'"""
    text = text.lower().strip()
    if text in ["навсегда", "0", "inf", "forever"]:
        return None  # Бесконечный мут

    match = re.match(r"^(\d+)([мчдmhd]?)$", text)
    if not match:
        return 10 * 60  # По умолчанию 10 минут

    value, unit = int(match.group(1)), match.group(2)
    if unit in ["ч", "h"]:
        return value * 3600
    elif unit in ["д", "d"]:
        return value * 86400
    else:
        return value * 60


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
        "temperature": 0.6,
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers
            ) as response:
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
    reply = await ask_groq(
        message.text, message.from_user.id, ELIZABETH_PROMPT_DIRECT
    )
    await message.answer(reply)


# --- ОБРАБОТКА БИЗНЕС-СООБЩЕНИЙ ---
@dp.business_message()
async def handle_business_message(message: types.Message):
    chat_id = message.chat.id
    bus_id = message.business_connection_id
    is_owner = message.from_user.id != chat_id

    # --- 1. ПРОВЕРКА И АВТО-УДАЛЕНИЕ СООБЩЕНИЙ СОБЕСЕДНИКА В МУТЕ ---
    if not is_owner and chat_id in muted_users:
        until_time = muted_users[chat_id]
        if until_time is None or time.time() < until_time:
            try:
                await bot.delete_business_message(
                    business_connection_id=bus_id,
                    chat_id=chat_id,
                    message_id=message.message_id,
                )
                return
            except Exception:
                pass
        else:
            del muted_users[chat_id]

    if not message.text:
        return

    text = message.text.strip()
    lower_text = text.lower()

    # --- 2. МУТ СОБЕСЕДНИКА ---
    if lower_text.startswith("!эли мут"):
        if is_owner:
            args = text.split(maxsplit=2)
            duration_str = args[2] if len(args) >= 3 else "10м"
            duration_sec = parse_duration(duration_str)

            if duration_sec is None:
                muted_users[chat_id] = None
                time_text = "навсегда ♾️"
            else:
                muted_users[chat_id] = time.time() + duration_sec
                time_text = f"на {duration_str}"

            await bot.send_message(
                chat_id=chat_id,
                text=f"🚫 Чат замучен {time_text}.",
                business_connection_id=bus_id,
            )
        return

    # --- 3. РАЗМУТ СОБЕСЕДНИКА ---
    if lower_text == "!эли размут":
        if is_owner:
            if chat_id in muted_users:
                del muted_users[chat_id]
                await bot.send_message(
                    chat_id=chat_id,
                    text="🔊 Чат размучен.",
                    business_connection_id=bus_id,
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text="Этот чат не замучен.",
                    business_connection_id=bus_id,
                )
        return

    # 4. Включение
    if lower_text in ["!эли вкл", "/bot_on"]:
        if is_owner:
            active_chats[chat_id] = True
            await bot.send_message(
                chat_id=chat_id,
                text="Элизабет подключена ✨",
                business_connection_id=bus_id,
            )
        return

    # 5. Выключение
    if lower_text in ["!эли выкл", "/bot_off"]:
        if is_owner:
            active_chats[chat_id] = False
            await bot.send_message(
                chat_id=chat_id,
                text="Элизабет отключена 💤",
                business_connection_id=bus_id,
            )
        return

    # 6. Статус
    if lower_text in ["!эли инфо", "!эли статус"]:
        if is_owner:
            status = (
                "ВКЛЮЧЕНА ✨"
                if active_chats.get(chat_id, False)
                else "ВЫКЛЮЧЕНА 💤"
            )
            await bot.send_message(
                chat_id=chat_id,
                text=f"Статус: {status}",
                business_connection_id=bus_id,
            )
        return

    # 7. Остановка спама
    if lower_text in ["!эли стоп", "!эли стоп спам"]:
        if is_owner:
            if chat_id in spam_tasks and not spam_tasks[chat_id].done():
                spam_tasks[chat_id].cancel()
                spam_tasks.pop(chat_id, None)
                await bot.send_message(
                    chat_id=chat_id,
                    text="⏹ Спам остановлен.",
                    business_connection_id=bus_id,
                )
            else:
                await bot.send_message(
                    chat_id=chat_id,
                    text="Активного спама нет.",
                    business_connection_id=bus_id,
                )
        return

    # 8. Спам-функция
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

                if chat_id in spam_tasks and not spam_tasks[chat_id].done():
                    spam_tasks[chat_id].cancel()

                async def run_spam(c_id, b_id, msg, cnt):
                    try:
                        i = 0
                        while cnt is None or i < cnt:
                            await bot.send_message(
                                chat_id=c_id,
                                text=msg,
                                business_connection_id=b_id,
                            )
                            await asyncio.sleep(0.4)
                            i += 1
                    except asyncio.CancelledError:
                        pass
                    finally:
                        spam_tasks.pop(c_id, None)

                task = asyncio.create_task(
                    run_spam(chat_id, bus_id, spam_msg, count)
                )
                spam_tasks[chat_id] = task
        return

    # 9. Сброс истории чата
    if lower_text in ["!эли сброс", "!эли кэш"]:
        if is_owner:
            user_histories.pop(chat_id, None)
            await bot.send_message(
                chat_id=chat_id,
                text="Память чата очищена 🧹",
                business_connection_id=bus_id,
            )
        return

    # Ответ ИИ собеседнику
    if active_chats.get(chat_id, False):
        bot_info = await bot.get_me()
        if not is_owner and message.from_user.id != bot_info.id:
            await bot.send_chat_action(
                chat_id=chat_id, action="typing", business_connection_id=bus_id
            )
            reply = await ask_groq(
                message.text, chat_id, ELIZABETH_PROMPT_BUSINESS
            )
            await bot.send_message(
                chat_id=chat_id, text=reply, business_connection_id=bus_id
            )


# --- ЗАПУСК ---
async def main():
    if not BOT_TOKEN:
        print("Ошибка: Переменная TELEGRAM_BOT_TOKEN не задана!")
        return

    await start_web_server()
    print("Запуск бота через Groq API...")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
