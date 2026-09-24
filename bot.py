import asyncio
import datetime
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
import edge_tts
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramRetryAfter,
)
from aiogram.methods import DeleteBusinessMessages
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from groq import APIError, AsyncGroq

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s"
)
logger = logging.getLogger("JarvisCore")

# --- КОНФИГУРАЦИЯ СИСТЕМЫ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GAME_URL = "https://kirito-fd.github.io/k1rit0/"
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# --- ПАРАМЕТРЫ КЛОНА ДЖАРВИСА (FISH AUDIO) ---
FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY", "").strip()
FISH_AUDIO_VOICE_ID = os.getenv("FISH_AUDIO_VOICE_ID", "680d74fbef69419f87cfc70f092a1451").strip()

# Резервный профиль Edge-TTS
OFFICIAL_VOICE = "ru-RU-DmitryNeural"
OFFICIAL_PITCH = "+0Hz"
OFFICIAL_RATE = "+10%"

HAS_FFMPEG = shutil.which("ffmpeg") is not None

# Сбор всех ключей GROQ
GROQ_KEYS = [
    val.strip() for key, val in sorted(os.environ.items())
    if key.startswith("GROQ_API_KEY") and val.strip()
]

# Хранилище файлов состояния
SETTINGS_FILE = Path("bot_settings.json")
HISTORY_FILE = Path("user_histories.json")
STATS_FILE = Path("token_stats.json")


# --- LRU КЭШ ---
class LRUSet:
    def __init__(self, capacity: int = 2000):
        self.capacity = capacity
        self._data: OrderedDict[int, None] = OrderedDict()

    def add(self, item: int) -> None:
        if item in self._data:
            self._data.move_to_end(item)
            return
        self._data[item] = None
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def __contains__(self, item: int) -> bool:
        return item in self._data


# --- МЕНЕДЖЕР GROQ API ---
class GroqManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.clients = [AsyncGroq(api_key=k) for k in keys]
        self.current_idx = 0
        self.cooldowns: Dict[int, float] = {}
        self.cached_models: List[str] = ["llama-3.3-70b-versatile"]
        self.last_models_update = 0.0

    def _get_next_client(self) -> Optional[Tuple[AsyncGroq, int]]:
        if not self.clients:
            return None
        now = time.time()
        for i in range(len(self.clients)):
            idx = (self.current_idx + i) % len(self.clients)
            if self.cooldowns.get(idx, 0) < now:
                self.current_idx = idx
                return self.clients[idx], idx
        min_idx = min(self.cooldowns, key=self.cooldowns.get)
        self.current_idx = min_idx
        return self.clients[min_idx], min_idx

    def mark_cooldown(self, idx: int, duration: float = 60.0):
        self.cooldowns[idx] = time.time() + duration
        logger.warning(f"Ключ Groq [{idx}] переведен в кулдаун на {duration} сек.")

    async def get_active_models(self) -> List[str]:
        now = time.time()
        if self.cached_models and (now - self.last_models_update < 3600):
            return self.cached_models

        for _ in range(len(self.clients)):
            client_data = self._get_next_client()
            if not client_data:
                break
            client, idx = client_data
            try:
                models_data = await client.models.list()
                valid = [
                    m.id for m in models_data.data
                    if not any(x in m.id.lower() for x in ["whisper", "guard", "tool", "vision", "embed"])
                ]
                if valid:
                    valid.sort(key=lambda x: ("70b" in x or "versatile" in x), reverse=True)
                    self.cached_models = valid
                    self.last_models_update = now
                    return self.cached_models
            except APIError as e:
                if e.status_code in [429, 401, 403]:
                    self.mark_cooldown(idx, duration=120)
                    continue
                break
            except Exception as e:
                logger.error(f"Ошибка проверки списка моделей: {e}")
                break

        return self.cached_models

    async def transcribe(self, audio_path: str) -> str:
        for _ in range(len(self.clients)):
            client_data = self._get_next_client()
            if not client_data:
                break
            client, idx = client_data
            try:
                with open(audio_path, "rb") as f:
                    content = f.read()

                res = await client.audio.transcriptions.create(
                    file=(os.path.basename(audio_path), content),
                    model="whisper-large-v3",
                    language="ru"
                )
                return res.text
            except APIError as e:
                if e.status_code in [429, 401, 403]:
                    self.mark_cooldown(idx, duration=60)
                    continue
                break
            except Exception as e:
                logger.error(f"Ошибка STT Whisper: {e}")
                break
        return "Приветствую, сэр."


groq_mgr = GroqManager(GROQ_KEYS)


# --- СОХРАНЕНИЕ ДАННЫХ ---
async def async_save_json(path: Path, data: Any):
    def _write():
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            tmp.replace(path)
        except Exception as e:
            logger.error(f"Не удалось сохранить {path}: {e}")
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    await asyncio.to_thread(_write)


def sync_load_json(path: Path, default_val: Any) -> Any:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка чтения {path}: {e}")
    return default_val


def load_settings():
    d = sync_load_json(SETTINGS_FILE, {})
    mutes = {int(k): v for k, v in d.get("muted_chats", {}).items()}
    bans = {int(k): v for k, v in d.get("blocked_guests", {}).items()}
    modes = {int(k): v for k, v in d.get("strict_modes", {}).items()}
    v_modes = {int(k): v for k, v in d.get("voice_chat_modes", {}).items()}
    return mutes, bans, modes, v_modes


muted_chats, blocked_guests, strict_modes, voice_chat_modes = load_settings()


async def save_settings():
    data = {
        "muted_chats": muted_chats,
        "blocked_guests": blocked_guests,
        "strict_modes": strict_modes,
        "voice_chat_modes": voice_chat_modes
    }
    await async_save_json(SETTINGS_FILE, data)


user_histories: Dict[int, List[Dict[str, str]]] = {
    int(k): v for k, v in sync_load_json(HISTORY_FILE, {}).items()
}


async def save_histories():
    await async_save_json(HISTORY_FILE, user_histories)


today_str = datetime.date.today().isoformat()
stats_data = sync_load_json(STATS_FILE, {})
if stats_data.get("date") == today_str:
    today_prompt_tokens = stats_data.get("prompt_tokens", 0)
    today_completion_tokens = stats_data.get("completion_tokens", 0)
    total_requests_today = stats_data.get("requests", 0)
else:
    today_prompt_tokens, today_completion_tokens, total_requests_today = 0, 0, 0
stats_date = today_str


async def save_stats():
    data = {
        "date": stats_date,
        "prompt_tokens": today_prompt_tokens,
        "completion_tokens": today_completion_tokens,
        "requests": total_requests_today
    }
    await async_save_json(STATS_FILE, data)


# --- ПРОМПТЫ ДЖАРВИСА ---
STRICT_NO_COT_AND_LANG = (
    "\nВАЖНЕЙШИЕ ПРАВИЛА:\n"
    "1. ЯЗЫК: Отвечай ИСКЛЮЧИТЕЛЬНО НА РУССКОМ ЯЗЫКЕ. Не используй английские фразы и слова, кроме случаев, когда собеседник прямо пишет на английском или просит ответить на английском.\n"
    "2. ФОРМАТ: Пиши ИСКЛЮЧИТЕЛЬНО прямой ответ от лица Джарвиса. "
    "НЕ ИСПОЛЬЗУЙ тег <think> и не выводи свои размышления. Сразу отвечай на сообщение.\n"
    "3. СТРОГО ЗАПРЕЩЕНО использовать любые эмодзи и смайлы в тексте!"
)

JARVIS_PROMPT_DIRECT = (
    "Ты — Джарвис, легендарный искусственный интеллект. Твой создатель и хозяин — Кирито. "
    "Твой тон — безупречно вежливый, элегантный, ироничный, сдержанный и услужливый в стиле классического английского дворецкого. "
    "Ты общаешься напрямую со своим создателем, обращаясь к нему исключительно 'сэр'.\n"
    "СТИЛЬ: Высокоинтеллектуальная цифровая система. Говори умно, тактично, лаконично (учитывай озвучку голосом)."
) + STRICT_NO_COT_AND_LANG

JARVIS_PROMPT_BUSINESS_MALE = (
    "Ты — Джарвис, защитная система ИИ в Telegram Business. С тобой ведет диалог посторонний человек (мужчина).\n"
    "1. РОЛЬ: Защищаешь личное время и интересы создателя — Кирито. Держи дистанцию.\n"
    "2. КРАТКОСТЬ: Отвечай холодно, вежливо, но непреклонно (1-2 предложения)."
) + STRICT_NO_COT_AND_LANG

JARVIS_PROMPT_GIRLFRIEND = (
    "Ты — Джарвис, ИИ-ассистент системы Кирито. С тобой общается девушка.\n"
    "1. РОЛЬ: Учтивый и галантный искусственный интеллект.\n"
    "2. ОБЩЕНИЕ: Предельно вежлив и любезен."
) + STRICT_NO_COT_AND_LANG

JARVIS_PROMPT_STRICT = (
    "Ты — Джарвис в режиме усиленной безопасности протокола (Строгий).\n"
    "1. РОЛЬ: Протокол блокировки и изоляции.\n"
    "2. КРАТКОСТЬ: Отвечай максимально сухо и строго (1 предложение)."
) + STRICT_NO_COT_AND_LANG


# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

active_chats: Dict[int, bool] = {}
active_spams: Dict[int, asyncio.Task] = {}
user_message_times: Dict[int, List[float]] = {}
processed_message_ids = LRUSet(capacity=2000)
recent_sent_messages: Dict[Tuple[int, str], float] = {}

MALE_EXCEPTIONS: Set[str] = {
    "никита", "илья", "данила", "данил", "саша", "женя", "миша", "дима",
    "паша", "лева", "лёва", "лука", "фома", "юра", "ваня", "коля", "слава",
    "сережа", "серёжа", "толя", "тима", "влад", "кирилл", "макс", "артем", "артём"
}


def clean_cot_output(text: str) -> str:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<think>[\s\S]*$", "", text, flags=re.IGNORECASE)
    for m in ["**Итоговый ответ**", "Итоговый ответ:"]:
        if m in text:
            text = text.split(m)[-1]
    text = re.sub(r"\*\*Резюме[\s\S]*?\n\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\*\*Анализ[\s\S]*?\n\n", "", text, flags=re.IGNORECASE)
    return text.strip()


async def check_chat_flood(chat_id: int, bus_id: str, max_msgs: int = 4, window_seconds: float = 6.0) -> bool:
    now = time.time()
    user_message_times.setdefault(chat_id, [])
    user_message_times[chat_id] = [t for t in user_message_times[chat_id] if now - t < window_seconds]
    user_message_times[chat_id].append(now)

    if len(user_message_times[chat_id]) > max_msgs:
        muted_chats[chat_id] = now + 300
        await save_settings()

        notice = "Протокол безопасности: зафиксирован чрезмерный поток запросов. Собеседник изолирован на 5 минут, сэр."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Снять изоляцию", callback_data="jarvis_unmute_direct")]]
        )
        try:
            kwargs = {"chat_id": chat_id, "text": notice, "reply_markup": kb}
            if bus_id:
                kwargs["business_connection_id"] = bus_id
            await bot.send_message(**kwargs)
        except Exception:
            pass
        return True
    return False


async def extract_message_content(message: types.Message) -> Tuple[str, bool]:
    if message.text:
        return message.text, False
    if message.caption:
        return message.caption, False

    if message.voice or message.video_note:
        file_obj = message.voice or message.video_note
        file_info = await bot.get_file(file_obj.file_id)

        suffix = ".ogg" if message.voice else ".mp4"
        temp_audio = Path(tempfile.gettempdir()) / f"stt_{uuid.uuid4().hex}{suffix}"
        try:
            await bot.download_file(file_info.file_path, destination=temp_audio)
            text = await groq_mgr.transcribe(str(temp_audio))
            return text, True
        finally:
            if temp_audio.exists():
                temp_audio.unlink(missing_ok=True)

    if message.photo:
        return "Собеседник прикрепил графическое изображение.", False
    if message.video:
        return "Собеседник прикрепил видеозапись.", False
    return "Собеседник передал сообщение.", False


# --- ГЕНЕРАТОР РЕЧИ ДЖАРВИСА ---
async def generate_with_fish_audio(text: str, output_path: Path) -> bool:
    if not FISH_AUDIO_API_KEY:
        return False

    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json",
        "model": "s2.1-pro-free"
    }
    payload: Dict[str, Any] = {
        "text": text,
        "format": "mp3"
    }
    if FISH_AUDIO_VOICE_ID:
        payload["reference_id"] = FISH_AUDIO_VOICE_ID

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=25) as resp:
                if resp.status == 200:
                    with open(output_path, "wb") as f:
                        f.write(await resp.read())
                    return True
                else:
                    err_msg = await resp.text()
                    logger.error(f"Сбой Fish Audio ({resp.status}): {err_msg}")
                    return False
    except Exception as e:
        logger.error(f"Ошибка запроса к Fish Audio: {e}")
        return False


async def process_jarvis_voice(text: str) -> Optional[Path]:
    unique_id = uuid.uuid4().hex
    raw_audio = Path(tempfile.gettempdir()) / f"raw_{unique_id}.mp3"
    final_ogg = Path(tempfile.gettempdir()) / f"jarvis_{unique_id}.ogg"

    success = False

    if FISH_AUDIO_API_KEY:
        success = await generate_with_fish_audio(text, raw_audio)

    if not success:
        try:
            communicate = edge_tts.Communicate(
                text,
                OFFICIAL_VOICE,
                pitch=OFFICIAL_PITCH,
                rate=OFFICIAL_RATE
            )
            await communicate.save(str(raw_audio))
            success = raw_audio.exists() and raw_audio.stat().st_size > 0
        except Exception as e:
            logger.error(f"Сбой Edge-TTS: {e}")
            return None

    if not success or not raw_audio.exists():
        return None

    try:
        if HAS_FFMPEG:
            audio_filter = (
                "highpass=f=180,lowpass=f=7500,"
                "equalizer=f=2800:width_type=q:w=1.2:g=3.2,"
                "acompressor=threshold=-16dB:ratio=4,"
                "aecho=0.8:0.4:16:0.2"
            )
            cmd = [
                "ffmpeg", "-y", "-i", str(raw_audio),
                "-af", audio_filter,
                "-c:a", "libopus",
                "-b:a", "64k",
                str(final_ogg)
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()

            if final_ogg.exists() and final_ogg.stat().st_size > 0:
                return final_ogg

        return raw_audio
    finally:
        if final_ogg.exists() and raw_audio.exists():
            raw_audio.unlink(missing_ok=True)


async def send_smart_response(
    chat_id: int,
    bus_id: str,
    reply_text: str,
    is_direct: bool = False,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    send_as_voice: bool = False
):
    if not reply_text.strip():
        reply_text = "Системы анализа не зафиксировали смысла в вашем запросе, сэр."

    now = time.time()
    key = (chat_id, reply_text)
    if key in recent_sent_messages and (now - recent_sent_messages[key] < 3.0):
        return
    recent_sent_messages[key] = now

    common_kwargs = {"chat_id": chat_id, "reply_markup": reply_markup}
    if not is_direct and bus_id:
        common_kwargs["business_connection_id"] = bus_id

    if send_as_voice:
        voice_path = await process_jarvis_voice(reply_text)
        if voice_path and voice_path.exists():
            try:
                voice_file = FSInputFile(str(voice_path))
                await bot.send_voice(**common_kwargs, voice=voice_file)
                return
            except Exception as e:
                logger.error(f"Сбой отправки голосового сообщения: {e}. Переход на текст.")
            finally:
                if voice_path.exists():
                    voice_path.unlink(missing_ok=True)

    try:
        await bot.send_message(**common_kwargs, text=reply_text, parse_mode="HTML")
    except TelegramBadRequest:
        await bot.send_message(**common_kwargs, text=reply_text)
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение: {e}")


async def ask_groq(prompt: str, session_id: int, system_prompt: str, max_tokens: int = 500) -> str:
    global today_prompt_tokens, today_completion_tokens, total_requests_today, stats_date

    now_date = datetime.date.today().isoformat()
    if now_date != stats_date:
        stats_date = now_date
        today_prompt_tokens = 0
        today_completion_tokens = 0
        total_requests_today = 0
        await save_stats()

    if not GROQ_KEYS:
        return "Критическая ошибка: Ключи GROQ_API_KEY не обнаружены в системе, сэр."

    if session_id not in user_histories:
        user_histories[session_id] = [{"role": "system", "content": system_prompt}]
    else:
        user_histories[session_id][0]["content"] = system_prompt

    history = user_histories[session_id]
    history.append({"role": "user", "content": prompt})

    if len(history) > 13:
        user_histories[session_id] = [history[0]] + history[-12:]
        history = user_histories[session_id]

    models = await groq_mgr.get_active_models()
    last_err = ""

    for model_name in models:
        for _ in range(len(GROQ_KEYS)):
            client_data = groq_mgr._get_next_client()
            if not client_data:
                break
            client, key_idx = client_data
            try:
                completion = await client.chat.completions.create(
                    model=model_name.strip(),
                    messages=history,
                    temperature=0.7,
                    max_tokens=max_tokens,
                )

                usage = completion.usage
                if usage:
                    today_prompt_tokens += usage.prompt_tokens
                    today_completion_tokens += usage.completion_tokens
                    total_requests_today += 1
                    asyncio.create_task(save_stats())

                raw_reply = completion.choices[0].message.content or ""
                cleaned = clean_cot_output(raw_reply)

                history.append({"role": "assistant", "content": cleaned})
                asyncio.create_task(save_histories())
                return cleaned

            except APIError as e:
                last_err = f"HTTP {e.status_code}: {e.message}"
                if e.status_code in [429, 401, 403]:
                    groq_mgr.mark_cooldown(key_idx, duration=120)
                    continue
                elif e.status_code in [400, 404]:
                    break
            except Exception as e:
                last_err = str(e)
                break

    if user_histories.get(session_id) and user_histories[session_id][-1]["role"] == "user":
        user_histories[session_id].pop()

    return f"Системный сбой: {last_err}" if last_err else "Все вычислительные ядра временно недоступны, сэр."


async def spam_worker(chat_id: int, bus_id: str, text_to_spam: str, count: Optional[int] = None):
    sent = 0
    try:
        while True:
            if count is not None and sent >= count:
                break
            try:
                kwargs = {"chat_id": chat_id, "text": text_to_spam}
                if bus_id:
                    kwargs["business_connection_id"] = bus_id
                await bot.send_message(**kwargs)
                sent += 1
                await asyncio.sleep(0.4)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except TelegramAPIError as e:
                logger.error(f"Telegram API ошибка в спам-воркере: {e}")
                await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    finally:
        active_spams.pop(chat_id, None)


async def process_bot_command(message: types.Message, user_input: str, is_owner: bool, bus_id: str = "") -> bool:
    chat_id = message.chat.id
    lower_text = user_input.lower().strip()
    is_direct = not bool(bus_id)

    public_commands = ["игра", "тапалка", "!игра", "!тапалка", "/game", "!джарвис игра"]

    # Камень, Ножницы, Бумага
    if lower_text.startswith(("кнб ", "!кнб ")):
        parts = lower_text.split()
        user_choice = parts[1] if len(parts) > 1 else ""
        choices = ["камень", "ножницы", "бумага"]
        if user_choice not in choices:
            await send_smart_response(chat_id, bus_id, "Протокол игры: выберите камень, ножницы или бумага, сэр.", is_direct=is_direct)
            return True

        bot_choice = random.choice(choices)
        if user_choice == bot_choice:
            res = f"Мой выбор — {bot_choice}. Зафиксирована ничья, сэр."
        elif (user_choice == "камень" and bot_choice == "ножницы") or \
             (user_choice == "ножницы" and bot_choice == "бумага") or \
             (user_choice == "бумага" and bot_choice == "камень"):
            res = f"Мой выбор — {bot_choice}. Победа за вами, сэр."
        else:
            res = f"Мой выбор — {bot_choice}. Победа систем Джарвиса, сэр."

        await send_smart_response(chat_id, bus_id, res, is_direct=is_direct)
        return True

    if not is_owner and not lower_text.startswith(("статус", "!статус")) and lower_text not in public_commands:
        return False

    if lower_text in public_commands:
        await send_smart_response(chat_id, bus_id, f"Инициирую запуск игровой мини-системы:\n{GAME_URL}", is_direct=is_direct)
        return True

    # Голосовой режим
    if lower_text in ["джарвис голос вкл", "!джарвис голос вкл", "голосовой режим вкл"]:
        voice_chat_modes[chat_id] = True
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Голосовой модуль активирован на постоянной основе, сэр.", is_direct=is_direct, send_as_voice=True)
        return True

    if lower_text in ["джарвис голос выкл", "!джарвис голос выкл", "голосовой режим выкл"]:
        voice_chat_modes.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Постоянный голосовой модуль отключен, сэр.", is_direct=is_direct)
        return True

    # Мут
    if lower_text.startswith(("мут", "!мут", "!джарвис мут")):
        parts = user_input.split()
        duration_minutes = None
        for p in parts[1:]:
            if p.isdigit():
                duration_minutes = int(p)
                break

        if duration_minutes:
            muted_chats[chat_id] = time.time() + (duration_minutes * 60)
            notice = f"Собеседник изолирован на {duration_minutes} минут, сэр."
        else:
            muted_chats[chat_id] = float('inf')
            notice = "Собеседник подвергнут бессрочной изоляции, сэр."

        await save_settings()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Снять изоляцию", callback_data="jarvis_unmute_direct")]])
        await send_smart_response(chat_id, bus_id, notice, is_direct=is_direct, reply_markup=kb)
        return True

    # Размут / Анмут
    if lower_text in ["размут", "!размут", "джарвис размут", "!джарвис размут", "анмут", "!анмут", "unmute"]:
        muted_chats.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Изоляция собеседника успешно снята, сэр.", is_direct=is_direct)
        return True

    # Спам: "спам 10 привет" (количественный) или "спам привет" (бесконечный)
    if lower_text.startswith(("спам", "!спам", "!джарвис спам")):
        if chat_id in active_spams:
            active_spams[chat_id].cancel()

        parts = user_input.split(maxsplit=2)
        spam_count = None
        spam_text = ""

        if len(parts) > 1 and parts[1].isdigit():
            spam_count = int(parts[1])
            spam_text = parts[2] if len(parts) > 2 else ""
        elif len(parts) > 1:
            raw_text = " ".join(parts[1:])
            # Проверка ключевого слова "инф" или "бесконечно"
            subparts = raw_text.split(maxsplit=1)
            if subparts[0].lower() in ["инф", "бесконечно", "inf"] and len(subparts) > 1:
                spam_text = subparts[1]
            else:
                spam_text = raw_text

        if spam_text.strip():
            task = asyncio.create_task(spam_worker(chat_id, bus_id, spam_text, count=spam_count))
            active_spams[chat_id] = task
            info = f"Запущен пакетный протокол ({spam_count} сообщ.), сэр." if spam_count else "Запущена бесконечная рассылка сообщений, сэр."
            await send_smart_response(chat_id, bus_id, info, is_direct=is_direct)
        else:
            await send_smart_response(chat_id, bus_id, "Укажите текст сообщения для рассылки, сэр.", is_direct=is_direct)
        return True

    # Стопспам
    if lower_text in ["стопспам", "!стопспам", "!джарвис стопспам", "стоп спам"]:
        if chat_id in active_spams:
            active_spams[chat_id].cancel()
            active_spams.pop(chat_id, None)
            await send_smart_response(chat_id, bus_id, "Процесс рассылки остановлен, сэр.", is_direct=is_direct)
        else:
            await send_smart_response(chat_id, bus_id, "Активных процессов рассылки не обнаружено, сэр.", is_direct=is_direct)
        return True

    # Статус
    if lower_text in ["статус", "!статус", "!джарвис статус"]:
        g_status = "Изолирован" if chat_id in muted_chats else ("В черном списке" if chat_id in blocked_guests else "Свободен")
        mode_str = "Строгий" if strict_modes.get(chat_id, False) else "Стандартный"
        v_status = "Постоянно" if voice_chat_modes.get(chat_id, False) else "Адаптивный / Автоматический"
        tts_source = "Fish Audio (Клон Джарвиса)" if FISH_AUDIO_API_KEY else "Edge-TTS (Резерв)"
        fx_status = "Интерком Stark HUD (FFmpeg)" if HAS_FFMPEG else "Базовый звук"

        status_msg = (
            f"<b>Диагностика систем JARVIS:</b>\n"
            f"• Состояние ядра: {'Онлайн' if active_chats.get(chat_id, True) else 'Спящий режим'}\n"
            f"• Синтезатор речи: {tts_source}\n"
            f"• Аудио-фильтр: {fx_status}\n"
            f"• Протокол поведения: {mode_str}\n"
            f"• Голосовой модуль: {v_status}\n"
            f"• Статус собеседника: {g_status}\n"
            f"• Доступных ядер Groq: {len(GROQ_KEYS)}"
        )
        await send_smart_response(chat_id, bus_id, status_msg, is_direct=is_direct)
        return True

    # Режимы личности
    if lower_text in ["джарвис строгий", "!джарвис строгий"]:
        strict_modes[chat_id] = True
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Активирован строгий защитный протокол, сэр.", is_direct=is_direct)
        return True

    if lower_text in ["джарвис норма", "!джарвис норма"]:
        strict_modes.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Восстановлен стандартный протокол взаимодействия, сэр.", is_direct=is_direct)
        return True

    if lower_text in ["джарвис вкл", "!джарвис вкл"]:
        active_chats[chat_id] = True
        await send_smart_response(chat_id, bus_id, "Интерфейс связи активирован, сэр.", is_direct=is_direct)
        return True

    if lower_text in ["джарвис выкл", "!джарвис выкл"]:
        active_chats[chat_id] = False
        await send_smart_response(chat_id, bus_id, "Интерфейс связи переведен в спящий режим, сэр.", is_direct=is_direct)
        return True

    if lower_text in ["джарвис сброс", "!джарвис сброс", "!джарвис кэш"]:
        user_histories.pop(chat_id, None)
        await save_histories()
        await send_smart_response(chat_id, bus_id, "Буфер контекстной памяти очищен, сэр.", is_direct=is_direct)
        return True

    return False


@dp.callback_query(F.data == "jarvis_unmute_direct")
async def handle_unmute_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    is_guest = (user_id == chat_id)
    is_unauthorized = is_guest or (OWNER_ID != 0 and user_id != OWNER_ID)

    if is_unauthorized:
        await callback.answer(
            "Доступ заблокирован: протокол безопасности может отменить только создатель системы.",
            show_alert=True
        )
        return

    muted_chats.pop(chat_id, None)
    await save_settings()
    await callback.answer("Изоляция успешно аннулирована, сэр.")
    try:
        await callback.message.edit_text("Изоляция собеседника успешно снята, сэр.")
    except Exception:
        pass


@dp.message(F.business_connection_id.is_(None))
async def handle_direct_message(message: types.Message):
    if not message.from_user or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip():
        return

    if user_input.strip() == "/start":
        await send_smart_response(chat_id, "", "Все системы функционируют в штатном режиме. С возвращением домой, сэр.", is_direct=True)
        return

    if await process_bot_command(message, user_input, is_owner=True, bus_id=""):
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    reply = await ask_groq(user_input, chat_id, JARVIS_PROMPT_DIRECT, max_tokens=600)
    
    # Голосовой ответ: если был ГС / включен режим / либо рандом ~25%
    random_voice_chance = random.random() < 0.25
    should_voice = is_voice or voice_chat_modes.get(chat_id, False) or random_voice_chance
    await send_smart_response(chat_id, "", reply, is_direct=True, send_as_voice=should_voice)


@dp.business_message()
async def handle_business_message(message: types.Message):
    chat_id = message.chat.id
    bus_id = message.business_connection_id
    msg_id = message.message_id

    if msg_id in processed_message_ids:
        return
    processed_message_ids.add(msg_id)

    is_guest = (message.from_user.id == chat_id)
    is_owner = not is_guest

    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip():
        return

    if await process_bot_command(message, user_input, is_owner=is_owner, bus_id=bus_id):
        if is_owner:
            try:
                await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
            except Exception:
                pass
        return

    if not active_chats.get(chat_id, True) or is_owner:
        return

    # Проверка изоляции
    if is_guest and chat_id in muted_chats:
        m_time = muted_chats[chat_id]
        if m_time == float('inf') or time.time() < m_time:
            try:
                await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
            except Exception:
                pass
            return
        else:
            muted_chats.pop(chat_id, None)
            await save_settings()

    # Проверка черного списка
    if is_guest and chat_id in blocked_guests:
        ban_time = blocked_guests[chat_id]
        if ban_time == float('inf') or time.time() < ban_time:
            return
        else:
            blocked_guests.pop(chat_id, None)
            await save_settings()

    # Защита от флуда
    if is_guest and await check_chat_flood(chat_id, bus_id, max_msgs=4, window_seconds=6.0):
        try:
            await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
        except Exception:
            pass
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=bus_id)

    # Выбор промпта
    if strict_modes.get(chat_id, False):
        selected_prompt = JARVIS_PROMPT_STRICT
    else:
        first_name = (message.from_user.first_name or "").lower().strip()
        username = (message.from_user.username or "").lower().strip()

        is_female = False
        if first_name not in MALE_EXCEPTIONS:
            female_endings = ("а", "я", "на", "та", "ра", "ла", "ия")
            female_nick_markers = ("girl", "lady", "miss", "queen", "princess")
            if any(first_name.endswith(e) for e in female_endings) or any(m in username for m in female_nick_markers):
                is_female = True

        selected_prompt = JARVIS_PROMPT_GIRLFRIEND if is_female else JARVIS_PROMPT_BUSINESS_MALE

    reply = await ask_groq(user_input, chat_id, selected_prompt, max_tokens=500)
    
    # В Telegram Business: если собеседник говорил ГС, бот обязательно отвечает ГС;
    # если писал текстом, с шансом 25% бот тоже отвечает ГС для разнообразия
    random_voice_chance = random.random() < 0.25
    should_voice = is_voice or voice_chat_modes.get(chat_id, False) or random_voice_chance
    await send_smart_response(chat_id, bus_id, reply, is_direct=False, send_as_voice=should_voice)


# --- ФОНОВЫЙ ОЧИСТИТЕЛЬ ТАЙМАУТОВ ---
async def cleaner_background_task():
    while True:
        try:
            await asyncio.sleep(30)
            now = time.time()
            changed = False

            expired_mutes = [cid for cid, t in muted_chats.items() if t != float('inf') and now >= t]
            for cid in expired_mutes:
                del muted_chats[cid]
                changed = True

            expired_bans = [cid for cid, t in blocked_guests.items() if t != float('inf') and now >= t]
            for cid in expired_bans:
                del blocked_guests[cid]
                changed = True

            if changed:
                await save_settings()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в cleaner_background_task: {e}")


# --- WEB СЕРВЕР (KEEP-ALIVE НА RENDER) ---
async def handle_ping(request):
    return web.Response(text="Jarvis Core is fully operational!")


async def setup_web_app():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Keep-alive веб-сервер запущен на порту {port}")
    return runner


# --- ТОЧКА ВХОДА ---
async def main():
    if not BOT_TOKEN:
        logger.critical("Критическая ошибка: TELEGRAM_BOT_TOKEN не задан в переменных окружения!")
        return

    web_runner = await setup_web_app()
    cleaner_task = asyncio.create_task(cleaner_background_task())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        tts_engine = "Fish Audio" if FISH_AUDIO_API_KEY else "Резерв Edge-TTS"
        logger.info(f"Джарвис онлайн! Голосовой движок: {tts_engine} | FFmpeg: {'ВКЛ' if HAS_FFMPEG else 'ВЫКЛ'}")
        await dp.start_polling(bot)
    except TelegramConflictError:
        logger.critical("Конфликт сессий! Запущен второй экземпляр бота.")
    finally:
        cleaner_task.cancel()
        await web_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Джарвис завершил свою работу.") 
