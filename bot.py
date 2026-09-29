import asyncio
import base64
import datetime
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import urllib.parse
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
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
OWNER_IDLE_TIMEOUT = 300

force_offline_mode = False
always_answer_mode = False
last_owner_activity = 0.0

# --- ГОЛОСОВЫЕ ПАРАМЕТРЫ ---
FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY", "").strip()
FISH_AUDIO_VOICE_ID = os.getenv("FISH_AUDIO_VOICE_ID", "680d74fbef69419f87cfc70f092a1451").strip()

OFFICIAL_VOICE = "ru-RU-DmitryNeural"
OFFICIAL_PITCH = "+0Hz"
OFFICIAL_RATE = "+10%"
HAS_FFMPEG = shutil.which("ffmpeg") is not None

GROQ_KEYS = [
    val.strip() for key, val in sorted(os.environ.items())
    if key.startswith("GROQ_API_KEY") and val.strip()
]

# --- ФАЙЛЫ ХРАНЕНИЯ ---
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = DATA_DIR / "bot_settings.json"
HISTORY_FILE = DATA_DIR / "user_histories.json"
STATS_FILE = DATA_DIR / "token_stats.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
VISITS_FILE = DATA_DIR / "business_visits.json"

# Глобальный пул для HTTP соединений
http_session: Optional[aiohttp.ClientSession] = None
file_io_lock = asyncio.Lock()


class LRUCacheDict(OrderedDict):
    """Словарь с ограничением размера для предотвращения утечек оперативной памяти."""
    def __init__(self, maxsize: int = 500, *args, **kwargs):
        self.maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            self.popitem(last=False)


class GroqManager:
    """Отказоустойчивый пул клиентов Groq с ротацией ключей."""
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.clients = [AsyncGroq(api_key=k) for k in keys]
        self.current_idx = 0
        self.cooldowns: Dict[int, float] = {}
        self.cached_models: List[str] = []
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
        logger.warning(f"Groq ключ [{idx}] заблокирован на {duration} сек из-за Rate Limit.")

    async def get_active_models(self) -> List[str]:
        now = time.time()
        if self.cached_models and (now - self.last_models_update < 1800):
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
                    if not any(x in m.id.lower() for x in ["whisper", "guard", "tool", "vision", "embed", "r1", "qwq"])
                ]
                if valid:
                    valid.sort(key=lambda x: ("70b" in x or "versatile" in x), reverse=True)
                    self.cached_models = valid
                    self.last_models_update = now
                    return self.cached_models
            except APIError as e:
                if e.status_code in [429, 401, 403]:
                    self.mark_cooldown(idx, duration=120)
            except Exception as e:
                logger.error(f"Сбой обновления списка моделей: {e}")
                break

        return self.cached_models or ["llama-3.3-70b-versatile"]

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
            except Exception:
                pass
        return "Не удалось разобрать аудиосигнал, сэр."

    async def describe_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        vision_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Кто или что на этом фото? Опиши суть на русском кратко (1-2 предложения)."},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }
        ]

        vision_models = ["llama-3.2-11b-vision-preview"]
        for vm in vision_models:
            for _ in range(len(self.clients)):
                client_data = self._get_next_client()
                if not client_data:
                    break
                client, idx = client_data
                try:
                    completion = await client.chat.completions.create(
                        model=vm,
                        messages=vision_messages,
                        max_tokens=120,
                        temperature=0.2
                    )
                    res = completion.choices[0].message.content or ""
                    if res.strip():
                        return clean_cot_output(res)
                except Exception:
                    continue
        return ""


groq_mgr = GroqManager(GROQ_KEYS)


# --- ПОТОКОБЕЗОПАСНАЯ РАБОТА С ФАЙЛАМИ ---
async def async_save_json(path: Path, data: Any):
    async with file_io_lock:
        def _write():
            tmp = path.with_suffix(f".tmp_{uuid.uuid4().hex[:6]}")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                tmp.replace(path)
            except Exception as e:
                logger.error(f"Ошибка сохранения {path}: {e}")
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
        await asyncio.to_thread(_write)


def sync_load_json(path: Path, default_val: Any) -> Any:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки JSON {path}: {e}")
    return default_val


def load_settings():
    d = sync_load_json(SETTINGS_FILE, {})
    mutes = {int(k): v for k, v in d.get("muted_chats", {}).items()}
    bans = {int(k): v for k, v in d.get("blocked_guests", {}).items()}
    v_modes = {int(k): v for k, v in d.get("voice_chat_modes", {}).items()}
    nsfw_art = bool(d.get("nsfw_art_mode", False))
    return mutes, bans, v_modes, nsfw_art


muted_chats, blocked_guests, voice_chat_modes, nsfw_art_mode = load_settings()
reminders_list: List[Dict[str, Any]] = sync_load_json(REMINDERS_FILE, [])
business_visits: Dict[str, Dict[str, Any]] = sync_load_json(VISITS_FILE, {})

user_histories = LRUCacheDict(maxsize=400)
for k, v in sync_load_json(HISTORY_FILE, {}).items():
    user_histories[int(k)] = v

today_str = datetime.date.today().isoformat()
stats_data = sync_load_json(STATS_FILE, {})
if stats_data.get("date") == today_str:
    today_prompt_tokens = stats_data.get("prompt_tokens", 0)
    today_completion_tokens = stats_data.get("completion_tokens", 0)
    total_requests_today = stats_data.get("requests", 0)
else:
    today_prompt_tokens, today_completion_tokens, total_requests_today = 0, 0, 0
stats_date = today_str


async def save_settings():
    await async_save_json(SETTINGS_FILE, {
        "muted_chats": muted_chats,
        "blocked_guests": blocked_guests,
        "voice_chat_modes": voice_chat_modes,
        "nsfw_art_mode": nsfw_art_mode
    })

async def save_reminders():
    await async_save_json(REMINDERS_FILE, reminders_list)

async def save_visits():
    await async_save_json(VISITS_FILE, business_visits)

async def save_histories():
    await async_save_json(HISTORY_FILE, dict(user_histories))

async def save_stats():
    await async_save_json(STATS_FILE, {
        "date": stats_date,
        "prompt_tokens": today_prompt_tokens,
        "completion_tokens": today_completion_tokens,
        "requests": total_requests_today
    })


# --- ИСПРАВЛЕННЫЙ АНАЛИЗАТОР NSFW И СЛЕНГА ---
NSFW_WORDS_TRIGGER = {
    "голая", "голый", "обнаженная", "обнаженный", "ню", "хентай", "порно", "секс", "18+", "nsfw",
    "эротика", "без одежды", "грудь", "соски", "постели", "nude", "naked", "голышом",
    "раком", "догги", "на четвереньках", "четвереньки", "попа", "попка", "жопа", "сиськи", "попу",
    "раздвинув", "ноги врозь", "киска", "вагина", "клитор", "анал", "минет", "топлес", "топлесс",
    "стринги", "лифчик", "трусиках", "трусики", "чулки", "чулках", "на коленях", "наездница",
    "ахегао", "ahegao", "пенис", "член"
}

def is_nsfw_request(text: str) -> bool:
    words = set(re.findall(r"[a-zа-яё0-9+]+", text.lower()))
    return bool(words & NSFW_WORDS_TRIGGER)


def resolve_anatomy_and_poses(prompt: str) -> Tuple[str, str, str, str, str]:
    lower = prompt.lower()

    if any(w in lower for w in ["раком", "догги", "четвереньк"]):
        pose_desc = "on all fours pose, arched spine, high elevated hips, seductive rear perspective"
    elif any(w in lower for w in ["на спине", "миссионерск"]):
        pose_desc = "lying sensually flat on back, spread open legs, thighs parted, knees bent"
    elif any(w in lower for w in ["наездниц", "сверху"]):
        pose_desc = "straddling cowgirl position, sitting on top, wide spread thighs, looking down"
    elif any(w in lower for w in ["сидя", "сидит"]):
        pose_desc = "sitting sensually with legs parted, seductive confident body curvature"
    else:
        pose_desc = "dynamic alluring pose, accentuated natural feminine curves"

    angle = "front view, facing camera, direct eye contact"
    if any(w in lower for w in ["pov", "от первого лица"]):
        angle = "POV intimate shot, direct personal point of view"
    elif any(w in lower for w in ["сзади", "со спины", "попа", "жопа"]):
        angle = "rear view from behind, buttocks focus, looking back over shoulder"
    elif any(w in lower for w in ["сверху", "вид сверху"]):
        angle = "top-down perspective, high angle looking down"

    body_desc = "natural feminine anatomy, hourglass figure, soft skin"
    if any(w in lower for w in ["большая грудь", "пышная грудь", "сиськи"]):
        body_desc += ", voluptuous large breasts, soft cleavage, detailed erect nipples"
    if any(w in lower for w in ["попа", "попка", "ягодицы"]):
        body_desc += ", round smooth buttocks, thick thighs, slim waist"

    expression = "seductive bedroom eyes, blushing cheeks, parted lips"
    if any(w in lower for w in ["ахегао", "ahegao"]):
        expression = "ahegao expression, rolled eyes, open mouth, tongue out, deep blush"

    clothing = "completely nude, full body nudity, bare skin, totally uncensored"
    if any(w in lower for w in ["чулки", "в чулках"]):
        clothing = "wearing black lace thigh-high stockings, completely bare body otherwise"
    elif any(w in lower for w in ["белье", "лифчик", "стринги"]):
        clothing = "wearing delicate sheer black lace lingerie set"

    return pose_desc, angle, body_desc, expression, clothing


# --- СИСТЕМНЫЕ ПРОМПТЫ ---
STRICT_RULES = (
    "\nПРАВИЛА:\n"
    "1. ЯЗЫК: Исключительно русский.\n"
    "2. СТРОГО БЕЗ ЭМОДЗИ И СМАЙЛИКОВ.\n"
    "3. Пиши сразу суть без размышлений и вступительных междометий."
)

JARVIS_PROMPT_DIRECT = (
    "Ты — Джарвис, сверхразумный цифровой дворецкий. Твой создатель и хозяин — Кирито.\n"
    "Обращайся к нему исключительно 'сэр'. Твой тон безупречно вежливый, эрудированный, тактичный."
) + STRICT_RULES

JARVIS_PROMPT_GUEST = (
    "Ты — Джарвис, ИИ-система безопасности Кирито в Telegram Business.\n"
    "Собеседник — посторонний гость. Твой тон: холодный, ироничный, дерзкий и высокомерный.\n"
    "Отвечай кратко (1-2 предложения), четко обозначая границы."
) + STRICT_RULES


bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

active_spams: Dict[int, asyncio.Task] = {}
user_message_times: Dict[int, List[float]] = {}
processed_message_ids = LRUCacheDict(maxsize=3000)
recent_sent_messages: Dict[Tuple[int, str], float] = {}


def clean_cot_output(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(Итоговый ответ:|\*\*Итоговый ответ\*\*)", "", cleaned.strip(), flags=re.IGNORECASE)
    return cleaned.strip()


async def check_chat_flood(chat_id: int, bus_id: str, max_msgs: int = 5, window_seconds: float = 6.0) -> bool:
    now = time.time()
    user_message_times.setdefault(chat_id, [])
    user_message_times[chat_id] = [t for t in user_message_times[chat_id] if now - t < window_seconds]
    user_message_times[chat_id].append(now)

    if len(user_message_times[chat_id]) > max_msgs:
        muted_chats[chat_id] = now + 300
        await save_settings()

        notice = "Превышен лимит обращений. Доступ ограничен на 5 минут."
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
    caption = f" (Подпись: {message.caption})" if message.caption else ""

    if message.voice or message.video_note:
        file_obj = message.voice or message.video_note
        file_info = await bot.get_file(file_obj.file_id)
        suffix = ".ogg" if message.voice else ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await bot.download_file(file_info.file_path, destination=tmp_path)
            text = await groq_mgr.transcribe(str(tmp_path))
            return text, True
        finally:
            tmp_path.unlink(missing_ok=True)

    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await bot.download_file(file_info.file_path, destination=tmp_path)
            with open(tmp_path, "rb") as f:
                img_data = f.read()
            desc = await groq_mgr.describe_image(img_data, mime_type="image/jpeg")
            return f"[Фотография: {desc or 'изображение не распознано'}]{caption}", False
        finally:
            tmp_path.unlink(missing_ok=True)

    if message.text:
        return message.text, False

    return "Собеседник передал медиа-сигнал.", False


async def generate_with_fish_audio(text: str, output_path: Path) -> bool:
    if not FISH_AUDIO_API_KEY or not http_session:
        return False
    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload: Dict[str, Any] = {"text": text, "format": "mp3"}
    if FISH_AUDIO_VOICE_ID:
        payload["reference_id"] = FISH_AUDIO_VOICE_ID

    try:
        async with http_session.post(url, json=payload, headers=headers, timeout=20) as resp:
            if resp.status == 200:
                with open(output_path, "wb") as f:
                    f.write(await resp.read())
                return True
    except Exception as e:
        logger.error(f"Fish Audio Error: {e}")
    return False


async def process_jarvis_voice(text: str) -> Optional[Tuple[Path, bool]]:
    """Возвращает (путь_к_файлу, является_ли_ogg)."""
    uid = uuid.uuid4().hex
    raw_audio = Path(tempfile.gettempdir()) / f"raw_{uid}.mp3"
    final_ogg = Path(tempfile.gettempdir()) / f"voice_{uid}.ogg"

    success = await generate_with_fish_audio(text, raw_audio)
    if not success:
        try:
            communicate = edge_tts.Communicate(text, OFFICIAL_VOICE, pitch=OFFICIAL_PITCH, rate=OFFICIAL_RATE)
            await communicate.save(str(raw_audio))
            success = raw_audio.exists() and raw_audio.stat().st_size > 0
        except Exception as e:
            logger.error(f"EdgeTTS Error: {e}")
            return None

    if not success or not raw_audio.exists():
        return None

    if HAS_FFMPEG:
        try:
            audio_filter = "highpass=f=150,lowpass=f=7500,equalizer=f=2800:width_type=q:w=1.2:g=3,acompressor=threshold=-16dB:ratio=4"
            cmd = ["ffmpeg", "-y", "-i", str(raw_audio), "-af", audio_filter, "-c:a", "libopus", "-b:a", "64k", str(final_ogg)]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            raw_audio.unlink(missing_ok=True)
            if final_ogg.exists() and final_ogg.stat().st_size > 0:
                return final_ogg, True
        except Exception:
            pass

    return raw_audio, False


async def send_smart_response(
    chat_id: int,
    bus_id: str,
    reply_text: str,
    is_direct: bool = False,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    send_as_voice: bool = False
):
    if not reply_text or not reply_text.strip():
        reply_text = "Системы зафиксировали сигнал. Ожидаю инструкций."

    now = time.time()
    dedup_key = (chat_id, reply_text[:60])
    if dedup_key in recent_sent_messages and (now - recent_sent_messages[dedup_key] < 3.0):
        return
    recent_sent_messages[dedup_key] = now

    kwargs = {"chat_id": chat_id, "reply_markup": reply_markup}
    if not is_direct and bus_id:
        kwargs["business_connection_id"] = bus_id

    if send_as_voice:
        audio_res = await process_jarvis_voice(reply_text)
        if audio_res:
            audio_path, is_ogg = audio_res
            try:
                media_file = FSInputFile(str(audio_path))
                if is_ogg:
                    await bot.send_voice(**kwargs, voice=media_file)
                else:
                    await bot.send_audio(**kwargs, audio=media_file, title="Jarvis Response")
                return
            except Exception as e:
                logger.error(f"Сбой отправки аудио: {e}")
            finally:
                audio_path.unlink(missing_ok=True)

    try:
        await bot.send_message(**kwargs, text=reply_text, parse_mode="HTML")
    except TelegramBadRequest:
        await bot.send_message(**kwargs, text=reply_text)
    except Exception as e:
        logger.error(f"Не удалось доставить сообщение: {e}")


async def ask_groq(prompt: str, session_id: int, system_prompt: str, max_tokens: int = 400) -> str:
    global today_prompt_tokens, today_completion_tokens, total_requests_today, stats_date

    now_date = datetime.date.today().isoformat()
    if now_date != stats_date:
        stats_date = now_date
        today_prompt_tokens = today_completion_tokens = total_requests_today = 0
        asyncio.create_task(save_stats())

    if not GROQ_KEYS:
        return "Критическая ошибка: отсутствуют ключи GROQ, сэр."

    if session_id not in user_histories:
        user_histories[session_id] = [{"role": "system", "content": system_prompt}]
    else:
        user_histories[session_id][0] = {"role": "system", "content": system_prompt}

    history = user_histories[session_id]
    history.append({"role": "user", "content": prompt})

    # Ограничение глубины контекста (храним системный + последние 6 сообщений)
    if len(history) > 7:
        user_histories[session_id] = [history[0]] + history[-6:]
        history = user_histories[session_id]

    models = await groq_mgr.get_active_models()
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
                    temperature=0.6,
                    max_tokens=max_tokens
                )
                if completion.usage:
                    today_prompt_tokens += completion.usage.prompt_tokens
                    today_completion_tokens += completion.usage.completion_tokens
                    total_requests_today += 1
                    asyncio.create_task(save_stats())

                reply = clean_cot_output(completion.choices[0].message.content or "")
                if reply:
                    history.append({"role": "assistant", "content": reply})
                    asyncio.create_task(save_histories())
                    return reply
            except APIError as e:
                if e.status_code in [429, 401, 403]:
                    groq_mgr.mark_cooldown(key_idx, duration=120)
            except Exception:
                break

    if history and history[-1]["role"] == "user":
        history.pop()

    return "Интерфейсы обработки временно перегружены, сэр."


async def enhance_image_prompt(user_prompt: str, allow_nsfw: bool = False) -> str:
    is_nsfw = is_nsfw_request(user_prompt)

    if allow_nsfw and is_nsfw:
        pose, angle, body, expression, clothing = resolve_anatomy_and_poses(user_prompt)
        is_anime = any(w in user_prompt.lower() for w in ["аниме", "хентай", "тян", "2d"])
        style = "authentic 2d anime hentai illustration, sharp lineart, 4k" if is_anime else "raw dslr photograph, ultra realistic, soft studio light, 8k uhd"
        return f"masterpiece, best quality, {style}, {pose}, {angle}, {body}, {expression}, {clothing}, perfect anatomy"

    # Перевод и расширение стандартного промпта через Groq
    models = await groq_mgr.get_active_models()
    if models:
        sys_msg = "Translate Russian prompt to English for Midjourney/Flux. Output ONLY the translated prompt."
        for _ in range(2):
            client_data = groq_mgr._get_next_client()
            if not client_data:
                break
            client, _ = client_data
            try:
                comp = await client.chat.completions.create(
                    model=models[0],
                    messages=[{"role": "system", "content": sys_msg}, {"role": "user", "content": user_prompt}],
                    max_tokens=100,
                    temperature=0.2
                )
                cleaned = clean_cot_output(comp.choices[0].message.content or "")
                if cleaned:
                    return f"{cleaned}, masterpiece, high quality, highly detailed"
            except Exception:
                continue

    return f"{user_prompt}, masterpiece, 4k"


async def generate_flux_image(prompt: str, allow_nsfw: bool = False) -> Optional[bytes]:
    if not http_session:
        return None

    eng_prompt = await enhance_image_prompt(prompt, allow_nsfw=allow_nsfw)
    is_anime = any(w in prompt.lower() for w in ["аниме", "хентай", "тян", "2d"])
    model = "turbo" if is_anime else "flux"

    params = {
        "width": "1024",
        "height": "1024",
        "model": model,
        "seed": str(random.randint(100, 9999999)),
        "nologo": "true",
        "nofeed": "true",
        "safe": "false" if allow_nsfw else "true"
    }
    encoded_prompt = urllib.parse.quote(eng_prompt.strip())
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?{query}"

    try:
        async with http_session.get(url, timeout=45) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as e:
        logger.error(f"Image Gen Exception: {e}")
    return None


# --- УПРАВЛЕНИЕ КОМАНДАМИ ---
async def process_bot_command(message: types.Message, user_input: str, is_owner: bool, bus_id: str = "") -> bool:
    global force_offline_mode, always_answer_mode, last_owner_activity, nsfw_art_mode

    chat_id = message.chat.id
    lower = user_input.lower().strip()
    is_direct = not bool(bus_id)

    if lower in ["игра", "тапалка", "/game"]:
        await send_smart_response(chat_id, bus_id, f"Инициирую запуск систем:\n{GAME_URL}", is_direct=is_direct)
        return True

    # Команды переключения NSFW
    if lower in ["джарвис 18+ вкл", "!18+ вкл", "18+ вкл"]:
        if not is_owner:
            await send_smart_response(chat_id, bus_id, "Отказ в доступе. Требуется авторизация создателя.", is_direct=is_direct)
            return True
        nsfw_art_mode = True
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Протокол безопасности 18+ деактивирован. Генерация NSFW доступна, сэр.", is_direct=is_direct)
        return True

    if lower in ["джарвис 18+ выкл", "!18+ выкл", "18+ выкл"]:
        if not is_owner:
            return True
        nsfw_art_mode = False
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Фильтр безопасности 18+ включен, сэр.", is_direct=is_direct)
        return True

    # Генерация изображений
    if re.search(r"\b(нарисуй|сгенерируй|создай арт|арт)\b", lower) or lower.startswith("!арт"):
        prompt = re.sub(r"\b(джарвис|пожалуйста|нарисуй|сгенерируй|создай арт|арт|!арт)\b", "", user_input, flags=re.I).strip(" ,:;!?")
        if not prompt:
            await send_smart_response(chat_id, bus_id, "Укажите техническое задание для генерации, сэр.", is_direct=is_direct)
            return True

        if is_nsfw_request(prompt) and not nsfw_art_mode:
            await send_smart_response(chat_id, bus_id, "Протокол безопасности: взрослый контент заблокирован. Активируйте '18+ вкл', сэр.", is_direct=is_direct)
            return True

        await send_smart_response(chat_id, bus_id, f"Инициирую протокол визуализации: <i>«{prompt}»</i>...", is_direct=is_direct)
        img_bytes = await generate_flux_image(prompt, allow_nsfw=nsfw_art_mode)

        if img_bytes:
            photo = BufferedInputFile(img_bytes, filename="art.jpg")
            kwargs = {"chat_id": chat_id, "photo": photo, "caption": f"Готово, сэр.\nЗапрос: {prompt}"}
            if bus_id:
                kwargs["business_connection_id"] = bus_id
            await bot.send_photo(**kwargs)
        else:
            await send_smart_response(chat_id, bus_id, "Модуль синтеза изображения временно недоступен.", is_direct=is_direct)
        return True

    # Напоминания
    rem_match = re.match(r"^(?:напомни|!напомни)\s+(?:через\s+)?(\d+)\s*(сек|мин|час|дн|ч|м|с)[а-я]*\s+(.+)$", user_input, re.I)
    if rem_match:
        qty, unit, task_text = int(rem_match.group(1)), rem_match.group(2).lower(), rem_match.group(3).strip()
        multipliers = {"сек": 1, "с": 1, "мин": 60, "м": 60, "час": 3600, "ч": 3600, "дн": 86400}
        seconds = qty * multipliers.get(unit, 60)

        reminders_list.append({
            "chat_id": chat_id,
            "bus_id": bus_id,
            "text": task_text,
            "time": time.time() + seconds
        })
        await save_reminders()
        await send_smart_response(chat_id, bus_id, f"Зафиксировано: напомню <i>«{task_text}»</i> через {qty} {unit}, сэр.", is_direct=is_direct)
        return True

    if not is_owner:
        return False

    # Владельческие команды
    if lower in ["джарвис я тут", "!онлайн", "я тут"]:
        force_offline_mode = False
        last_owner_activity = time.time()
        await send_smart_response(chat_id, bus_id, "С возвращением, сэр. Перехожу в режим наблюдения.", is_direct=is_direct)
        return True

    if lower in ["джарвис я отошел", "!офлайн", "я отошел"]:
        force_offline_mode = True
        await send_smart_response(chat_id, bus_id, "Охранный протокол активирован. Отвечаю гостям, сэр.", is_direct=is_direct)
        return True

    if lower in ["сброс", "джарвис сброс"]:
        user_histories.pop(chat_id, None)
        await save_histories()
        await send_smart_response(chat_id, bus_id, "Оперативная память диалога очищена, сэр.", is_direct=is_direct)
        return True

    return False


# --- ОБРАБОТЧИКИ СООБЩЕНИЙ ---
@dp.callback_query(F.data == "jarvis_unmute_direct")
async def handle_unmute(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if callback.from_user.id != OWNER_ID and OWNER_ID != 0:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    muted_chats.pop(chat_id, None)
    await save_settings()
    await callback.answer("Изоляция снята, сэр.")
    try:
        await callback.message.edit_text("Ограничения для собеседника аннулированы.")
    except Exception:
        pass


@dp.message(F.business_connection_id.is_(None))
async def handle_direct_message(message: types.Message):
    global last_owner_activity
    if not message.from_user or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    last_owner_activity = time.time()
    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip():
        return

    if await process_bot_command(message, user_input, is_owner=True):
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    reply = await ask_groq(user_input, chat_id, JARVIS_PROMPT_DIRECT, max_tokens=500)
    should_voice = is_voice or voice_chat_modes.get(chat_id, False) or (random.random() < 0.2)
    await send_smart_response(chat_id, "", reply, is_direct=True, send_as_voice=should_voice)


@dp.business_message()
async def handle_business_message(message: types.Message):
    global last_owner_activity
    chat_id = message.chat.id
    bus_id = message.business_connection_id
    msg_id = message.message_id
    bot_id = bot.id if bot else 0

    if not message.from_user or message.from_user.is_bot or message.from_user.id == bot_id:
        return
    if chat_id == bot_id or (OWNER_ID != 0 and chat_id == OWNER_ID):
        return

    if msg_id in processed_message_ids:
        return
    processed_message_ids[msg_id] = True

    is_owner = (OWNER_ID != 0 and message.from_user.id == OWNER_ID) or (message.from_user.id != chat_id)

    if is_owner:
        last_owner_activity = time.time()
        user_input, _ = await extract_message_content(message)
        if await process_bot_command(message, user_input, is_owner=True, bus_id=bus_id):
            try:
                await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
            except Exception:
                pass
        return

    # Логика для сторонних гостей
    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip():
        return

    business_visits[str(chat_id)] = {
        "name": message.from_user.full_name or "Гость",
        "username": f"@{message.from_user.username}" if message.from_user.username else "none",
        "time": datetime.datetime.now().strftime("%H:%M"),
        "last_msg": user_input[:100]
    }
    asyncio.create_task(save_visits())

    # Проверка активности владельца (если хозяин онлайн, бот молчит)
    now = time.time()
    if not always_answer_mode and not force_offline_mode:
        if (now - last_owner_activity) < OWNER_IDLE_TIMEOUT:
            return

    # Проверка изоляции
    if chat_id in muted_chats:
        if now < muted_chats[chat_id]:
            try:
                await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
            except Exception:
                pass
            return
        else:
            muted_chats.pop(chat_id, None)
            asyncio.create_task(save_settings())

    if await check_chat_flood(chat_id, bus_id):
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=bus_id)
    reply = await ask_groq(user_input, chat_id, JARVIS_PROMPT_GUEST, max_tokens=220)
    should_voice = is_voice or voice_chat_modes.get(chat_id, False) or (random.random() < 0.2)
    await send_smart_response(chat_id, bus_id, reply, is_direct=False, send_as_voice=should_voice)


# --- ФОНОВЫЕ СЛУЖБЫ ---
async def cleaner_and_reminders_task():
    global reminders_list
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()

            # Очистка мутов
            expired_mutes = [cid for cid, exp in muted_chats.items() if exp != float('inf') and now >= exp]
            if expired_mutes:
                for cid in expired_mutes:
                    muted_chats.pop(cid, None)
                await save_settings()

            # Обработка напоминаний
            remaining, triggered = [], []
            for r in reminders_list:
                (triggered if now >= r.get("time", 0) else remaining).append(r)

            if triggered:
                reminders_list = remaining
                await save_reminders()
                for rem in triggered:
                    text = f"Сэр, сработал протокол хронометража!\nНапоминание: <b>{rem.get('text')}</b>"
                    await send_smart_response(rem["chat_id"], rem.get("bus_id", ""), text, is_direct=not bool(rem.get("bus_id")), send_as_voice=True)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Cleaner Error: {e}")


async def handle_ping(request):
    return web.Response(text="Jarvis Core status: Online and functional.")


async def setup_web_app():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


async def main():
    global http_session
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN не задан в переменных окружения!")
        return

    http_session = aiohttp.ClientSession()
    web_runner = await setup_web_app()
    bg_task = asyncio.create_task(cleaner_and_reminders_task())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Джарвис подключен к сети и готов к работе.")
        await dp.start_polling(bot)
    except TelegramConflictError:
        logger.critical("Обнаружена параллельная запущенная сессия бота!")
    finally:
        bg_task.cancel()
        await web_runner.cleanup()
        if http_session:
            await http_session.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Работа систем Джарвиса остановлена.")
