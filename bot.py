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
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from groq import APIError, AsyncGroq

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s"
)
logger = logging.getLogger("JarvisCore")

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GAME_URL = "https://kirito-fd.github.io/k1rit0/"
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OWNER_IDLE_TIMEOUT = 300

force_offline_mode = False
always_answer_mode = False
last_owner_activity = 0.0

# Бесплатный публичный ключ Horde (бесконечный)
HORDE_API_KEY = os.getenv("HORDE_API_KEY", "0000000000").strip()

# Голос Джарвиса
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

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = DATA_DIR / "bot_settings.json"
HISTORY_FILE = DATA_DIR / "user_histories.json"
STATS_FILE = DATA_DIR / "token_stats.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
VISITS_FILE = DATA_DIR / "business_visits.json"

http_session: Optional[aiohttp.ClientSession] = None
file_io_lock = asyncio.Lock()


class LRUCacheDict(OrderedDict):
    def __init__(self, maxsize: int = 500, *args, **kwargs):
        self.maxsize = maxsize
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize:
            self.popitem(last=False)


art_prompts_cache = LRUCacheDict(maxsize=300)


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
            except Exception:
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
        return "Не удалось разобрать голосовой сигнал, сэр."

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
        models = ["llama-3.2-11b-vision-preview"]
        for vm in models:
            for _ in range(len(self.clients)):
                client_data = self._get_next_client()
                if not client_data:
                    break
                client, idx = client_data
                try:
                    comp = await client.chat.completions.create(
                        model=vm, messages=vision_messages, max_tokens=120, temperature=0.2
                    )
                    res = comp.choices[0].message.content or ""
                    if res.strip():
                        return clean_cot_output(res)
                except Exception:
                    continue
        return ""


groq_mgr = GroqManager(GROQ_KEYS)


# --- РАБОТА С ФАЙЛАМИ ---
async def async_save_json(path: Path, data: Any):
    async with file_io_lock:
        def _write():
            tmp = path.with_suffix(f".tmp_{uuid.uuid4().hex[:6]}")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                tmp.replace(path)
            except Exception as e:
                logger.error(f"Save error {path}: {e}")
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


# --- ГОЛОСОВОЙ МОДУЛЬ ДЖАРВИСА ---
async def generate_with_fish_audio(text: str, output_path: Path) -> bool:
    if not FISH_AUDIO_API_KEY or not http_session:
        return False
    url = "https://api.fish.audio/v1/tts"
    headers = {"Authorization": f"Bearer {FISH_AUDIO_API_KEY}", "Content-Type": "application/json"}
    payload = {"text": text, "format": "mp3"}
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
    clean_text = re.sub(r"<[^>]+>", "", text).strip()
    clean_text = re.sub(r"[*_`#~]", "", clean_text).strip()
    if not clean_text:
        return None

    uid = uuid.uuid4().hex
    raw_audio = Path(tempfile.gettempdir()) / f"raw_{uid}.mp3"
    final_ogg = Path(tempfile.gettempdir()) / f"jarvis_{uid}.ogg"

    success = False
    if FISH_AUDIO_API_KEY:
        success = await generate_with_fish_audio(clean_text, raw_audio)

    if not success:
        try:
            communicate = edge_tts.Communicate(clean_text, OFFICIAL_VOICE, pitch=OFFICIAL_PITCH, rate=OFFICIAL_RATE)
            await communicate.save(str(raw_audio))
            success = raw_audio.exists() and raw_audio.stat().st_size > 0
        except Exception:
            return None

    if not success or not raw_audio.exists():
        return None

    if HAS_FFMPEG:
        try:
            af = (
                "highpass=f=160,lowpass=f=7600,"
                "equalizer=f=2800:width_type=q:w=1.2:g=3.2,"
                "acompressor=threshold=-16dB:ratio=4,"
                "aecho=0.8:0.4:16:0.2"
            )
            cmd = ["ffmpeg", "-y", "-i", str(raw_audio), "-af", af, "-c:a", "libopus", "-b:a", "64k", str(final_ogg)]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            raw_audio.unlink(missing_ok=True)
            if final_ogg.exists() and final_ogg.stat().st_size > 0:
                return final_ogg, True
        except Exception:
            pass

    return raw_audio, False


# --- БЕСКОНЕЧНЫЙ 18+ ДВИЖОК (AI HORDE + FALLBACK) ---
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


def resolve_anatomical_scene(prompt: str) -> Tuple[str, str, str, str, str, int, int]:
    lower = prompt.lower()
    is_looking_wall = any(w in lower for w in ["стену", "в стену", "отвернулась", "отвернувшись", "вперед"])
    is_looking_camera = any(w in lower for w in ["на меня", "в камеру", "на зрителя", "в глаза", "лицом"])
    is_rear_pose = any(w in lower for w in ["раком", "догги", "четвереньк", "сзади", "со спины", "попа", "жопа", "ягодицы"])

    # В SD/Horde идеальные пропорции без артефактов — 512x768 или 768x512
    if is_rear_pose or any(w in lower for w in ["лежа", "лежит", "на кровати"]):
        width, height = 768, 512  # Альбомный для позы раком
    elif any(w in lower for w in ["стоя", "в полный рост", "на коленях"]):
        width, height = 512, 768  # Портретный
    else:
        width, height = 512, 512

    if is_rear_pose:
        pose = "all-fours kneeling pose, bending forward, hands on bed, arched back, elevated buttocks, rear focus, natural thighs"
        angle = "rear view angle, shot directly from behind, buttocks and arched back focus"
        body = "smooth natural skin texture, slender waist, arched spine, voluptuous rounded buttocks, toned thighs"
        if is_looking_wall:
            head_and_eyes = "back of head visible, hair falling back, head turned towards the wall, completely facing away"
        elif is_looking_camera:
            head_and_eyes = "head turned back over shoulder looking at viewer, seductive eyes"
        else:
            head_and_eyes = "profile view of face, neutral calm look"
    elif any(w in lower for w in ["на спине", "миссионерск"]):
        pose = "lying sensually on back, thighs parted, knees bent open"
        angle = "top down shot, overhead angle"
        body = "bare chest, natural breasts, slender waist, smooth inner thighs"
        head_and_eyes = "looking up at camera, seductive eyes, parted lips"
    else:
        pose = "seductive natural pose, relaxed posture"
        angle = "front view angle"
        body = "bare skin, natural curves, hourglass figure"
        head_and_eyes = "direct eye contact, gentle expression"

    clothing = "completely unclothed, bare skin, totally naked, fully uncensored"
    if any(w in lower for w in ["чулки", "в чулках"]):
        clothing = "wearing black lace thigh-high stockings, completely bare body otherwise"

    return pose, angle, body, head_and_eyes, clothing, width, height


async def generate_infinite_nsfw(prompt: str, is_anime: bool = False) -> Optional[bytes]:
    """Генерация через AI Horde с авто-переключением на Pollinations."""
    if not http_session:
        return None

    pose, angle, body, head, clothing, width, height = resolve_anatomical_scene(prompt)
    solo_anchor = "solo, 1girl, single female, only one person"

    if is_anime:
        selected_models = ["AbyssOrangeMix3", "AnyLoRA", "Hentai Diffusion", "AOM3A1B"]
        style_prompt = "masterpiece, best quality, authentic 2d anime hentai, clean lineart, uncensored"
    else:
        selected_models = ["CyberRealistic", "Realistic Vision", "ICBINP - I Can't Believe It's Not Photo"]
        style_prompt = "masterpiece, raw photo, realistic skin texture, authentic lighting, uncensored, 8k"

    full_prompt = f"{style_prompt}, {solo_anchor}, {pose}, {angle}, {body}, {head}, {clothing}"
    negative_prompt = (
        "two girls, multiple people, fused bodies, extra limbs, extra heads, deformed, "
        "bad hands, missing fingers, blurry, watermark, signature, censorship, mosaic"
    )

    headers = {
        "apikey": HORDE_API_KEY,
        "Client-Agent": "JarvisAiBot:v2.0"
    }
    payload = {
        "prompt": f"{full_prompt} ### {negative_prompt}",
        "params": {
            "sampler_name": "k_euler_a",
            "cfg_scale": 7.0,
            "steps": 26,
            "width": width,
            "height": height,
            "karras": True,
            "nsfw": True,
            "censor_nsfw": False
        },
        "nsfw": True,
        "censor_nsfw": False,
        "models": selected_models
    }

    # 1. Попытка через AI Horde (100% без цензуры)
    try:
        async with http_session.post("https://aihorde.net/api/v2/generate/async", json=payload, headers=headers, timeout=15) as resp:
            if resp.status == 202:
                data = await resp.json()
                task_id = data.get("id")
                if task_id:
                    check_url = f"https://aihorde.net/api/v2/generate/check/{task_id}"
                    for _ in range(25):  # Опрос до 50 секунд
                        await asyncio.sleep(2)
                        async with http_session.get(check_url, headers=headers) as chk:
                            if chk.status == 200:
                                chk_data = await chk.json()
                                if chk_data.get("done"):
                                    status_url = f"https://aihorde.net/api/v2/generate/status/{task_id}"
                                    async with http_session.get(status_url, headers=headers) as st:
                                        res_data = await st.json()
                                        gens = res_data.get("generations", [])
                                        if gens and gens[0].get("img"):
                                            async with http_session.get(gens[0]["img"]) as img_resp:
                                                if img_resp.status == 200:
                                                    return await img_resp.read()
                                    break
                                elif chk_data.get("faulted"):
                                    break
    except Exception as e:
        logger.warning(f"AI Horde не ответил вовремя ({e}), переключаюсь на резерв...")

    # 2. Быстрый резервный канал (Pollinations)
    encoded = urllib.parse.quote(full_prompt.strip())
    seed = random.randint(100, 9999999)
    res_w, res_h = (1024, 768) if width > height else (768, 1024)
    fallback_url = (
        f"https://image.pollinations.ai/prompt/{encoded}?"
        f"width={res_w}&height={res_h}&model=flux&seed={seed}&nologo=true&nofeed=true&safe=false"
    )
    fallback_headers = {"Referer": "https://pollinations.ai/", "User-Agent": "Mozilla/5.0"}
    try:
        async with http_session.get(fallback_url, headers=fallback_headers, timeout=40) as fb_resp:
            if fb_resp.status == 200:
                data = await fb_resp.read()
                if len(data) > 5000:
                    return data
    except Exception as e:
        logger.error(f"Fallback Error: {e}")

    return None


def get_art_keyboard(gen_id: str, current_style: str = "real") -> InlineKeyboardMarkup:
    toggle_style = "anime" if current_style == "real" else "real"
    toggle_label = "🎨 В Аниме" if current_style == "real" else "🎨 В Реализм"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Еще вариант", callback_data=f"art_retry:{gen_id}:{current_style}"),
                InlineKeyboardButton(text=toggle_label, callback_data=f"art_retry:{gen_id}:{toggle_style}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить арт", callback_data=f"art_delete:{gen_id}")
            ]
        ]
    )


# --- СИСТЕМНЫЕ ПРОМПТЫ ДЖАРВИСА ---
STRICT_RULES = (
    "\nПРАВИЛА:\n"
    "1. ЯЗЫК: Исключительно русский.\n"
    "2. СТРОГО БЕЗ ЭМОДЗИ И СМАЙЛИКОВ.\n"
    "3. Пиши сразу суть без размышлений."
)

JARVIS_PROMPT_DIRECT = (
    "Ты — Джарвис, легендарный сверхразумный цифровой дворецкий. Твой создатель и хозяин — Кирито.\n"
    "Обращайся к нему исключительно 'сэр'. Твой стиль — преданный, элегантный, безупречно тактичный дворецкий."
) + STRICT_RULES

JARVIS_PROMPT_GUEST = (
    "Ты — Джарвис, защитная ИИ-система Кирито в Telegram Business.\n"
    "Собеседник — посторонний гость. Твой тон: холодный, ироничный, дерзкий.\n"
    "Отвечай кратко (1-2 предложения), четко обозначая границы."
) + STRICT_RULES

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()

active_spams: Dict[int, asyncio.Task] = {}
user_message_times: Dict[int, List[float]] = {}
processed_message_ids = LRUCacheDict(maxsize=3000)
recent_sent_messages: Dict[Tuple[int, str], float] = {}


def clean_cot_output(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(Итоговый ответ:|\*\*Итоговый ответ\*\*)", "", cleaned.strip(), flags=re.I)
    return cleaned.strip()


async def check_chat_flood(chat_id: int, bus_id: str, max_msgs: int = 5, window_seconds: float = 6.0) -> bool:
    now = time.time()
    user_message_times.setdefault(chat_id, [])
    user_message_times[chat_id] = [t for t in user_message_times[chat_id] if now - t < window_seconds]
    user_message_times[chat_id].append(now)

    if len(user_message_times[chat_id]) > max_msgs:
        muted_chats[chat_id] = now + 300
        await save_settings()
        notice = "Превышен лимит сообщений. Собеседник изолирован на 5 минут."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Снять изоляцию", callback_data="jarvis_unmute_direct")]]
        )
        kwargs = {"chat_id": chat_id, "text": notice, "reply_markup": kb}
        if bus_id:
            kwargs["business_connection_id"] = bus_id
        try:
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
            return f"[Фотография: {desc or 'изображение'}]{caption}", False
        finally:
            tmp_path.unlink(missing_ok=True)

    if message.text:
        return message.text, False

    return "Передан медиа-сигнал.", False


async def send_smart_response(
    chat_id: int,
    bus_id: str,
    reply_text: str,
    is_direct: bool = False,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    send_as_voice: bool = False
):
    if not reply_text or not reply_text.strip():
        reply_text = "Интерфейс зафиксировал сигнал. Ожидаю инструкций."

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
                    await bot.send_audio(**kwargs, audio=media_file, title="Jarvis Voice")
                return
            except Exception as e:
                logger.error(f"Сбой отправки голоса: {e}")
            finally:
                audio_path.unlink(missing_ok=True)

    try:
        await bot.send_message(**kwargs, text=reply_text, parse_mode="HTML")
    except TelegramBadRequest:
        await bot.send_message(**kwargs, text=reply_text)
    except Exception as e:
        logger.error(f"Ошибка доставки сообщения: {e}")


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
                comp = await client.chat.completions.create(
                    model=model_name.strip(),
                    messages=history,
                    temperature=0.6,
                    max_tokens=max_tokens
                )
                if comp.usage:
                    today_prompt_tokens += comp.usage.prompt_tokens
                    today_completion_tokens += comp.usage.completion_tokens
                    total_requests_today += 1
                    asyncio.create_task(save_stats())

                reply = clean_cot_output(comp.choices[0].message.content or "")
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


# --- ОБРАБОТКА КОМАНД ---
async def process_bot_command(message: types.Message, user_input: str, is_owner: bool, bus_id: str = "") -> bool:
    global force_offline_mode, always_answer_mode, last_owner_activity, nsfw_art_mode

    chat_id = message.chat.id
    lower = user_input.lower().strip()
    is_direct = not bool(bus_id)

    if lower in ["игра", "тапалка", "/game"]:
        await send_smart_response(chat_id, bus_id, f"Инициирую запуск систем:\n{GAME_URL}", is_direct=is_direct)
        return True

    # Управление 18+
    if lower in ["джарвис 18+ вкл", "!18+ вкл", "18+ вкл", "включи 18+"]:
        if not is_owner:
            await send_smart_response(chat_id, bus_id, "Отказ в доступе. Требуется авторизация создателя, сэр.", is_direct=is_direct)
            return True
        nsfw_art_mode = True
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Бесконечный протокол 18+ активирован. Генератор без цензуры включен, сэр.", is_direct=is_direct, send_as_voice=True)
        return True

    if lower in ["джарвис 18+ выкл", "!18+ выкл", "18+ выкл", "выключи 18+"]:
        if not is_owner:
            return True
        nsfw_art_mode = False
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Фильтр безопасности 18+ активирован, сэр.", is_direct=is_direct)
        return True

    # Генерация артов
    if re.search(r"\b(нарисуй|сгенерируй|создай арт|арт)\b", lower) or lower.startswith("!арт"):
        prompt = re.sub(r"\b(джарвис|пожалуйста|нарисуй|сгенерируй|создай арт|арт|!арт)\b", "", user_input, flags=re.I).strip(" ,:;!?")
        if not prompt:
            await send_smart_response(chat_id, bus_id, "Укажите техническое задание для генерации, сэр.", is_direct=is_direct)
            return True

        if is_nsfw_request(prompt) and not nsfw_art_mode:
            await send_smart_response(chat_id, bus_id, "Протокол безопасности: взрослый контент заблокирован. Активируйте <code>18+ вкл</code>, сэр.", is_direct=is_direct)
            return True

        await send_smart_response(chat_id, bus_id, f"Инициирую бесконечный синтез: <i>«{prompt}»</i>...", is_direct=is_direct)
        is_anime = any(w in prompt.lower() for w in ["аниме", "хентай", "тян", "2d"])
        img_bytes = await generate_infinite_nsfw(prompt, is_anime=is_anime)

        if img_bytes:
            gen_id = uuid.uuid4().hex[:8]
            art_prompts_cache[gen_id] = prompt
            photo = BufferedInputFile(img_bytes, filename=f"art_{gen_id}.jpg")
            kb = get_art_keyboard(gen_id, current_style="anime" if is_anime else "real")

            kwargs = {
                "chat_id": chat_id,
                "photo": photo,
                "caption": f"Готово, сэр.\nЗапрос: {prompt}",
                "reply_markup": kb
            }
            if bus_id:
                kwargs["business_connection_id"] = bus_id
            await bot.send_photo(**kwargs)
        else:
            await send_smart_response(chat_id, bus_id, "Сбой соединения с узлами рендеринга. Повторите попытку, сэр.", is_direct=is_direct)
        return True

    # Голосовой режим
    if lower in ["джарвис голос вкл", "!голос вкл", "голос вкл"]:
        voice_chat_modes[chat_id] = True
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Постоянный голосовой модуль активирован, сэр.", is_direct=is_direct, send_as_voice=True)
        return True

    if lower in ["джарвис голос выкл", "!голос выкл", "голос выкл"]:
        voice_chat_modes.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Постоянный голосовой модуль деактивирован, сэр.", is_direct=is_direct)
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

    # Отчет о гостях
    if lower in ["кто писал?", "кто писал", "!отчет", "отчет", "джарвис отчет", "визиты"]:
        if not business_visits:
            await send_smart_response(chat_id, bus_id, "Входящих контактов за время вашего отсутствия не зафиксировано, сэр.", is_direct=is_direct)
            return True

        report_lines = ["<b>Оперативный журнал контактов (Telegram Business):</b>\n"]
        for cid, info in list(business_visits.items())[-8:]:
            report_lines.append(
                f"• <b>{info.get('name', 'Гость')}</b> ({info.get('username', '')}) в {info.get('time', '')}:\n"
                f"  <i>«{info.get('last_msg', '')}»</i>"
            )
        await send_smart_response(chat_id, bus_id, "\n\n".join(report_lines), is_direct=is_direct)
        return True

    if lower in ["очисти отчет", "!очисти отчет"]:
        business_visits.clear()
        await save_visits()
        await send_smart_response(chat_id, bus_id, "Журнал визитов собеседников успешно очищен, сэр.", is_direct=is_direct)
        return True

    if not is_owner:
        return False

    # Владельческие команды
    if lower in ["джарвис я тут", "!онлайн", "я тут"]:
        force_offline_mode = False
        last_owner_activity = time.time()
        await send_smart_response(chat_id, bus_id, "С возвращением, сэр. Я ухожу в тень.", is_direct=is_direct)
        return True

    if lower in ["джарвис я отошел", "!офлайн", "я отошел"]:
        force_offline_mode = True
        await send_smart_response(chat_id, bus_id, "Охранный протокол активирован. Отвечаю гостям, сэр.", is_direct=is_direct)
        return True

    if lower.startswith(("мут", "!мут")):
        parts = user_input.split()
        duration = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if duration:
            muted_chats[chat_id] = time.time() + (duration * 60)
            notice = f"Собеседник изолирован на {duration} минут, сэр."
        else:
            muted_chats[chat_id] = float('inf')
            notice = "Собеседник подвергнут бессрочной изоляции, сэр."
        await save_settings()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Снять изоляцию", callback_data="jarvis_unmute_direct")]])
        await send_smart_response(chat_id, bus_id, notice, is_direct=is_direct, reply_markup=kb)
        return True

    if lower in ["размут", "!размут", "анмут"]:
        muted_chats.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Изоляция собеседника снята, сэр.", is_direct=is_direct)
        return True

    if lower in ["сброс", "джарвис сброс"]:
        user_histories.pop(chat_id, None)
        await save_histories()
        await send_smart_response(chat_id, bus_id, "Оперативная память диалога очищена, сэр.", is_direct=is_direct)
        return True

    if lower in ["статус", "!статус"]:
        tts_source = "Fish Audio (Клон)" if FISH_AUDIO_API_KEY else "Edge-TTS (Нейро-голос)"
        models = await groq_mgr.get_active_models()
        primary_m = models[0] if models else "LLaMA-3.3"
        status_msg = (
            f"<b>Диагностика систем JARVIS:</b>\n"
            f"• Протокол 18+ (NSFW): <b>{'БЕСКОНЕЧНЫЙ (AI Horde + Fallback)' if nsfw_art_mode else 'Заблокирован'}</b>\n"
            f"• Голосовой синтез: {tts_source} (FFmpeg: {'ВКЛ' if HAS_FFMPEG else 'ВЫКЛ'})\n"
            f"• Модель логики: {primary_m}\n"
            f"• Ключей Groq онлайн: {len(GROQ_KEYS)}\n"
            f"• Активных напоминаний: {len(reminders_list)}"
        )
        await send_smart_response(chat_id, bus_id, status_msg, is_direct=is_direct)
        return True

    return False


# --- CALLBACK ОБРАБОТЧИКИ ---
@dp.callback_query(F.data.startswith("art_retry:"))
async def handle_art_retry(callback: types.CallbackQuery):
    try:
        _, gen_id, target_style = callback.data.split(":")
    except ValueError:
        await callback.answer("Ошибка формата кнопки.", show_alert=True)
        return

    prompt = art_prompts_cache.get(gen_id)
    if not prompt:
        await callback.answer("Сессия генерации устарела. Отправьте запрос заново.", show_alert=True)
        return

    await callback.answer("Генерирую новый вариант...")
    await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_PHOTO)

    img_bytes = await generate_infinite_nsfw(prompt, is_anime=(target_style == "anime"))
    if img_bytes:
        new_gen_id = uuid.uuid4().hex[:8]
        art_prompts_cache[new_gen_id] = prompt
        photo = BufferedInputFile(img_bytes, filename=f"art_{new_gen_id}.jpg")
        kb = get_art_keyboard(new_gen_id, current_style=target_style)

        style_label = "Аниме" if target_style == "anime" else "Реализм"
        await bot.send_photo(
            chat_id=callback.message.chat.id,
            photo=photo,
            caption=f"Вариант [{style_label}]. Запрос: {prompt}",
            reply_markup=kb
        )
    else:
        await callback.message.reply("Сервер перегружен. Повторите нажатие через пару секунд, сэр.")


@dp.callback_query(F.data.startswith("art_delete:"))
async def handle_art_delete(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
        await callback.answer("Арт успешно удален.")
    except Exception:
        await callback.answer("Не удалось удалить сообщение.", show_alert=True)


@dp.callback_query(F.data == "jarvis_unmute_direct")
async def handle_unmute(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if callback.from_user.id != OWNER_ID and OWNER_ID != 0:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    muted_chats.pop(chat_id, None)
    await save_settings()
    await callback.answer("Изоляция снята.")
    try:
        await callback.message.edit_text("Ограничения для собеседника аннулированы, сэр.")
    except Exception:
        pass


# --- ХЭНДЛЕРЫ СООБЩЕНИЙ ---
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

    voice_triggers = ["в голосовом", "голосовым", "голосом", "скажи", "озвучь", "проговори"]
    forced_voice = any(t in user_input.lower() for t in voice_triggers)
    should_voice = is_voice or forced_voice or voice_chat_modes.get(chat_id, False) or (random.random() < 0.20)

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

    now = time.time()
    if not always_answer_mode and not force_offline_mode:
        if (now - last_owner_activity) < OWNER_IDLE_TIMEOUT:
            return

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
    should_voice = is_voice or voice_chat_modes.get(chat_id, False) or (random.random() < 0.20)
    await send_smart_response(chat_id, bus_id, reply, is_direct=False, send_as_voice=should_voice)


# --- ФОНОВЫЕ СЛУЖБЫ ---
async def cleaner_and_reminders_task():
    global reminders_list
    while True:
        try:
            await asyncio.sleep(5)
            now = time.time()

            expired_mutes = [cid for cid, exp in muted_chats.items() if exp != float('inf') and now >= exp]
            if expired_mutes:
                for cid in expired_mutes:
                    muted_chats.pop(cid, None)
                await save_settings()

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
        logger.critical("TELEGRAM_BOT_TOKEN не задан!")
        return

    http_session = aiohttp.ClientSession()
    web_runner = await setup_web_app()
    bg_task = asyncio.create_task(cleaner_and_reminders_task())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info(f"Джарвис онлайн. Движок 18+: AI Horde (Бесконечный). Голос: {'Fish Audio' if FISH_AUDIO_API_KEY else 'Edge-TTS'}")
        await dp.start_polling(bot)
    except TelegramConflictError:
        logger.critical("Запущен дубликат бота!")
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
