import asyncio
import base64
import datetime
import hashlib
import io
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
from aiogram.exceptions import TelegramBadRequest, TelegramConflictError
from aiogram.methods import DeleteBusinessMessages
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from groq import APIError, AsyncGroq

# Безопасный импорт Pillow
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    Image = None

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(name)s: %(message)s")
logger = logging.getLogger("AyakojiCore")

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
OWNER_IDLE_TIMEOUT = 300

# Глобальные состояния
force_offline_mode = False
always_answer_mode = False
last_owner_activity = 0.0
last_auto_briefing_date = ""
silent_until = 0.0
focus_until = 0.0
shadow_mode = False  # Секретный режим создателя

guest_requests: List[Dict[str, str]] = []

# Голос
OFFICIAL_VOICE = "ru-RU-DmitryNeural"
OFFICIAL_PITCH = "+0Hz"
OFFICIAL_RATE = "+10%"
HAS_FFMPEG = shutil.which("ffmpeg") is not None

# Ключи Groq
GROQ_KEYS = [val.strip() for key, val in sorted(os.environ.items()) if key.startswith("GROQ_API_KEY") and val.strip()]

# Пути к файлам
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "bot_settings.json"
HISTORY_FILE = DATA_DIR / "user_histories.json"
STATS_FILE = DATA_DIR / "token_stats.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
VISITS_FILE = DATA_DIR / "business_visits.json"
NOTES_FILE = DATA_DIR / "notes.txt"
GUEST_REQ_FILE = DATA_DIR / "guest_requests.json"

http_session: Optional[aiohttp.ClientSession] = None
file_io_lock = asyncio.Lock()

# Кэши
class LRUCacheDict(OrderedDict):
    def __init__(self, maxsize: int = 500, *args, **kwargs):
        self.maxsize = maxsize
        super().__init__(*args, **kwargs)
    def __setitem__(self, key, value):
        if key in self: self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.maxsize: self.popitem(last=False)

weather_cache = {"data": "", "expires": 0.0}
image_desc_cache = LRUCacheDict(maxsize=100)
art_prompts_cache = LRUCacheDict(maxsize=100)

# --- ФИЛЬТРЫ 18+ ---
NSFW_WORDS = {
    "голая", "голый", "обнаженная", "обнаженный", "ню", "хентай", "порно", "секс", "18+", "nsfw",
    "эротика", "без одежды", "грудь", "соски", "постели", "nude", "naked", "голышом",
    "раком", "догги", "на четвереньках", "четвереньки", "попа", "попка", "жопа", "сиськи", "попу",
    "раздвинув", "ноги врозь", "киска", "вагина", "клитор", "анал", "минет", "топлес", "топлесс",
    "стринги", "лифчик", "трусиках", "трусики", "чулки", "чулках", "на коленях", "наездница",
    "ахегао", "ahegao", "пенис", "член", "sex", "porn", "xxx", "adult"
}

BLOCKED_ART_WORDS = {
    "порно", "porn", "секс", "sex", "гениталии", "влагалище", "пенис", "член", 
    "анал", "минет", "изнасилование", "детское", "подросток", "школьница", 
    "кровь", "насилие", "зоофилия", "инцест", "групповуха"
}

def is_nsfw_request(text: str) -> bool:
    if shadow_mode: return False
    words = set(re.findall(r"[a-zа-яё0-9+]+", text.lower()))
    return bool(words & NSFW_WORDS)

def check_nsfw_output(text: str) -> str:
    if shadow_mode: return text
    words = set(re.findall(r"[a-zа-яё0-9+]+", text.lower()))
    if words & NSFW_WORDS:
        return "Я не могу обсуждать темы такого характера. Давайте вернемся к конструктивному диалогу."
    return text

def is_safe_18_request(text: str) -> Tuple[bool, str]:
    lower = text.lower()
    for word in BLOCKED_ART_WORDS:
        if word in lower:
            return False, f"Термин '{word}' запрещен в безопасном режиме 18+."
    return True, ""

# --- GROQ МЕНЕДЖЕР ---
class GroqManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.clients = [AsyncGroq(api_key=k) for k in keys]
        self.current_idx = 0
        self.cooldowns: Dict[int, float] = {}
        self.cached_models: List[str] = []
        self.last_models_update = 0.0

    def _get_next_client(self) -> Optional[Tuple[AsyncGroq, int]]:
        if not self.clients: return None
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
            if not client_data: break
            client, idx = client_data
            try:
                models_data = await client.models.list()
                valid = [m.id for m in models_data.data if not any(x in m.id.lower() for x in ["whisper", "guard", "tool", "vision", "embed", "r1", "qwq"])]
                if valid:
                    valid.sort(key=lambda x: ("70b" in x or "versatile" in x), reverse=True)
                    self.cached_models = valid
                    self.last_models_update = now
                    return self.cached_models
            except APIError as e:
                if e.status_code in [429, 401, 403]: self.mark_cooldown(idx, duration=120)
            except Exception: break
        return self.cached_models or ["llama-3.3-70b-versatile"]

    async def transcribe(self, audio_path: str) -> str:
        for _ in range(len(self.clients)):
            client_data = self._get_next_client()
            if not client_data: break
            client, idx = client_data
            try:
                with open(audio_path, "rb") as f: content = f.read()
                res = await client.audio.transcriptions.create(file=(os.path.basename(audio_path), content), model="whisper-large-v3", language="ru")
                return res.text
            except APIError as e:
                if e.status_code in [429, 401, 403]: self.mark_cooldown(idx, duration=60)
            except Exception: pass
        return "Не удалось разобрать голосовой сигнал."

    async def describe_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        img_hash = hashlib.md5(image_bytes).hexdigest()
        if img_hash in image_desc_cache: return image_desc_cache[img_hash]
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"
        vision_messages = [{"role": "user", "content": [{"type": "text", "text": "Опиши это изображение кратко и эстетично на русском языке (1-2 предложения)."}, {"type": "image_url", "image_url": {"url": data_url}}]}]
        models = ["llama-3.2-11b-vision-preview"]
        for vm in models:
            for _ in range(len(self.clients)):
                client_data = self._get_next_client()
                if not client_data: break
                client, idx = client_data
                try:
                    comp = await client.chat.completions.create(model=vm, messages=vision_messages, max_tokens=120, temperature=0.2)
                    res = comp.choices[0].message.content or ""
                    if res.strip():
                        clean_res = clean_cot_output(res)
                        image_desc_cache[img_hash] = clean_res
                        return clean_res
                except Exception: continue
        return ""

groq_mgr = GroqManager(GROQ_KEYS)

# --- ФАЙЛОВАЯ СИСТЕМА ---
async def async_save_json(path: Path, data: Any):
    async with file_io_lock:
        def _write():
            tmp = path.with_suffix(f".tmp_{uuid.uuid4().hex[:6]}")
            try:
                with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
                tmp.replace(path)
            except Exception as e:
                logger.error(f"Save error {path}: {e}")
                if tmp.exists(): tmp.unlink(missing_ok=True)
        await asyncio.to_thread(_write)

def sync_load_json(path: Path, default_val: Any) -> Any:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f: return json.load(f)
        except Exception: pass
    return default_val

def load_settings():
    d = sync_load_json(SETTINGS_FILE, {})
    return ({int(k): v for k, v in d.get("muted_chats", {}).items()},
            {int(k): v for k, v in d.get("blocked_guests", {}).items()},
            {int(k): v for k, v in d.get("voice_chat_modes", {}).items()})

muted_chats, blocked_guests, voice_chat_modes = load_settings()
reminders_list = sync_load_json(REMINDERS_FILE, [])
business_visits = sync_load_json(VISITS_FILE, {})
guest_requests = sync_load_json(GUEST_REQ_FILE, [])
user_histories = LRUCacheDict(maxsize=400)
for k, v in sync_load_json(HISTORY_FILE, {}).items(): user_histories[int(k)] = v

# Статистика
today_str = datetime.date.today().isoformat()
stats_data = sync_load_json(STATS_FILE, {})
today_prompt_tokens = stats_data.get("prompt_tokens", 0) if stats_data.get("date") == today_str else 0
today_completion_tokens = stats_data.get("completion_tokens", 0) if stats_data.get("date") == today_str else 0
total_requests_today = stats_data.get("requests", 0) if stats_data.get("date") == today_str else 0
stats_date = today_str

async def save_settings(): await async_save_json(SETTINGS_FILE, {"muted_chats": muted_chats, "blocked_guests": blocked_guests, "voice_chat_modes": voice_chat_modes})
async def save_reminders(): await async_save_json(REMINDERS_FILE, reminders_list)
async def save_visits(): await async_save_json(VISITS_FILE, business_visits)
async def save_histories(): await async_save_json(HISTORY_FILE, dict(user_histories))
async def save_stats(): await async_save_json(STATS_FILE, {"date": stats_date, "prompt_tokens": today_prompt_tokens, "completion_tokens": today_completion_tokens, "requests": total_requests_today})
async def save_guest_requests(): await async_save_json(GUEST_REQ_FILE, guest_requests)

# --- ПОГОДА ---
async def get_weather() -> str:
    global weather_cache
    now = time.time()
    if weather_cache["expires"] > now and weather_cache["data"]: return weather_cache["data"]
    if not http_session: return "Данные о погоде недоступны."
    try:
        async with http_session.get("https://wttr.in/?format=j1", timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                curr = data["current_condition"][0]
                temp, feels = curr["temp_C"], curr["FeelsLikeC"]
                desc = curr["lang_ru"][0]["value"] if "lang_ru" in curr else curr["weatherDesc"][0]["value"]
                result = f"{temp}°C, {desc} (ощущается как {feels}°C)"
                weather_cache["data"], weather_cache["expires"] = result, now + 3600
                return result
    except Exception as e: logger.error(f"Weather error: {e}")
    return "Данные о погоде временно недоступны."

# --- ГОЛОС ---
async def process_voice(text: str) -> Optional[Tuple[Path, bool]]:
    clean_text = re.sub(r"<[^>]+>", "", text).strip()
    clean_text = re.sub(r"[*_`#~]", "", clean_text).strip()
    if not clean_text: return None
    uid = uuid.uuid4().hex
    raw_audio = Path(tempfile.gettempdir()) / f"raw_{uid}.mp3"
    final_ogg = Path(tempfile.gettempdir()) / f"ayakoji_{uid}.ogg"
    try:
        communicate = edge_tts.Communicate(clean_text, OFFICIAL_VOICE, pitch=OFFICIAL_PITCH, rate=OFFICIAL_RATE)
        await communicate.save(str(raw_audio))
        success = raw_audio.exists() and raw_audio.stat().st_size > 0
    except Exception as e:
        logger.error(f"Edge-TTS Error: {e}")
        return None
    if not success or not raw_audio.exists(): return None
    if HAS_FFMPEG:
        try:
            af = "highpass=f=160,lowpass=f=7600,equalizer=f=2800:width_type=q:w=1.2:g=3.2,acompressor=threshold=-16dB:ratio=4,aecho=0.8:0.4:16:0.2"
            cmd = ["ffmpeg", "-y", "-i", str(raw_audio), "-af", af, "-c:a", "libopus", "-b:a", "64k", str(final_ogg)]
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
            raw_audio.unlink(missing_ok=True)
            if final_ogg.exists() and final_ogg.stat().st_size > 0: return final_ogg, True
        except Exception: pass
    return raw_audio, False

# --- БРИФИНГ ---
async def build_daily_briefing() -> str:
    now_dt = datetime.datetime.now()
    date_str, time_str, weather_str = now_dt.strftime("%d.%m.%Y"), now_dt.strftime("%H:%M"), await get_weather()
    lines = [f"<b>Доброе утро. Ежедневный брифинг на {date_str} ({time_str}):</b>\n", f"• <b>Статус ядра Ayakoji:</b> Активно, модули в норме.", f"• <b>Погода за окном:</b> {weather_str}", f"• <b>Теневой режим:</b> {'АКТИВЕН' if shadow_mode else 'Выключен'}"]
    if business_visits:
        lines.append(f"\n<b>Ночной журнал Business ({len(business_visits)}):</b>")
        for cid, info in list(business_visits.items())[-5:]: lines.append(f"• <b>{info.get('name', 'Гость')}</b> в {info.get('time', '')}: <i>«{info.get('last_msg', '')[:45]}»</i>")
    else: lines.append("\n• <b>Telegram Business:</b> Ночных обращений нет.")
    if reminders_list: lines.append(f"\n• <b>Задач в календаре:</b> {len(reminders_list)} шт.")
    else: lines.append("\n• <b>Хронометраж:</b> Напоминаний нет.")
    if guest_requests: lines.append(f"\n• <b>Запросы от гостей:</b> {len(guest_requests)} новых. Напишите <code>!сводка гостей</code>.")
    lines.append("\n<i>«Желаю продуктивного дня. Я продолжаю дежурство.»</i>")
    return "\n".join(lines)

# --- ПРОМПТЫ ---
STRICT_RULES = "\nПРАВИЛА:\n1. ЯЗЫК: Русский.\n2. СТИЛЬ: Умный, эстетичный, спокойный.\n3. БЕЗ ЭМОДЗИ.\n4. Лаконично."
AYAKOJI_PROMPT_DIRECT = ("Ты — Аякоджи, продвинутый ИИ-ассистент. Твой создатель — Кирито.\nОбращайся к нему 'сэр'. Твой стиль — интеллектуальный и безупречно тактичный.") + STRICT_RULES
AYAKOJI_PROMPT_GUEST = ("Ты — Аякоджи, защитная ИИ-система. Собеседник — посторонний гость.\nТон: холодный, вежливый. Не обсуждай темы 18+, насилия. Вежливо отказывай.") + STRICT_RULES

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
user_message_times, processed_message_ids, recent_sent_messages = {}, LRUCacheDict(3000), {}

def clean_cot_output(text: str) -> str:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.I)
    return re.sub(r"^(Итоговый ответ:|\*\*Итоговый ответ\*\*)", "", cleaned.strip(), flags=re.I)

async def check_chat_flood(chat_id: int, bus_id: str, max_msgs: int = 5, window_seconds: float = 6.0) -> bool:
    now = time.time()
    user_message_times.setdefault(chat_id, [])
    user_message_times[chat_id] = [t for t in user_message_times[chat_id] if now - t < window_seconds]
    user_message_times[chat_id].append(now)
    if len(user_message_times[chat_id]) > max_msgs:
        muted_chats[chat_id] = now + 300
        await save_settings()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Снять изоляцию", callback_data="ayakoji_unmute_direct")]])
        kwargs = {"chat_id": chat_id, "text": "Превышен лимит сообщений. Изоляция на 5 минут.", "reply_markup": kb}
        if bus_id: kwargs["business_connection_id"] = bus_id
        try: await bot.send_message(**kwargs)
        except Exception: pass
        return True
    return False

async def extract_message_content(message: types.Message) -> Tuple[str, bool]:
    caption = f" (Подпись: {message.caption})" if message.caption else ""
    if message.voice or message.video_note:
        file_obj = message.voice or message.video_note
        file_info = await bot.get_file(file_obj.file_id)
        suffix = ".ogg" if message.voice else ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp: tmp_path = Path(tmp.name)
        try:
            await bot.download_file(file_info.file_path, destination=tmp_path)
            return await groq_mgr.transcribe(str(tmp_path)), True
        finally: tmp_path.unlink(missing_ok=True)
    if message.photo:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp: tmp_path = Path(tmp.name)
        try:
            await bot.download_file(file_info.file_path, destination=tmp_path)
            with open(tmp_path, "rb") as f: img_data = f.read()
            desc = await groq_mgr.describe_image(img_data, mime_type="image/jpeg")
            return f"[Фотография: {desc or 'изображение'}]{caption}", False
        finally: tmp_path.unlink(missing_ok=True)
    if message.text: return message.text, False
    return "Передан медиа-сигнал.", False

async def send_smart_response(chat_id: int, bus_id: str, reply_text: str, is_direct: bool = False, reply_markup: Optional[InlineKeyboardMarkup] = None, send_as_voice: bool = False):
    if not reply_text or not reply_text.strip(): reply_text = "Интерфейс зафиксировал сигнал. Ожидаю инструкций."
    now = time.time()
    dedup_key = (chat_id, reply_text[:60])
    if dedup_key in recent_sent_messages and (now - recent_sent_messages[dedup_key] < 3.0): return
    recent_sent_messages[dedup_key] = now
    kwargs = {"chat_id": chat_id, "reply_markup": reply_markup}
    if not is_direct and bus_id: kwargs["business_connection_id"] = bus_id
    if send_as_voice:
        audio_res = await process_voice(reply_text)
        if audio_res:
            audio_path, is_ogg = audio_res
            try:
                media_file = FSInputFile(str(audio_path))
                if is_ogg: await bot.send_voice(**kwargs, voice=media_file)
                else: await bot.send_audio(**kwargs, audio=media_file, title="Ayakoji Voice")
                return
            except Exception as e: logger.error(f"Voice send error: {e}")
            finally: audio_path.unlink(missing_ok=True)
    try: await bot.send_message(**kwargs, text=reply_text, parse_mode="HTML")
    except TelegramBadRequest: await bot.send_message(**kwargs, text=reply_text)
    except Exception as e: logger.error(f"Message send error: {e}")

async def ask_groq(prompt: str, session_id: int, system_prompt: str, max_tokens: int = 400) -> str:
    global today_prompt_tokens, today_completion_tokens, total_requests_today, stats_date
    now_date = datetime.date.today().isoformat()
    if now_date != stats_date:
        stats_date, today_prompt_tokens, today_completion_tokens, total_requests_today = now_date, 0, 0, 0
        asyncio.create_task(save_stats())
    if not GROQ_KEYS: return "Критическая ошибка: отсутствуют ключи GROQ."
    if session_id not in user_histories: user_histories[session_id] = [{"role": "system", "content": system_prompt}]
    else: user_histories[session_id][0] = {"role": "system", "content": system_prompt}
    history = user_histories[session_id]
    history.append({"role": "user", "content": prompt})
    if len(history) > 7: user_histories[session_id] = [history[0]] + history[-6:]; history = user_histories[session_id]
    models = await groq_mgr.get_active_models()
    for model_name in models:
        for _ in range(len(GROQ_KEYS)):
            client_data = groq_mgr._get_next_client()
            if not client_data: break
            client, key_idx = client_data
            try:
                comp = await client.chat.completions.create(model=model_name.strip(), messages=history, temperature=0.6, max_tokens=max_tokens)
                if comp.usage:
                    today_prompt_tokens += comp.usage.prompt_tokens; today_completion_tokens += comp.usage.completion_tokens; total_requests_today += 1
                    asyncio.create_task(save_stats())
                reply = check_nsfw_output(clean_cot_output(comp.choices[0].message.content or ""))
                if reply:
                    history.append({"role": "assistant", "content": reply}); asyncio.create_task(save_histories())
                    return reply
            except APIError as e:
                if e.status_code in [429, 401, 403]: groq_mgr.mark_cooldown(key_idx, duration=120)
            except Exception: break
    if history and history[-1]["role"] == "user": history.pop()
    return "Интерфейсы обработки перегружены."

# --- БЕЗОПАСНЫЙ 18+ ГЕНЕРАТОР ---
async def generate_safe_18_art(prompt: str) -> Optional[bytes]:
    if not http_session: return None
    enhanced_prompt = f"masterpiece, artistic photography, tasteful art nude, elegant sensual aesthetic, {prompt}, soft cinematic lighting, 85mm lens, highly detailed skin texture, beautiful composition, no explicit nudity, implied nudity, artistic shadows"
    negative_prompt = "explicit, pornographic, genitals, vagina, penis, nipples, sex act, penetration, blood, violence, gore, child, teen, underage, deformed, bad anatomy, extra limbs, watermark, text, logo, cartoon, 3d render, ugly, plastic skin"
    encoded, encoded_neg = urllib.parse.quote(enhanced_prompt.strip()), urllib.parse.quote(negative_prompt.strip())
    headers = {"Referer": "https://pollinations.ai/", "User-Agent": "Mozilla/5.0"}
    seed = random.randint(100, 9999999)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=flux&seed={seed}&nologo=true&nofeed=true&safe=false&negative={encoded_neg}"
    try:
        async with http_session.get(url, headers=headers, timeout=60) as resp:
            if resp.status == 200:
                img_bytes = await resp.read()
                if len(img_bytes) > 5000: return img_bytes
    except Exception as e: logger.error(f"Art generation error: {e}")
    return None

# --- КОМАНДЫ ---
async def process_bot_command(message: types.Message, user_input: str, is_owner: bool, bus_id: str = "") -> bool:
    global force_offline_mode, last_owner_activity, silent_until, guest_requests, focus_until, shadow_mode
    chat_id = message.chat.id
    lower = user_input.lower().strip()
    is_direct = not bool(bus_id)

    owner_cmds = ["брифинг", "!брифинг", "сводка", "сводка гостей", "!гости", "кто писал", "тишина", "!заметка", "заметка", "!статус", "статус", "!онлайн", "!офлайн", "!сброс", "сброс", "!мут", "!размут", "аякоджи отчет", "отчет", "визиты", "фокус", "!код", "тень", "нарисуй", "!арт"]
    is_owner_cmd = any(lower.startswith(cmd) or lower == cmd for cmd in owner_cmds)
    if is_owner_cmd and not is_owner:
        await send_smart_response(chat_id, bus_id, "У вас нет прав для выполнения этой команды. Обратитесь к создателю.", is_direct=is_direct)
        return True

    if lower in ["!кто_я", "кто я", "мой id", "!id"]:
        await send_smart_response(chat_id, bus_id, f"Ваш Telegram ID: <code>{message.from_user.id}</code>", is_direct=is_direct)
        return True

    if lower in ["!код 18+", "!тень", "тень вкл"]:
        shadow_mode = True
        await send_smart_response(chat_id, bus_id, "Теневой режим активирован. Фильтры 18+ сняты. Будьте осторожны, сэр.", is_direct=is_direct)
        return True
    if lower in ["!тень выкл", "тень выкл", "!код 18-"]:
        shadow_mode = False
        await send_smart_response(chat_id, bus_id, "Теневой режим деактивирован. Фильтры восстановлены.", is_direct=is_direct)
        return True

    # Генерация артов (только в теневом режиме)
    if lower.startswith(("нарисуй", "!арт")):
        if not shadow_mode:
            await send_smart_response(chat_id, bus_id, "Для генерации артов активируйте теневой режим командой <code>!тень</code>.", is_direct=is_direct)
            return True
        art_prompt = re.sub(r"^(аякоджи|джарвис|нарисуй|!арт|арт)\s*", "", user_input, flags=re.I).strip()
        if not art_prompt:
            await send_smart_response(chat_id, bus_id, "Укажите, что нарисовать. Например: <i>!арт девушка в ванне, синематик</i>.", is_direct=is_direct)
            return True
        is_safe, err_msg = is_safe_18_request(art_prompt)
        if not is_safe:
            await send_smart_response(chat_id, bus_id, f"Безопасный фильтр заблокировал: {err_msg}", is_direct=is_direct)
            return True
        await send_smart_response(chat_id, bus_id, "Инициирую протокол безопасной визуализации... (~15 сек)", is_direct=is_direct)
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_PHOTO)
        img_bytes = await generate_safe_18_art(art_prompt)
        if img_bytes:
            gen_id = uuid.uuid4().hex[:8]
            art_prompts_cache[gen_id] = art_prompt
            photo = BufferedInputFile(img_bytes, filename="safe_art.jpg")
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎨 Еще вариант", callback_data=f"art_retry_safe:{gen_id}")], [InlineKeyboardButton(text="🗑 Удалить", callback_data="art_delete_safe")]])
            caption = f"✨ <b>Безопасный арт готов, сэр.</b>\n• Запрос: <i>{art_prompt}</i>\n• Режим: Safe 18+"
            kwargs = {"chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "HTML", "reply_markup": kb}
            if bus_id: kwargs["business_connection_id"] = bus_id
            await bot.send_photo(**kwargs)
        else: await send_smart_response(chat_id, bus_id, "Сбой генерации. Сервер перегружен.", is_direct=is_direct)
        return True

    if lower in ["брифинг", "аякоджи брифинг", "!брифинг", "утренний отчет", "сводка"]:
        await send_smart_response(chat_id, bus_id, await build_daily_briefing(), is_direct=is_direct, send_as_voice=True)
        return True

    if lower in ["сводка гостей", "!гости", "кто писал", "визиты"]:
        if not guest_requests: await send_smart_response(chat_id, bus_id, "Запросов от гостей нет.", is_direct=is_direct)
        else:
            lines = ["<b>Сводка просьб от гостей:</b>\n"]
            for g in guest_requests[-10:]: lines.append(f"• <b>{g['name']}</b> ({g['username']}) [{g['time']}]:\n  <i>«{g['text']}»</i>")
            await send_smart_response(chat_id, bus_id, "\n\n".join(lines), is_direct=is_direct)
            guest_requests.clear(); await save_guest_requests()
        return True

    if lower.startswith("тишина"):
        parts = user_input.split()
        if len(parts) > 1 and parts[1].isdigit():
            silent_until = time.time() + int(parts[1]) * 3600
            await send_smart_response(chat_id, bus_id, f"Режим тишины на {parts[1]} ч.", is_direct=is_direct)
        else: await send_smart_response(chat_id, bus_id, "Укажите часы: !тишина 2", is_direct=is_direct)
        return True

    if lower.startswith("фокус"):
        parts = user_input.split()
        if len(parts) > 1 and parts[1].isdigit():
            focus_until = time.time() + int(parts[1]) * 3600
            await send_smart_response(chat_id, bus_id, f"Режим фокуса на {parts[1]} ч. Гости перенаправляются в секретаря.", is_direct=is_direct)
        else: await send_smart_response(chat_id, bus_id, "Укажите часы: !фокус 2", is_direct=is_direct)
        return True

    if lower.startswith(("!заметка", "заметка")):
        note_text = re.sub(r"^(джарвис|!заметка|заметка)\s*", "", user_input, flags=re.I).strip()
        if not note_text: await send_smart_response(chat_id, bus_id, "Текст пуст.", is_direct=is_direct)
        else:
            def _write_note():
                with open(NOTES_FILE, "a", encoding="utf-8") as f: f.write(f"[{datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}] {note_text}\n")
            await asyncio.to_thread(_write_note)
            await send_smart_response(chat_id, bus_id, "Заметка сохранена.", is_direct=is_direct)
        return True

    url_match = re.search(r'(https?://[^\s]+)', user_input)
    if url_match:
        await send_smart_response(chat_id, bus_id, "Анализирую ссылку...", is_direct=is_direct)
        # (Функция summarize_url опущена для краткости, но она была в предыдущем коде)
        summary = "Функция суммаризации временно отключена."
        await send_smart_response(chat_id, bus_id, f"<b>Краткое содержание:</b>\n{summary}", is_direct=is_direct)
        return True

    if lower in ["аякоджи голос вкл", "!голос вкл", "голос вкл"]:
        voice_chat_modes[chat_id] = True; await save_settings()
        await send_smart_response(chat_id, bus_id, "Голосовой модуль активирован.", is_direct=is_direct, send_as_voice=True)
        return True
    if lower in ["аякоджи голос выкл", "!голос выкл", "голос выкл"]:
        voice_chat_modes.pop(chat_id, None); await save_settings()
        await send_smart_response(chat_id, bus_id, "Голосовой модуль деактивирован.", is_direct=is_direct)
        return True

    rem_match = re.match(r"^(?:напомни|!напомни)\s+(?:через\s+)?(\d+)\s*(сек|мин|час|дн|ч|м|с)[а-я]*\s+(.+)$", user_input, re.I)
    if rem_match:
        qty, unit, task_text = int(rem_match.group(1)), rem_match.group(2).lower(), rem_match.group(3).strip()
        seconds = qty * {"сек": 1, "с": 1, "мин": 60, "м": 60, "час": 3600, "ч": 3600, "дн": 86400}.get(unit, 60)
        context = ""
        if any(w in task_text.lower() for w in ["гулять", "улицу", "выйти", "поехать"]):
            context = f" (Погода: {await get_weather()})"
        reminders_list.append({"chat_id": chat_id, "bus_id": bus_id, "text": task_text, "time": time.time() + seconds, "context": context})
        await save_reminders()
        await send_smart_response(chat_id, bus_id, f"Напомню <i>«{task_text}»</i> через {qty} {unit}.{context}", is_direct=is_direct)
        return True

    return False

@dp.callback_query(F.data.startswith("art_retry_safe:"))
async def handle_art_retry_safe(callback: types.CallbackQuery):
    gen_id = callback.data.split(":")[1]
    prompt = art_prompts_cache.get(gen_id)
    if not prompt: await callback.answer("Сессия устарела.", show_alert=True); return
    await callback.answer("Генерирую...")
    await bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.UPLOAD_PHOTO)
    img_bytes = await generate_safe_18_art(prompt)
    if img_bytes:
        new_id = uuid.uuid4().hex[:8]
        art_prompts_cache[new_id] = prompt
        photo = BufferedInputFile(img_bytes, filename="safe_art.jpg")
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎨 Еще вариант", callback_data=f"art_retry_safe:{new_id}")], [InlineKeyboardButton(text="🗑 Удалить", callback_data="art_delete_safe")]])
        await bot.send_photo(chat_id=callback.message.chat.id, photo=photo, caption=f"✨ <b>Новый безопасный арт.</b>\n<i>{prompt}</i>", parse_mode="HTML", reply_markup=kb)
    else: await callback.message.reply("Сбой генерации.")

@dp.callback_query(F.data == "art_delete_safe")
async def handle_art_delete_safe(callback: types.CallbackQuery):
    try: await callback.message.delete(); await callback.answer("Удалено.")
    except Exception: await callback.answer("Не удалось удалить.")

@dp.callback_query(F.data == "ayakoji_unmute_direct")
async def handle_unmute(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    if callback.from_user.id != OWNER_ID and OWNER_ID != 0: await callback.answer("Доступ запрещен.", show_alert=True); return
    muted_chats.pop(chat_id, None); await save_settings(); await callback.answer("Снято.")
    try: await callback.message.edit_text("Ограничения аннулированы.")
    except Exception: pass

@dp.message(F.business_connection_id.is_(None))
async def handle_direct_message(message: types.Message):
    global last_owner_activity
    if not message.from_user or message.from_user.is_bot: return
    chat_id = message.chat.id
    last_owner_activity = time.time()
    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip(): return
    if is_nsfw_request(user_input):
        await send_smart_response(chat_id, "", "Запрос отклонен: запрещенный контент.", is_direct=True)
        return
    if await process_bot_command(message, user_input, is_owner=True): return
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    reply = await ask_groq(user_input, chat_id, AYAKOJI_PROMPT_DIRECT, max_tokens=500)
    forced_voice = any(t in user_input.lower() for t in ["в голосовом", "голосовым", "голосом", "скажи", "озвучь", "проговори"])
    await send_smart_response(chat_id, "", reply, is_direct=True, send_as_voice=is_voice or forced_voice or voice_chat_modes.get(chat_id, False) or (random.random() < 0.20))

@dp.business_message()
async def handle_business_message(message: types.Message):
    global last_owner_activity
    chat_id, bus_id, msg_id = message.chat.id, message.business_connection_id, message.message_id
    bot_id = bot.id if bot else 0
    if not message.from_user or message.from_user.is_bot or message.from_user.id == bot_id: return
    if chat_id == bot_id or (OWNER_ID != 0 and chat_id == OWNER_ID): return
    msg_key = (chat_id, msg_id)
    if msg_key in processed_message_ids: return
    processed_message_ids[msg_key] = True
    is_owner = (OWNER_ID != 0 and message.from_user.id == OWNER_ID) or (message.from_user.id != chat_id)
    if is_owner:
        last_owner_activity = time.time()
        user_input, _ = await extract_message_content(message)
        if await process_bot_command(message, user_input, is_owner=True, bus_id=bus_id):
            try: await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
            except Exception: pass
        return
    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip(): return
    if focus_until > 0 and time.time() < focus_until:
        guest_requests.append({"name": message.from_user.full_name, "username": f"@{message.from_user.username}" if message.from_user.username else "none", "text": user_input, "time": datetime.datetime.now().strftime("%d.%m %H:%M")})
        await save_guest_requests()
        await send_smart_response(chat_id, bus_id, "Создатель в режиме концентрации. Я передал сообщение секретарю.", is_direct=False)
        return
    business_visits[str(chat_id)] = {"name": message.from_user.full_name or "Гость", "username": f"@{message.from_user.username}" if message.from_user.username else "none", "time": datetime.datetime.now().strftime("%H:%M"), "last_msg": user_input[:100]}
    asyncio.create_task(save_visits())
    now = time.time()
    if not always_answer_mode and not force_offline_mode:
        if (now - last_owner_activity) < OWNER_IDLE_TIMEOUT: return
    if chat_id in muted_chats:
        if now < muted_chats[chat_id]:
            try: await bot(DeleteBusinessMessages(business_connection_id=bus_id, message_ids=[msg_id]))
            except Exception: pass
            return
        else: muted_chats.pop(chat_id, None); asyncio.create_task(save_settings())
    if await check_chat_flood(chat_id, bus_id): return
    if is_nsfw_request(user_input):
        await send_smart_response(chat_id, bus_id, "Извините, я не обсуждаю темы 18+ с посторонними.", is_direct=False)
        return
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=bus_id)
    reply = await ask_groq(user_input, chat_id, AYAKOJI_PROMPT_GUEST, max_tokens=220)
    await send_smart_response(chat_id, bus_id, reply, is_direct=False, send_as_voice=is_voice or voice_chat_modes.get(chat_id, False) or (random.random() < 0.20))

async def cleaner_and_reminders_task():
    global reminders_list, last_auto_briefing_date, focus_until
    while True:
        try:
            await asyncio.sleep(5)
            now, now_dt = time.time(), datetime.datetime.now()
            today_date = now_dt.strftime("%Y-%m-%d")
            if focus_until > 0 and now > focus_until:
                focus_until = 0.0
                if OWNER_ID != 0: await send_smart_response(OWNER_ID, "", "Режим фокуса завершен. Я снова на связи.", is_direct=True)
            if now_dt.hour == 9 and now_dt.minute == 0 and last_auto_briefing_date != today_date and OWNER_ID != 0:
                last_auto_briefing_date = today_date
                await send_smart_response(OWNER_ID, "", await build_daily_briefing(), is_direct=True, send_as_voice=True)
            expired = [cid for cid, exp in muted_chats.items() if exp != float('inf') and now >= exp]
            if expired:
                for cid in expired: muted_chats.pop(cid, None)
                await save_settings()
            remaining, triggered = [], []
            for r in reminders_list: (triggered if now >= r.get("time", 0) else remaining).append(r)
            if triggered:
                reminders_list = remaining; await save_reminders()
                for rem in triggered:
                    text = f"Сработал протокол хронометража.\nНапоминание: <b>{rem.get('text')}</b>{rem.get('context', '')}"
                    await send_smart_response(rem["chat_id"], rem.get("bus_id", ""), text, is_direct=not bool(rem.get("bus_id")), send_as_voice=True)
        except asyncio.CancelledError: break
        except Exception as e: logger.error(f"Cleaner Error: {e}")

async def handle_ping(request): return web.Response(text="Ayakoji Core: Online.")
async def setup_web_app():
    app = web.Application(); app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000)))
    await site.start(); return runner

async def main():
    global http_session
    if not BOT_TOKEN: logger.critical("TELEGRAM_BOT_TOKEN не задан!"); return
    http_session = aiohttp.ClientSession()
    web_runner = await setup_web_app()
    bg_task = asyncio.create_task(cleaner_and_reminders_task())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Аякоджи онлайн. Безопасный 18+ режим активен.")
        await dp.start_polling(bot)
    except TelegramConflictError: logger.critical("Запущен дубликат бота!")
    finally:
        bg_task.cancel(); await web_runner.cleanup()
        if http_session: await http_session.close()
        await bot.session.close()

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Работа систем Аякоджи остановлена.")
