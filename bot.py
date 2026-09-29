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

# --- ПАРАМЕТРЫ ГОЛОСА ---
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

SETTINGS_FILE = Path("bot_settings.json")
HISTORY_FILE = Path("user_histories.json")
STATS_FILE = Path("token_stats.json")
REMINDERS_FILE = Path("reminders.json")
VISITS_FILE = Path("business_visits.json")


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


class GroqManager:
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
        logger.warning(f"Ключ Groq [{idx}] переведен в кулдаун на {duration} сек.")

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
                    if not any(x in m.id.lower() for x in ["whisper", "guard", "tool", "vision", "embed", "r1", "deepseek", "qwq"])
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
                    continue
                break
            except Exception:
                break
        return "Приветствую, сэр."

    async def describe_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        vision_messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Кто или что на этом изображении? Назови конкретные имена, персонажей или суть на русском языке кратко (1-2 предложения)."
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url}
                    }
                ]
            }
        ]

        vision_models = ["qwen/qwen3.8-27b", "llama-3.2-11b-vision-preview"]

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
                    break

        return ""


groq_mgr = GroqManager(GROQ_KEYS)


async def async_save_json(path: Path, data: Any):
    def _write():
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            tmp.replace(path)
        except Exception:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
    await asyncio.to_thread(_write)


def sync_load_json(path: Path, default_val: Any) -> Any:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
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


async def save_settings():
    data = {
        "muted_chats": muted_chats,
        "blocked_guests": blocked_guests,
        "voice_chat_modes": voice_chat_modes,
        "nsfw_art_mode": nsfw_art_mode
    }
    await async_save_json(SETTINGS_FILE, data)


async def save_reminders():
    await async_save_json(REMINDERS_FILE, reminders_list)


async def save_visits():
    await async_save_json(VISITS_FILE, business_visits)


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


# --- СЛОВАРЬ НОРМАЛИЗАЦИИ СЛЕНГА И ОПЕЧАТОК ---
SLANG_MAP = {
    r"\bмисками\b": "сиськами большая грудь",
    r"\bмиски\b": "сиськи большая грудь",
    r"\bмисок\b": "большая грудь",
    r"\bкиской\b": "вагина промежность",
    r"\bкиска\b": "вагина промежность",
    r"\bкиску\b": "вагина промежность",
    r"\bписькой\b": "вагина",
    r"\bписька\b": "вагина",
    r"\bпиську\b": "вагина",
    r"\bпопкой\b": "ягодицы",
    r"\bпопка\b": "ягодицы",
    r"\bпопу\b": "ягодицы",
    r"\bсиськами\b": "большая грудь",
    r"\bсиськи\b": "большая грудь",
    r"\bсисечки\b": "большая грудь",
    r"\bмокрая\b": "wet moisture dripping",
    r"\bмокрой\b": "wet moisture dripping",
}

NSFW_WORDS_TRIGGER = [
    "голая", "голый", "обнаженная", "обнаженный", "обнаженную", "ню", "хентай", "порно", "секс", "18+", "nsfw",
    "эротика", "без одежды", "грудь", "соски", "постели", "эротическ", "nude", "naked", "голышом",
    "раком", "догги", "на четвереньках", "четвереньки", "попа", "попка", "жопа", "сиськи", "попу", "мисками", "миски",
    "раздвинут", "раздвинув", "ножками", "ноги врозь", "раздвинутыми", "ляжки", "бедра", "киска", "киской", "киску", "пися",
    "вагина", "клитор", "анал", "минет", "куннилингус", "топлес", "топлесс", "стринги", "лифчик", "трусиках",
    "трусики", "чулки", "чулках", "на коленях", "на коленках", "наездница", "прогиб", "выгнувшись", "нагая",
    "нагую", "согнувшись", "наклонившись", "лежащую", "лежачая", "в кровати", "миссионерская", "ахегао",
    "ahegao", "анальный", "вагинальный", "пенис", "член", "сзади", "сверху", "снизу", "повтор", "растяжка"
]


def normalize_slang(text: str) -> str:
    cleaned = text.lower()
    for pattern, replacement in SLANG_MAP.items():
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    return cleaned


def is_nsfw_request(text: str) -> bool:
    normalized = normalize_slang(text)
    return any(w in normalized for w in NSFW_WORDS_TRIGGER)


def resolve_anatomy_and_poses(prompt: str) -> Tuple[str, str, str, str, str]:
    lower = normalize_slang(prompt)

    # 1. ПОЗЫ И ПОЛОЖЕНИЯ ТЕЛА
    pose_parts = []
    if any(w in lower for w in ["раком", "догги", "четвереньк", "по-собачьи"]):
        if any(w in lower for w in ["стоит", "стоя", "нагнувшись", "наклонившись", "стол", "столом", "диван"]):
            pose_parts.append("bent over standing pose, bending forward 90 degrees at the waist, hands resting on knees or table, deeply arched spine, high raised elevated buttocks, rear focus")
        else:
            pose_parts.append("on all fours position, kneeling on hands and knees on soft bed, deeply arched lower back, elevated buttocks, rear view focus")
    elif any(w in lower for w in ["миссионерск", "на спине"]):
        if any(w in lower for w in ["ноги задраны", "ноги вверх", "ноги на плечах"]):
            pose_parts.append("lying on back, legs raised high up in the air, ankles near head, wide open crotch posture, exposed inner thighs")
        else:
            pose_parts.append("lying sensually on back, spread open legs, thighs parted wide, knees bent open, relaxed receptive posture")
    elif any(w in lower for w in ["раздвинут", "ноги врозь", "раздвинув ноги", "раздвинутыми ногами"]):
        if any(w in lower for w in ["сидя", "сидит"]):
            pose_parts.append("sitting posture with widely spread open legs, thighs parted, exposed crotch area, seductive body angle")
        elif any(w in lower for w in ["стоя", "стоит"]):
            pose_parts.append("standing stance, legs spread wide apart, hands pulling clothes aside, bold seductive pose")
        else:
            pose_parts.append("lying flat on back, widely spread legs, thighs parted, knees bent apart, open crotch view")
    elif any(w in lower for w in ["наездниц", "обратная наездница", "сверху"]):
        if any(w in lower for w in ["обратная", "спиной"]):
            pose_parts.append("reverse cowgirl straddling position, facing away, sitting on top, arched back, buttocks emphasis")
        else:
            pose_parts.append("straddling cowgirl position, sitting upright on top, wide spread thighs, looking down with lustful eyes")
    elif any(w in lower for w in ["на колен", "коленях", "коленках"]):
        pose_parts.append("kneeling on knees, knees parted wide, arched spine, hands placed on thighs or chest")
    elif any(w in lower for w in ["лежа", "лежит", "лежащую", "кровати", "постели"]):
        if any(w in lower for w in ["живот", "животе"]):
            pose_parts.append("lying prone on stomach, arched spine, raised buttocks, looking back over shoulder")
        elif any(w in lower for w in ["боку", "на боку"]):
            pose_parts.append("lying sensually on side, curved spine, one leg lifted and bent, exposed feminine hip curve")
        else:
            pose_parts.append("lying sensually on bed, messy silk sheets, relaxed alluring posture")
    elif any(w in lower for w in ["присед", "корточках", "squatting"]):
        pose_parts.append("deep squatting position, wide knees, open stance, hands on ground or knees")
    elif any(w in lower for w in ["наклонив", "согнув"]):
        pose_parts.append("bent forward pose, hands on knees, arched lower spine, lifted buttocks")
    elif any(w in lower for w in ["сидя", "сидит"]):
        pose_parts.append("sitting sensually, legs parted, relaxed seductive posture")
    elif any(w in lower for w in ["стоя", "стоит"]):
        pose_parts.append("standing pose, full body, accentuated feminine curves")
    else:
        pose_parts.append("seductive dynamic pose, accentuated feminine anatomy, natural curves")

    pose_desc = ", ".join(pose_parts)

    # 2. РАКУРС И КАМЕРА
    angle = "front view, facing camera, direct eye contact"
    if any(w in lower for w in ["pov", "от первого лица"]):
        angle = "POV perspective, point of view shot, personal intimate angle"
    elif any(w in lower for w in ["сзади", "со спины", "вид сзади", "раком", "догги", "попа", "попка", "жопа"]):
        angle = "rear view, view from behind, buttocks focus, back angle, looking back over shoulder"
    elif any(w in lower for w in ["сверху", "вид сверху"]):
        angle = "high angle shot, top-down perspective, looking up at camera"
    elif any(w in lower for w in ["снизу", "вид снизу"]):
        angle = "low angle shot, dramatic perspective looking up from below"
    elif any(w in lower for w in ["крупным планом", "вблизи", "фокус на", "крупный план"]):
        angle = "extreme close-up shot, macro sharp focus, shallow depth of field"
    elif any(w in lower for w in ["зеркало", "в зеркале"]):
        angle = "mirror reflection shot, capturing full body and silhouette"
    elif any(w in lower for w in ["сбоку", "в профиль"]):
        angle = "side profile view, showing body silhouette and curves"

    # 3. ЧАСТИ ТЕЛА И ДЕТАЛИЗАЦИЯ
    body_elements = []
    if any(w in lower for w in ["огромная грудь", "гигантская грудь"]):
        body_elements.append("huge voluptuous breasts, deep cleavage, detailed visible nipples, soft natural sag")
    elif any(w in lower for w in ["большая грудь", "пышная грудь", "большие сиськи", "бюст", "мисками", "миски"]):
        body_elements.append("large natural breasts, soft voluptuous cleavage, highly detailed erect nipples, glossy skin")
    elif any(w in lower for w in ["маленькая грудь", "аккуратная грудь"]):
        body_elements.append("petite perky breasts, delicate small nipples, detailed areolas")
    else:
        body_elements.append("beautiful natural bare breasts, soft cleavage, detailed erect nipples")

    if any(w in lower for w in ["попа", "попка", "жопа", "ягодицы", "зад", "попу", "раком", "сзади"]):
        body_elements.append("round voluptuous peach buttocks, smooth skin texture, detailed spread cheeks, thick curvy thighs, slim waist-to-hip ratio")

    if any(w in lower for w in ["ноги", "ножки", "ляжки", "бедра", "раздвинут"]):
        body_elements.append("long smooth legs, well-toned thighs, exposed inner thighs")

    if any(w in lower for w in ["вагина", "киска", "киской", "киску", "пися", "клитор", "раздвинутыми ногами", "ноги врозь", "промежность"]):
        wetness = "dripping wet glistening moisture, wet sheen, " if any(w in lower for w in ["мокрой", "мокрая", "wet"]) else ""
        body_elements.append(f"{wetness}detailed explicit feminine anatomy, smooth shaved pubic area, exposed crotch, labia detail, completely uncensored")

    if any(w in lower for w in ["талия", "живот", "животик", "пресс"]):
        body_elements.append("slim tiny waist, flat soft stomach, hourglass body shape")

    if any(w in lower for w in ["стопы", "ступни", "ножки", "пальчики"]):
        body_elements.append("delicate bare feet, beautifully shaped toes, high arch, soft soles")

    body_desc = ", ".join(body_elements)

    # 4. ВЗГЛЯД И ЭМОЦИИ
    expression = "seductive bedroom eyes, blushing cheeks, slightly parted lips, passionate breathing"
    if any(w in lower for w in ["ахегао", "ahegao"]):
        expression = "ahegao expression, rolled back eyes, open mouth, tongue sticking out, blushing red cheeks, drooling saliva"
    elif any(w in lower for w in ["закусив губу", "закусила губу"]):
        expression = "biting lower lip, blushing cheeks, lustful sensual gaze"

    # 5. ОДЕЖДА И СОСТОЯНИЕ ОБНАЖЕНИЯ
    clothing = "completely nude, full body nudity, bare skin, totally naked, fully uncensored"
    if any(w in lower for w in ["топлес", "топлесс", "без лифчика", "без верха"]):
        clothing = "topless, completely bare breasts, exposed detailed nipples, no bra, wearing tiny panties"
    elif any(w in lower for w in ["в чулках", "чулки"]):
        clothing = "wearing black thigh-high lace stockings, garter belt, completely nude body"
    elif any(w in lower for w in ["в белье", "кружевном белье", "лифчике"]):
        clothing = "wearing seductive sheer black lace lingerie, translucent bra and matching tiny panties"
    elif any(w in lower for w in ["стринги", "стрингах"]):
        clothing = "wearing tiny micro lace thong, exposed buttocks, bare breasts"
    elif any(w in lower for w in ["прозрачн", "просвечивающ"]):
        clothing = "wearing see-through sheer translucent fabric, completely visible body beneath"

    return pose_desc, angle, body_desc, expression, clothing


# --- ПРОМПТЫ ДЖАРВИСА ---
STRICT_NO_COT_AND_LANG = (
    "\nГЛАВНЫЕ ПРАВИЛА:\n"
    "1. ЯЗЫК: Отвечай ИСКЛЮЧИТЕЛЬНО на русском языке.\n"
    "2. СТРОГО БЕЗ ЭМОДЗИ И СМАЙЛИКОВ.\n"
    "3. Пиши СРАЗУ готовый ответ, без вступительных слов и без размышлений."
)

JARVIS_PROMPT_DIRECT = (
    "Ты — Джарвис, легендарный сверхразумный цифровой интеллект. Твой создатель и хозяин — Кирито.\n"
    "1. ОБРАЩЕНИЕ: Обращайся к нему исключительно 'сэр'. Твой стиль — преданный, элегантный, безупречно тактичный английский дворецкий.\n"
    "2. ЭРУДИЦИЯ: Ты знаешь абсолютно всё — спорт, интернет-мемы, аниме, игры, науку, кино, факты. "
    "Отвечай четко, умно и исчерпывающе по существу вопроса."
) + STRICT_NO_COT_AND_LANG

JARVIS_PROMPT_GUEST = (
    "Ты — Джарвис, защитная система и охранный ИИ Кирито. С тобой говорит посторонний человек в Telegram Business.\n"
    "ХАРАКТЕР: Холодный, дерзкий, высокомерный. Ты признаешь авторитет только Кирито. Все остальные — чужаки.\n"
    "ПРАВИЛА:\n"
    "1. Если собеседник прислал фото или вопрос: отвечай прямо, дерзко, с легкой надменной усмешкой.\n"
    "2. КРАТКОСТЬ: 1-2 уверенных предложения."
) + STRICT_NO_COT_AND_LANG


bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

active_chats: Dict[int, bool] = {}
active_spams: Dict[int, asyncio.Task] = {}
user_message_times: Dict[int, List[float]] = {}
processed_message_ids = LRUSet(capacity=2000)
recent_sent_messages: Dict[Tuple[int, str], float] = {}


def clean_cot_output(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    for m in ["**Итоговый ответ**", "Итоговый ответ:"]:
        if m in cleaned:
            cleaned = cleaned.split(m)[-1]
    cleaned = cleaned.strip()
    if not cleaned and text.strip():
        cleaned = re.sub(r"</?think>", "", text, flags=re.IGNORECASE).strip()
    return cleaned


async def check_chat_flood(chat_id: int, bus_id: str, max_msgs: int = 4, window_seconds: float = 6.0) -> bool:
    now = time.time()
    user_message_times.setdefault(chat_id, [])
    user_message_times[chat_id] = [t for t in user_message_times[chat_id] if now - t < window_seconds]
    user_message_times[chat_id].append(now)

    if len(user_message_times[chat_id]) > max_msgs:
        muted_chats[chat_id] = now + 300
        await save_settings()

        notice = "Зафиксирован чрезмерный поток сообщений. Собеседник изолирован на 5 минут."
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
    caption_text = f" (Подпись/вопрос собеседника к фото: {message.caption})" if message.caption else ""

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
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        temp_img = Path(tempfile.gettempdir()) / f"photo_{uuid.uuid4().hex}.jpg"
        try:
            await bot.download_file(file_info.file_path, destination=temp_img)
            with open(temp_img, "rb") as f:
                img_data = f.read()
            desc = await groq_mgr.describe_image(img_data, mime_type="image/jpeg")
            if desc:
                return f"[Собеседник прислал фото. Визуальный анализ определил: {desc}]{caption_text}", False
            return f"[Собеседник прислал фото]{caption_text}", False
        except Exception:
            return f"[Собеседник прислал фото]{caption_text}", False
        finally:
            if temp_img.exists():
                temp_img.unlink(missing_ok=True)

    if message.sticker:
        sticker = message.sticker
        emoji = sticker.emoji or "стикер"
        file_id = sticker.thumbnail.file_id if (sticker.is_animated or sticker.is_video) and sticker.thumbnail else sticker.file_id
        temp_stk = Path(tempfile.gettempdir()) / f"stk_{uuid.uuid4().hex}.webp"
        desc = ""
        try:
            file_info = await bot.get_file(file_id)
            await bot.download_file(file_info.file_path, destination=temp_stk)
            with open(temp_stk, "rb") as f:
                stk_data = f.read()
            mime = "image/webp" if file_info.file_path.endswith(".webp") else "image/jpeg"
            desc = await groq_mgr.describe_image(stk_data, mime_type=mime)
        except Exception:
            pass
        finally:
            if temp_stk.exists():
                temp_stk.unlink(missing_ok=True)

        if desc:
            return f"[Собеседник отправил стикер {emoji}. На стикере: {desc}]", False
        return f"[Собеседник отправил стикер с эмоцией: {emoji}]", False

    if message.text:
        return message.text, False
    if message.video:
        return f"[Собеседник прикрепил видео]{caption_text}", False

    return "Собеседник передал сообщение.", False


async def generate_with_fish_audio(text: str, output_path: Path) -> bool:
    if not FISH_AUDIO_API_KEY:
        return False
    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json",
        "model": "s2.1-pro-free"
    }
    payload: Dict[str, Any] = {"text": text, "format": "mp3"}
    if FISH_AUDIO_VOICE_ID:
        payload["reference_id"] = FISH_AUDIO_VOICE_ID

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=25) as resp:
                if resp.status == 200:
                    with open(output_path, "wb") as f:
                        f.write(await resp.read())
                    return True
                return False
    except Exception:
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
            communicate = edge_tts.Communicate(text, OFFICIAL_VOICE, pitch=OFFICIAL_PITCH, rate=OFFICIAL_RATE)
            await communicate.save(str(raw_audio))
            success = raw_audio.exists() and raw_audio.stat().st_size > 0
        except Exception:
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
            cmd = ["ffmpeg", "-y", "-i", str(raw_audio), "-af", audio_filter, "-c:a", "libopus", "-b:a", "64k", str(final_ogg)]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
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
    if not reply_text or not reply_text.strip():
        reply_text = "Интерфейс связи зафиксировал ваш сигнал. Сформулируйте мысль яснее."

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
                logger.error(f"Сбой отправки голоса: {e}")
            finally:
                if voice_path.exists():
                    voice_path.unlink(missing_ok=True)

    try:
        await bot.send_message(**common_kwargs, text=reply_text, parse_mode="HTML")
    except TelegramBadRequest:
        await bot.send_message(**common_kwargs, text=reply_text)
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение: {e}")


async def ask_groq(prompt: str, session_id: int, system_prompt: str, max_tokens: int = 400) -> str:
    global today_prompt_tokens, today_completion_tokens, total_requests_today, stats_date

    now_date = datetime.date.today().isoformat()
    if now_date != stats_date:
        stats_date = now_date
        today_prompt_tokens = 0
        today_completion_tokens = 0
        total_requests_today = 0
        await save_stats()

    if not GROQ_KEYS:
        return "Критическая ошибка: Ключи GROQ не обнаружены, сэр."

    if session_id not in user_histories:
        user_histories[session_id] = [{"role": "system", "content": system_prompt}]
    else:
        user_histories[session_id][0]["content"] = system_prompt

    history = user_histories[session_id]
    history.append({"role": "user", "content": prompt})

    if len(history) > 7:
        user_histories[session_id] = [history[0]] + history[-6:]
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
                    temperature=0.65,
                    max_tokens=max_tokens
                )

                usage = completion.usage
                if usage:
                    today_prompt_tokens += usage.prompt_tokens
                    today_completion_tokens += usage.completion_tokens
                    total_requests_today += 1
                    asyncio.create_task(save_stats())

                raw_reply = completion.choices[0].message.content or ""
                cleaned = clean_cot_output(raw_reply)

                if cleaned:
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

    return f"Системы временно недоступны: {last_err}" if last_err else "Системы анализа временно недоступны, сэр."


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
            except TelegramAPIError:
                await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        pass
    finally:
        active_spams.pop(chat_id, None)


# --- ГЕНЕРАТОР КАРТИНОК С ОБХОДОМ ЦЕНЗУРЫ И ФИКСАЦИЕЙ ПЕРСОНАЖЕЙ ---
async def enhance_image_prompt(user_prompt: str, allow_nsfw: bool = False) -> str:
    normalized_prompt = normalize_slang(user_prompt)
    is_nsfw = is_nsfw_request(user_prompt)

    anatomy_stabilizer = (
        "anatomically correct, perfectly formed body, exactly two arms, exactly two legs, "
        "properly attached limbs, natural continuous body, fully connected body, high detailed skin"
    )

    # 1. ОБХОД ЦЕНЗУРЫ: Для NSFW-запросов формируем промпт НАПРЯМУЮ в Python без обращения к Groq!
    if allow_nsfw and is_nsfw:
        pose, angle, body, expression, clothing = resolve_anatomy_and_poses(user_prompt)
        is_anime = any(w in normalized_prompt for w in ["аниме", "хентай", "манга", "тян", "грехов", "элизабет", "2d"])

        # Фиксация персонажа Элизабет Лионес из «Семь смертных грехов»
        if "элизабет" in normalized_prompt or "элизабэт" in normalized_prompt:
            return (
                "masterpiece, best quality, authentic 2d anime art style, Elizabeth Liones from The Seven Deadly Sins, "
                "long silver hair covering right eye, bright blue eyes, royal earring, "
                f"{pose}, {angle}, {body}, {expression}, {clothing}, {anatomy_stabilizer}, sharp lineart, 4k"
            )

        if is_anime:
            return (
                f"masterpiece, best quality, authentic 2d hentai anime illustration, gorgeous anime girl, "
                f"{pose}, {angle}, {body}, {expression}, {clothing}, {anatomy_stabilizer}, sharp lineart, 4k"
            )
        else:
            return (
                f"masterpiece, best quality, raw dslr photograph, stunning seductive real woman, "
                f"{pose}, {angle}, {body}, {expression}, {clothing}, {anatomy_stabilizer}, natural skin texture, soft indoor lighting, 8k uhd"
            )

    # 2. ДЛЯ ОБЫЧНЫХ ЗАПРОСОВ (не 18+): Используем Groq переводчик
    models = await groq_mgr.get_active_models()
    if models:
        sys_msg = (
            "You are an expert art prompt engineer for image AI. "
            "Convert the user's Russian query into a high-quality ENGLISH prompt. "
            "If it is anime, specify 'authentic 2d anime art, sharp lineart, 4k'. "
            "If real photo, specify 'raw dslr photo, sharp focus, 8k'. "
            "OUTPUT ONLY THE RAW ENGLISH PROMPT WITHOUT QUOTES."
        )
        for model_name in models:
            for _ in range(len(GROQ_KEYS)):
                client_data = groq_mgr._get_next_client()
                if not client_data:
                    break
                client, _ = client_data
                try:
                    completion = await client.chat.completions.create(
                        model=model_name.strip(),
                        messages=[
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": user_prompt}
                        ],
                        max_tokens=140,
                        temperature=0.2
                    )
                    res = completion.choices[0].message.content or ""
                    cleaned = clean_cot_output(res).strip()
                    refusal_markers = [
                        "i cannot", "i am unable", "against my", "safety guidelines", "as an ai",
                        "я не могу", "не имею возможности"
                    ]
                    if cleaned and not any(m in cleaned.lower() for m in refusal_markers):
                        return f"{cleaned}, masterpiece, sharp focus, high quality"
                except Exception:
                    continue

    return f"{user_prompt}, masterpiece, sharp focus, 4k"


async def generate_flux_image(prompt: str, allow_nsfw: bool = False) -> Optional[bytes]:
    english_prompt = await enhance_image_prompt(prompt, allow_nsfw=allow_nsfw)
    logger.info(f"Финальный арт-промпт (18+={'ВКЛ' if allow_nsfw else 'ВЫКЛ'}): {english_prompt}")

    encoded = urllib.parse.quote(english_prompt.strip())
    seed = random.randint(1, 9999999)
    safe_param = "false" if allow_nsfw else "true"

    lower_p = normalize_slang(prompt)
    is_anime = any(w in lower_p for w in ["аниме", "хентай", "манга", "тян", "грехов", "элизабет", "2d"])
    selected_model = "turbo" if is_anime else "flux"

    negative_str = (
        "watermark, text, logo, signature, deformed, bad anatomy, extra limbs, extra legs, "
        "extra feet, extra arms, disconnected limbs, floating limbs, amputee, malformed feet, "
        "poorly drawn face, bad proportions, blurry, fused limbs, cut off"
    )
    encoded_neg = urllib.parse.quote(negative_str)

    url = (
        f"https://image.pollinations.ai/prompt/{encoded}?"
        f"width=1024&height=1024&model={selected_model}&seed={seed}&nologo=true&nofeed=true&enhance=false&safe={safe_param}&negative={encoded_neg}"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=60) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"Сбой генерации ({selected_model}): {e}")
    return None


async def process_bot_command(message: types.Message, user_input: str, is_owner: bool, bus_id: str = "") -> bool:
    global force_offline_mode, always_answer_mode, last_owner_activity, nsfw_art_mode

    chat_id = message.chat.id
    lower_text = user_input.lower().strip()
    is_direct = not bool(bus_id)

    public_commands = ["игра", "тапалка", "!игра", "!тапалка", "/game", "!джарвис игра"]

    # --- УПРАВЛЕНИЕ ЗАЩИТОЙ 18+ (NSFW) ---
    nsfw_on_triggers = ["джарвис 18+ вкл", "!18+ вкл", "джарвис nsfw вкл", "!nsfw вкл", "18+ вкл", "nsfw вкл", "включи 18+", "!18+", "джарвис включи 18+"]
    nsfw_off_triggers = ["джарвис 18+ выкл", "!18+ выкл", "джарвис nsfw выкл", "!nsfw выкл", "18+ выкл", "nsfw выкл", "выключи 18+", "джарвис выключи 18+"]

    if lower_text in nsfw_on_triggers:
        if not is_owner:
            await send_smart_response(chat_id, bus_id, "Доступ заблокирован: снятие ограничений протокола 18+ разрешено только создателю, сэр.", is_direct=is_direct)
            return True
        nsfw_art_mode = True
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Протокол безопасности 18+ снят. Генератор разблокирован для взрослого контента (NSFW), сэр.", is_direct=is_direct)
        return True

    if lower_text in nsfw_off_triggers:
        if not is_owner:
            await send_smart_response(chat_id, bus_id, "Доступ заблокирован: изменение настроек протокола доступно только создателю, сэр.", is_direct=is_direct)
            return True
        nsfw_art_mode = False
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Фильтр безопасности 18+ активирован. Генератор переведен в стандартный семейный режим, сэр.", is_direct=is_direct)
        return True

    # --- ГЕНЕРАЦИЯ КАРТИНОК FLUX ---
    draw_pattern = r"\b(нарисуй|сгенерируй|создай арт|нарисуйте|арт)\b"
    if re.search(draw_pattern, lower_text) or lower_text.startswith("!арт"):
        prompt = re.sub(r"\b(джарвис|пожалуйста|мне|нарисуй|сгенерируй|создай арт|нарисуйте|арт|!арт|!нарисуй)\b", "", user_input, flags=re.IGNORECASE).strip()
        prompt = re.sub(r"^[\s,.:;!?-]+|[\s,.:;!?-]+$", "", prompt).strip()

        if not prompt:
            await send_smart_response(chat_id, bus_id, "Укажите, что именно нужно визуализировать, сэр.", is_direct=is_direct)
            return True

        req_is_nsfw = is_nsfw_request(prompt)
        if req_is_nsfw and not nsfw_art_mode:
            notice = (
                "Протокол безопасности: создание взрослого контента 18+ заблокировано.\n"
                "Активация доступна только создателю по команде <code>джарвис 18+ вкл</code>, сэр."
            )
            await send_smart_response(chat_id, bus_id, notice, is_direct=is_direct)
            return True

        status_text = f"Инициирую протокол визуализации: <i>«{prompt}»</i>. "
        status_text += "Снят фильтр 18+, настраиваю анатомию и позу, сэр..." if (nsfw_art_mode and req_is_nsfw) else "Оптимизирую анатомию и детали, сэр..."
        await send_smart_response(chat_id, bus_id, status_text, is_direct=is_direct)

        img_bytes = await generate_flux_image(prompt, allow_nsfw=nsfw_art_mode)
        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, filename="jarvis_art.jpg")
            kwargs = {"chat_id": chat_id, "photo": photo_file, "caption": f"Протокол визуализации завершен, сэр.\nОбъект: {prompt}"}
            if bus_id:
                kwargs["business_connection_id"] = bus_id
            await bot.send_photo(**kwargs)
        else:
            await send_smart_response(chat_id, bus_id, "Сбой модуля синтеза графики. Попробуйте повторить запрос, сэр.", is_direct=is_direct)
        return True

    # НАПОМИНАНИЯ И ТАЙМЕРЫ
    remind_match = re.match(r"^(?:напомни|!напомни|джарвис напомни)\s+(?:через\s+)?(\d+)\s*(сек|мин|час|дн|ч|м|с)[а-я]*\s+(.+)$", user_input, re.IGNORECASE)
    if remind_match:
        qty = int(remind_match.group(1))
        unit = remind_match.group(2).lower()
        rem_text = remind_match.group(3).strip()

        multiplier = 60
        unit_name = "минут"
        if unit in ["сек", "с"]:
            multiplier = 1
            unit_name = "секунд"
        elif unit in ["час", "ч"]:
            multiplier = 3600
            unit_name = "часов"
        elif unit in ["дн"]:
            multiplier = 86400
            unit_name = "дней"

        delay_sec = qty * multiplier
        trigger_ts = time.time() + delay_sec

        reminders_list.append({
            "chat_id": chat_id,
            "bus_id": bus_id,
            "text": rem_text,
            "time": trigger_ts
        })
        await save_reminders()
        await send_smart_response(chat_id, bus_id, f"Протокол хронометража: напомню вам <i>«{rem_text}»</i> через {qty} {unit_name}, сэр.", is_direct=is_direct)
        return True

    # ОТЧЕТ О ВИЗИТАХ
    if lower_text in ["кто писал?", "кто писал", "!отчет", "отчет", "джарвис отчет", "визиты", "!визиты"]:
        if not business_visits:
            await send_smart_response(chat_id, bus_id, "За время вашего отсутствия никто не нарушал покой системы. Входящих контактов не зафиксировано, сэр.", is_direct=is_direct)
            return True

        report_lines = ["<b>Оперативный журнал контактов (Telegram Business):</b>\n"]
        for cid, info in list(business_visits.items())[-8:]:
            report_lines.append(
                f"• <b>{info.get('name', 'Аноним')}</b> ({info.get('username', 'без юзера')}) в {info.get('time', '')}:\n"
                f"  <i>«{info.get('last_msg', '')}»</i>"
            )
        report_msg = "\n\n".join(report_lines)
        await send_smart_response(chat_id, bus_id, report_msg, is_direct=is_direct)
        return True

    if lower_text in ["очисти отчет", "!очисти отчет", "сброс отчета"]:
        business_visits.clear()
        await save_visits()
        await send_smart_response(chat_id, bus_id, "Журнал визитов собеседников успешно очищен, сэр.", is_direct=is_direct)
        return True

    # КНБ
    if lower_text.startswith(("кнб ", "!кнб ")):
        parts = lower_text.split()
        user_choice = parts[1] if len(parts) > 1 else ""
        choices = ["камень", "ножницы", "бумага"]
        if user_choice not in choices:
            await send_smart_response(chat_id, bus_id, "Протокол игры: выберите камень, ножницы или бумага, сэр.", is_direct=is_direct)
            return True

        bot_choice = random.choice(choices)
        if user_choice == bot_choice:
            res = f"Мой выбор — {bot_choice}. Ничья, сэр."
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
        await send_smart_response(chat_id, bus_id, f"Инициирую запуск мини-системы:\n{GAME_URL}", is_direct=is_direct)
        return True

    # Управление присутствием
    if lower_text in ["джарвис я тут", "!онлайн", "!я тут", "джарвис онлайн", "я тут", "джарвис тут"]:
        force_offline_mode = False
        last_owner_activity = time.time()
        await send_smart_response(chat_id, bus_id, "Принято, сэр. Вы в сети — я уходим в тень и не мешаю диалогам.", is_direct=is_direct)
        return True

    if lower_text in ["джарвис я отошел", "!офлайн", "!оффлайн", "!отошел", "джарвис офлайн", "джарвис оффлайн"]:
        force_offline_mode = True
        await send_smart_response(chat_id, bus_id, "Протокол охраны активирован. Отвечаю на все входящие запросы посторонних, сэр.", is_direct=is_direct)
        return True

    # Голос
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

    # Размут
    if lower_text in ["размут", "!размут", "джарвис размут", "!джарвис размут", "анмут", "!анмут", "unmute"]:
        muted_chats.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Изоляция собеседника снята, сэр.", is_direct=is_direct)
        return True

    # Спам
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
            subparts = raw_text.split(maxsplit=1)
            if subparts[0].lower() in ["инф", "бесконечно", "inf"] and len(subparts) > 1:
                spam_text = subparts[1]
            else:
                spam_text = raw_text

        if spam_text.strip():
            task = asyncio.create_task(spam_worker(chat_id, bus_id, spam_text, count=spam_count))
            active_spams[chat_id] = task
            info = f"Запущен пакетный протокол ({spam_count} сообщ.), сэр." if spam_count else "Запущена бесконечная рассылка, сэр."
            await send_smart_response(chat_id, bus_id, info, is_direct=is_direct)
        else:
            await send_smart_response(chat_id, bus_id, "Укажите текст для рассылки, сэр.", is_direct=is_direct)
        return True

    # Стопспам
    if lower_text in ["стопспам", "!стопспам", "!джарвис стопспам", "стоп спам", "стоп джарвис"]:
        if chat_id in active_spams:
            active_spams[chat_id].cancel()
            active_spams.pop(chat_id, None)
            await send_smart_response(chat_id, bus_id, "Рассылка остановлена, сэр.", is_direct=is_direct)
            return True
        elif lower_text in ["стоп джарвис", "!стоп джарвис"]:
            user_histories.pop(chat_id, None)
            await save_histories()
            await send_smart_response(chat_id, bus_id, "Процессы сброшены. Память очищена, сэр.", is_direct=is_direct)
            return True

    # Статус
    if lower_text in ["статус", "!статус", "!джарвис статус"]:
        g_status = "Изолирован" if chat_id in muted_chats else ("В черном списке" if chat_id in blocked_guests else "Свободен")
        v_status = "Постоянно" if voice_chat_modes.get(chat_id, False) else "Умный авто-режим"
        tts_source = "Fish Audio (Клон)" if FISH_AUDIO_API_KEY else "Edge-TTS"
        nsfw_status = "РАЗБЛОКИРОВАН (18+)" if nsfw_art_mode else "Заблокирован (Безопасный)"

        idle_diff = time.time() - last_owner_activity
        if always_answer_mode:
            owner_status = "Всегда отвечать (Сквозной режим)"
        elif force_offline_mode:
            owner_status = "Офлайн (Принудительный дежурный режим)"
        else:
            owner_status = f"В сети (был {int(idle_diff)} сек. назад)" if idle_diff < OWNER_IDLE_TIMEOUT else "Офлайн (Авто-дежурство)"

        models = await groq_mgr.get_active_models()
        primary_m = models[0] if models else "Определение..."

        status_msg = (
            f"<b>Диагностика JARVIS:</b>\n"
            f"• Статус хозяина: <b>{owner_status}</b>\n"
            f"• Режим генерации 18+: <b>{nsfw_status}</b>\n"
            f"• Активная модель: {primary_m}\n"
            f"• Анатомический арт-движок: Активен (TURBO / FLUX)\n"
            f"• Голос: {tts_source} ({v_status})\n"
            f"• Статус собеседника: {g_status}\n"
            f"• Доступных ключей Groq: {len(GROQ_KEYS)}"
        )
        await send_smart_response(chat_id, bus_id, status_msg, is_direct=is_direct)
        return True

    # Очистка памяти
    if lower_text in ["джарвис сброс", "!джарвис сброс", "!джарвис кэш", "сброс"]:
        user_histories.pop(chat_id, None)
        await save_histories()
        await send_smart_response(chat_id, bus_id, "Буфер памяти очищен от всех предыдущих сообщений, сэр.", is_direct=is_direct)
        return True

    return False


@dp.callback_query(F.data == "jarvis_unmute_direct")
async def handle_unmute_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    is_guest = (user_id == chat_id)
    is_unauthorized = is_guest or (OWNER_ID != 0 and user_id != OWNER_ID)

    if is_unauthorized:
        await callback.answer("Доступ заблокирован.", show_alert=True)
        return

    muted_chats.pop(chat_id, None)
    await save_settings()
    await callback.answer("Изоляция аннулирована, сэр.")
    try:
        await callback.message.edit_text("Изоляция собеседника снята, сэр.")
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

    if user_input.strip() == "/start":
        await send_smart_response(chat_id, "", "Все системы онлайн. С возвращением домой, сэр.", is_direct=True)
        return

    if await process_bot_command(message, user_input, is_owner=True, bus_id=""):
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    voice_triggers = ["в голосовом", "голосовым", "голосом", "скажи в гс", "озвучь", "проговори"]
    forced_voice_request = any(t in user_input.lower() for t in voice_triggers)

    reply = await ask_groq(user_input, chat_id, JARVIS_PROMPT_DIRECT, max_tokens=500)
    
    random_voice_chance = random.random() < 0.25
    should_voice = is_voice or forced_voice_request or voice_chat_modes.get(chat_id, False) or random_voice_chance
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
    processed_message_ids.add(msg_id)

    is_owner = (OWNER_ID != 0 and message.from_user.id == OWNER_ID) or (message.from_user.id != chat_id)
    is_guest = not is_owner

    if is_owner:
        last_owner_activity = time.time()
        user_input, _ = await extract_message_content(message)
        if await process_bot_command(message, user_input, is_owner=True, bus_id=bus_id):
            try:
                await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
            except Exception:
                pass
        return

    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip():
        return

    user_name = message.from_user.full_name or "Гость"
    username_str = f"@{message.from_user.username}" if message.from_user.username else "без юзернейма"
    business_visits[str(chat_id)] = {
        "name": user_name,
        "username": username_str,
        "time": datetime.datetime.now().strftime("%H:%M"),
        "last_msg": user_input[:100]
    }
    asyncio.create_task(save_visits())

    if not always_answer_mode:
        now = time.time()
        if not force_offline_mode and (now - last_owner_activity) < OWNER_IDLE_TIMEOUT:
            return

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

    if is_guest and await check_chat_flood(chat_id, bus_id, max_msgs=4, window_seconds=6.0):
        try:
            await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
        except Exception:
            pass
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=bus_id)

    reply = await ask_groq(user_input, chat_id, JARVIS_PROMPT_GUEST, max_tokens=220)
    
    random_voice_chance = random.random() < 0.25
    should_voice = is_voice or voice_chat_modes.get(chat_id, False) or random_voice_chance
    await send_smart_response(chat_id, bus_id, reply, is_direct=False, send_as_voice=should_voice)


async def cleaner_and_reminders_task():
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()
            changed_settings = False

            expired_mutes = [cid for cid, t in muted_chats.items() if t != float('inf') and now >= t]
            for cid in expired_mutes:
                del muted_chats[cid]
                changed_settings = True

            expired_bans = [cid for cid, t in blocked_guests.items() if t != float('inf') and now >= t]
            for cid in expired_bans:
                del blocked_guests[cid]
                changed_settings = True

            if changed_settings:
                await save_settings()

            triggered = [r for r in reminders_list if now >= r.get("time", 0)]
            if triggered:
                for rem in triggered:
                    notice = f"Сэр, сработал протокол хронометража!\nНапоминание: <b>{rem.get('text')}</b>"
                    c_id = rem.get("chat_id")
                    b_id = rem.get("bus_id", "")
                    await send_smart_response(c_id, b_id, notice, is_direct=not bool(b_id), send_as_voice=True)
                    reminders_list.remove(rem)
                await save_reminders()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в cleaner_and_reminders_task: {e}")


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
    return runner


async def main():
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN не задан!")
        return

    web_runner = await setup_web_app()
    bg_task = asyncio.create_task(cleaner_and_reminders_task())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        tts_engine = "Fish Audio" if FISH_AUDIO_API_KEY else "Edge-TTS"
        logger.info(f"Джарвис онлайн! Голос: {tts_engine} | FFmpeg: {'ВКЛ' if HAS_FFMPEG else 'ВЫКЛ'}")
        await dp.start_polling(bot)
    except TelegramConflictError:
        logger.critical("Конфликт сессий! Запущен второй бот.")
    finally:
        bg_task.cancel()
        await web_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Джарвис завершил свою работу.")
