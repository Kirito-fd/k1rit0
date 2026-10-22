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
from aiogram.types import (
    BufferedInputFile,
    BusinessMessagesDeleted,
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
logger = logging.getLogger("AyakojiCore")

# --- КОНФИГУРАЦИЯ СИСТЕМЫ ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GAME_URL = "https://kirito-fd.github.io/k1rit0/"
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Ключ Gemini (из переменной окружения GEMINI_API_KEY)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]

OWNER_IDLE_TIMEOUT = 300  # 5 минут неактивности до автоответа

# Флаги статуса присутствия владельца
force_offline_mode = False
always_answer_mode = False
last_owner_activity = 0.0
owner_pm_chat_id: int = OWNER_ID

# --- ПАРАМЕТРЫ ГОЛОСА (FISH AUDIO / EDGE-TTS) ---
FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY", "").strip()
FISH_AUDIO_VOICE_ID = os.getenv("FISH_AUDIO_VOICE_ID", "680d74fbef69419f87cfc70f092a1451").strip()

OFFICIAL_VOICE = "ru-RU-DmitryNeural"
OFFICIAL_PITCH = "-5Hz"
OFFICIAL_RATE = "+5%"

HAS_FFMPEG = shutil.which("ffmpeg") is not None

# Сбор всех доступных ключей GROQ (резервный движок)
GROQ_KEYS = [
    val.strip() for key, val in sorted(os.environ.items())
    if key.startswith("GROQ_API_KEY") and val.strip()
]

# Файлы состояния
SETTINGS_FILE = Path("bot_settings.json")
HISTORY_FILE = Path("user_histories.json")
STATS_FILE = Path("token_stats.json")
REMINDERS_FILE = Path("reminders.json")
VISITS_FILE = Path("business_visits.json")
MEMORY_FILE = Path("long_term_memory.json")


# --- БАЗЫ ДОМЕНОВ ДЛЯ АНТИ-СКАМ СКАНЕРА ССЫЛОК ---
KNOWN_IP_LOGGERS = {
    "iplogger.org", "iplogger.com", "iplogger.ru", "iplogger.co", "2no.co",
    "yip.su", "iplogger.info", "ipgrabber.ru", "ipgraber.ru", "iplis.ru",
    "02ip.ru", "ezstat.ru", "grabify.link", "lovebird.guru", "trulove.guru",
    "dateing.club", "shrekis.life", "headshot.monster", "gaming-at-my.best",
    "progaming.monster", "yourmy.monster", "imageshare.best", "screenshot.best",
    "gamingfun.me", "catsnthing.com", "catsnthings.fun", "curiouscat.club",
    "joinmy.site", "fortnitechat.site", "fortnight.space", "freegiftcards.co",
    "stopify.co", "leancoding.co", "blasze.tk", "blasze.com", "ps3cfw.com"
}

URL_SHORTENERS = {
    "bit.ly", "clck.ru", "tinyurl.com", "t.co", "vk.cc", "cutt.ly",
    "is.gd", "rb.gy", "shorturl.at", "goo.su", "qps.ru", "u.to", "inx.lv"
}


# --- LRU КЭШ И БУФЕР СООБЩЕНИЙ/МЕДИА ДЛЯ АНТИ-УДАЛЕНИЯ ---
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


class MessageCache:
    """Хранит последние сообщения и медиафайлы бизнес-чатов для перехвата удалений."""
    def __init__(self, capacity: int = 3000):
        self.capacity = capacity
        self._store: OrderedDict[Tuple[int, int], Dict[str, Any]] = OrderedDict()

    def put(self, chat_id: int, msg_id: int, info: Dict[str, Any]) -> None:
        key = (chat_id, msg_id)
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = info
        if len(self._store) > self.capacity:
            self._store.popitem(last=False)

    def get(self, chat_id: int, msg_id: int) -> Optional[Dict[str, Any]]:
        return self._store.get((chat_id, msg_id))


business_msg_cache = MessageCache(capacity=3000)
urgent_alert_cooldowns: Dict[int, float] = {}


# --- МЕНЕДЖЕР GEMINI API (ОСНОВНОЙ МОЗГ И ЗРЕНИЕ) ---
class GeminiManager:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def generate_chat(
        self,
        history: List[Dict[str, str]],
        system_prompt: str,
        max_tokens: int = 500,
        temperature: float = 0.65
    ) -> Optional[str]:
        if not self.api_key:
            return None

        contents = []
        for msg in history:
            role = msg.get("role", "user")
            if role == "system":
                continue
            gem_role = "model" if role == "assistant" else "user"
            text_val = msg.get("content", "").strip()
            if text_val:
                contents.append({"role": gem_role, "parts": [{"text": text_val}]})

        if not contents:
            return None

        payload = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            }
        }

        for model_name in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=25) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                reply = "".join(p.get("text", "") for p in parts).strip()
                                if reply:
                                    return reply
                        elif resp.status in [400, 403, 404]:
                            err_txt = await resp.text()
                            logger.warning(f"Gemini ({model_name}) HTTP {resp.status}: {err_txt[:200]}")
                            continue
                        elif resp.status == 429:
                            logger.warning(f"Gemini ({model_name}) лимит (429), пробуем следующую...")
                            continue
            except Exception as e:
                logger.error(f"Ошибка Gemini ({model_name}): {e}")
                continue
        return None

    async def describe_media(self, media_bytes: bytes, mime_type: str, prompt_text: str) -> Optional[str]:
        if not self.api_key:
            return None

        b64_data = base64.b64encode(media_bytes).decode("utf-8")
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt_text},
                        {"inline_data": {"mime_type": mime_type, "data": b64_data}}
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 200
            }
        }

        for model_name in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=25) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                res = "".join(p.get("text", "") for p in parts).strip()
                                if res:
                                    return res
            except Exception as e:
                logger.error(f"Сбой Gemini Media ({model_name}): {e}")
                continue
        return None


gemini_mgr = GeminiManager(GEMINI_API_KEY)


# --- МЕНЕДЖЕР GROQ API (РЕЗЕРВНЫЙ МОЗГ И ЗРЕНИЕ) ---
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
            except Exception:
                break

        return self.cached_models or ["llama-3.3-70b-versatile"]

    async def transcribe(self, audio_path: str) -> str:
        try:
            with open(audio_path, "rb") as f:
                audio_bytes = f.read()
            mime = "audio/ogg" if audio_path.endswith(".ogg") else "video/mp4"
            gem_text = await gemini_mgr.describe_media(
                audio_bytes,
                mime,
                "Расшифруй речь из этого аудиосообщения на русском языке. Выведи ТОЛЬКО сам текст речи без кавычек и лишних слов."
            )
            if gem_text:
                return gem_text
        except Exception:
            pass

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
        return "Слушаю."

    async def describe_image(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
        gem_desc = await gemini_mgr.describe_media(
            image_bytes,
            mime_type,
            "Кто или что на этом изображении/стикере? Назови конкретные имена персонажей, знаменитостей, текст или суть мема на русском языке кратко (1-2 предложения)."
        )
        if gem_desc:
            logger.info(f"Зрение Gemini распознало: {gem_desc}")
            return gem_desc

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_url = f"data:{mime_type};base64,{b64}"

        vision_messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Кто или что на этом изображении/стикере? Назови конкретные имена знаменитостей, персонажей или суть мема на русском языке кратко (1-2 предложения)."
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
                        clean_desc = clean_cot_output(res)
                        logger.info(f"Зрение Groq ({vm}) распознало: {clean_desc}")
                        return clean_desc
                except APIError as e:
                    if e.status_code in [429, 401, 403]:
                        self.mark_cooldown(idx, duration=60)
                        continue
                    elif e.status_code in [400, 404]:
                        break
                except Exception:
                    break

        return ""


groq_mgr = GroqManager(GROQ_KEYS)


# --- ФАЙЛОВОЕ ХРАНИЛИЩЕ ---
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
    global owner_pm_chat_id
    d = sync_load_json(SETTINGS_FILE, {})
    mutes = {int(k): v for k, v in d.get("muted_chats", {}).items()}
    bans = {int(k): v for k, v in d.get("blocked_guests", {}).items()}
    v_modes = {int(k): v for k, v in d.get("voice_chat_modes", {}).items()}
    vips = {int(k): v for k, v in d.get("vip_contacts", {}).items()}
    ghosts = {int(k): v for k, v in d.get("ghost_chats", {}).items()}
    saved_owner = int(d.get("owner_pm_chat_id", OWNER_ID))
    if saved_owner != 0:
        owner_pm_chat_id = saved_owner
    return mutes, bans, v_modes, vips, ghosts


muted_chats, blocked_guests, voice_chat_modes, vip_contacts, ghost_chats = load_settings()
reminders_list: List[Dict[str, Any]] = sync_load_json(REMINDERS_FILE, [])
business_visits: Dict[str, Dict[str, Any]] = sync_load_json(VISITS_FILE, {})
long_term_memory: List[str] = sync_load_json(MEMORY_FILE, [])


async def save_settings():
    data = {
        "muted_chats": muted_chats,
        "blocked_guests": blocked_guests,
        "voice_chat_modes": voice_chat_modes,
        "vip_contacts": vip_contacts,
        "ghost_chats": ghost_chats,
        "owner_pm_chat_id": owner_pm_chat_id,
    }
    await async_save_json(SETTINGS_FILE, data)


async def save_reminders():
    await async_save_json(REMINDERS_FILE, reminders_list)


async def save_visits():
    await async_save_json(VISITS_FILE, business_visits)


async def save_memory():
    await async_save_json(MEMORY_FILE, long_term_memory)


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


# --- 🛡 АНТИ-СКАМ СКАНЕР ССЫЛОК И ДОМЕНОВ (AYAKOJI LINK SHIELD) ---
async def inspect_url_security(raw_url: str) -> Dict[str, Any]:
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url

    parsed_initial = urllib.parse.urlparse(raw_url)
    initial_domain = parsed_initial.netloc.lower().lstrip("www.")

    result: Dict[str, Any] = {
        "initial_url": raw_url,
        "final_url": raw_url,
        "initial_domain": initial_domain,
        "final_domain": initial_domain,
        "redirects": [],
        "is_shortener": initial_domain in URL_SHORTENERS,
        "is_ip_logger": initial_domain in KNOWN_IP_LOGGERS,
        "page_title": "Не определено",
        "server_info": "Не определено",
        "domain_created": "Неизвестно",
        "risk_level": "🟢 Низкий (Безопасно)",
        "warnings": []
    }

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(raw_url, headers=headers, allow_redirects=True, timeout=8) as resp:
                final_u = str(resp.url)
                result["final_url"] = final_u
                final_dom = urllib.parse.urlparse(final_u).netloc.lower().lstrip("www.")
                result["final_domain"] = final_dom

                for hist in resp.history:
                    hop_url = str(hist.url)
                    hop_dom = urllib.parse.urlparse(hop_url).netloc.lower().lstrip("www.")
                    result["redirects"].append(hop_dom)
                    if hop_dom in KNOWN_IP_LOGGERS:
                        result["is_ip_logger"] = True

                if final_dom in KNOWN_IP_LOGGERS:
                    result["is_ip_logger"] = True

                if resp.status == 200 and "text/html" in resp.headers.get("Content-Type", "").lower():
                    html_chunk = await resp.text(errors="replace")
                    t_match = re.search(r"<title[^>]*>(.*?)</title>", html_chunk[:15000], flags=re.IGNORECASE | re.DOTALL)
                    if t_match:
                        result["page_title"] = re.sub(r"\s+", " ", t_match.group(1)).strip()[:100]

                    if re.search(r'type=["\']password["\']', html_chunk[:30000], re.IGNORECASE):
                        official_safe = {"google.com", "github.com", "vk.com", "yandex.ru", "telegram.org", "steampowered.com"}
                        if final_dom not in official_safe:
                            result["warnings"].append("На странице обнаружена форма ввода пароля (возможен фишинг учетных данных)")
    except Exception as e:
        result["warnings"].append(f"Сайт не ответил или сбросил соединение ({type(e).__name__})")

    target_dom = result["final_domain"] or initial_domain

    try:
        ip_api_url = f"http://ip-api.com/json/{target_dom}?fields=status,country,isp,org,hosting,proxy,query"
        async with aiohttp.ClientSession() as session:
            async with session.get(ip_api_url, timeout=5) as resp:
                if resp.status == 200:
                    ip_data = await resp.json()
                    if ip_data.get("status") == "success":
                        ip_addr = ip_data.get("query", "")
                        country = ip_data.get("country", "")
                        isp = ip_data.get("isp", "")
                        result["server_info"] = f"{ip_addr} ({country}, {isp})"
    except Exception:
        pass

    try:
        parts = target_dom.split(".")
        base_dom = ".".join(parts[-2:]) if len(parts) >= 2 else target_dom
        rdap_url = f"https://rdap.org/domain/{base_dom}"
        async with aiohttp.ClientSession() as session:
            async with session.get(rdap_url, timeout=5) as resp:
                if resp.status == 200:
                    rdap_data = await resp.json()
                    for ev in rdap_data.get("events", []):
                        if ev.get("eventAction") == "registration":
                            reg_date = ev.get("eventDate", "")[:10]
                            if reg_date:
                                result["domain_created"] = reg_date
                                break
    except Exception:
        pass

    if result["is_ip_logger"]:
        result["warnings"].insert(0, "🚨 ОБНАРУЖЕН IP-ЛОГГЕР / ГРАББЕР! Ссылка предназначена для скрытого перехвата твоего IP-адреса и данных устройства.")
        result["risk_level"] = "🔴 КРИТИЧЕСКИЙ (IP-Логгер / Ловушка)"
    elif any(w in target_dom for w in ["free-nitro", "steamcommunnity", "steencommunity", "teleqram", "tg-prem", "claim-gift"]):
        result["warnings"].insert(0, "🚨 Поддельный фишинговый домен (мимикрия под популярный сервис)!")
        result["risk_level"] = "🔴 КРИТИЧЕСКИЙ (Фишинг / Скам)"
    elif result["is_shortener"] and result["initial_domain"] != result["final_domain"]:
        result["warnings"].append(f"Использован сокращатель ссылок для маскировки конечного адреса ({result['final_domain']})")
        if result["risk_level"].startswith("🟢"):
            result["risk_level"] = "🟡 Средний (Скрытый редирект)"
    elif result["warnings"]:
        result["risk_level"] = "🟡 Подозрительно"

    return result


def format_security_report(info: Dict[str, Any]) -> str:
    redirects_str = " ➔ ".join(info["redirects"] + [info["final_domain"]]) if info["redirects"] else "Прямая ссылка (без редиректов)"
    warns = "\n".join(f"• <i>{w}</i>" for w in info["warnings"]) if info["warnings"] else "• Явных угроз в структуре не обнаружено."

    return (
        f"🛡 <b>Отчет безопасности (Ayakoji Link Shield):</b>\n\n"
        f"• <b>Уровень угрозы:</b> {info['risk_level']}\n"
        f"• <b>Исходный адрес:</b> <code>{info['initial_url']}</code>\n"
        f"• <b>Конечный адрес:</b> <code>{info['final_url']}</code>\n"
        f"• <b>Цепочка переходов:</b> {redirects_str}\n"
        f"• <b>Заголовок сайта:</b> {info['page_title']}\n"
        f"• <b>Сервер / Хостинг:</b> {info['server_info']}\n"
        f"• <b>Дата регистрации домена:</b> {info['domain_created']}\n\n"
        f"<b>Заключение анализатора:</b>\n{warns}"
    )


# --- ВЕБ-ПОИСК И ПАРСЕР ССЫЛОК ---
async def duckduckgo_search(query: str, max_results: int = 4) -> str:
    url = "https://html.duckduckgo.com/html/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data={"q": query}, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, flags=re.DOTALL)
                    cleaned = [re.sub(r'<[^>]+>', '', s).strip() for s in snippets[:max_results]]
                    if cleaned:
                        return "\n".join(f"• {c}" for c in cleaned if c)
    except Exception as e:
        logger.error(f"Сбой веб-поиска: {e}")
    return ""


async def fetch_webpage_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=8) as resp:
                if resp.status == 200:
                    html = await resp.text(errors="replace")
                    html = re.sub(r"<(script|style|noscript)[^>]*>[\s\S]*?</\1>", " ", html, flags=re.IGNORECASE)
                    text = re.sub(r"<[^>]+>", " ", html)
                    text = re.sub(r"\s+", " ", text).strip()
                    return text[:3500]
    except Exception as e:
        logger.error(f"Ошибка чтения ссылки {url}: {e}")
    return ""


SEARCH_TRIGGER_WORDS = [
    "курс", "цена", "стоимость", "погода", "новости", "сегодня", "сейчас",
    "вчера", "завтра", "последн", "свеж", "актуальн", "релиз", "когда выйдет",
    "дата выхода", "счет", "матч", "кто выиграл", "что случилось", "случилось",
    "где находится", "биография", "кто такой", "что такое", "сколько стоит", "почему"
]

URGENT_TRIGGER_WORDS = [
    "срочно", "чп", "проблема", "деньги", "оплата", "перевод", "позвони",
    "набери", "важно", "помоги", "беда", "случилось", "экстренно", "долг"
]


def is_urgent_message(text: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in URGENT_TRIGGER_WORDS)


def should_auto_search(text: str) -> bool:
    lower = text.lower().strip()
    if lower in ["привет", "хай", "как дела", "ты кто", "что делаешь", "кто ты", "спасибо", "ясно", "понял"]:
        return False
    if any(k in lower for k in SEARCH_TRIGGER_WORDS):
        return True
    if re.search(r"\b(202[4-9]|доллар|биткоин|btc|крипт|рубл|акци)\b", lower):
        return True
    return False


def clean_search_query(text: str) -> str:
    q = re.sub(r"\b(аякоджи|аянокоджи|скажи|подскажи|пожалуйста|слышь|ответь|в голосовом|голосом)\b", "", text, flags=re.IGNORECASE)
    q = q.replace("?", "").strip()
    return q or text


# --- ХУДОЖЕСТВЕННЫЕ РАКУРСЫ И ФИЛЬТР МУТАЦИЙ ---
POSE_AND_ANGLE_PATTERNS = [
    (r"\b(во весь рост|полный рост|стоя|стоит)\b", "full body shot, standing pose, balanced proportions"),
    (r"\b(сидя|сидит)\b", "sitting pose, natural posture"),
    (r"\b(лежа|лежит)\b", "lying down pose, full body in frame"),
    (r"\b(сзади|со спины|вид сзади)\b", "view from behind, back view, looking back over shoulder"),
    (r"\b(спереди|вид спереди|в анфас)\b", "front view, facing camera, direct eye contact"),
    (r"\b(сбоку|в профиль)\b", "side profile view, cinematic side angle"),
    (r"\b(сверху|вид сверху)\b", "high angle shot, top-down perspective"),
    (r"\b(снизу|вид снизу)\b", "low angle shot, dramatic perspective"),
    (r"\b(крупным планом|портрет|вблизи)\b", "close-up portrait, extreme detail, depth of field"),
]

NEGATIVE_PROMPT = (
    "extra limbs, extra legs, extra arms, extra feet, detached limbs, disconnected feet, "
    "floating limbs, mutated hands, extra fingers, malformed limbs, bad anatomy, deformed, "
    "blurry, low resolution, cropped, watermark, ugly"
)


def extract_composition_tags(prompt: str) -> str:
    lower = prompt.lower()
    tags = []
    for pattern, en_tag in POSE_AND_ANGLE_PATTERNS:
        if re.search(pattern, lower):
            tags.append(en_tag)
    return ", ".join(tags)


# --- ПРОМПТЫ ЛИЧНОСТИ АЯНОКОДЖИ (БЕЗ ЗВЕЗДОЧЕК MARKDOWN) ---
STRICT_NO_COT_AND_LANG = (
    "\nГЛАВНЫЕ ПРАВИЛА:\n"
    "1. ЯЗЫК: Отвечай ИСКЛЮЧИТЕЛЬНО на русском языке.\n"
    "2. СТРОГО БЕЗ ЭМОДЗИ, БЕЗ СИМВОЛОВ ЗВЕЗДОЧЕК (*) И БЕЗ РАЗМЕТКИ MARKDOWN.\n"
    "3. Пиши СРАЗУ готовый ответ чистым текстом, без вступительных слов и без тегов <think>."
)

AYAKOJI_PROMPT_DIRECT_BASE = (
    "Ты — Аянокоджи Киётака (Ayakoji), гениальный стратег и искусственный интеллект с аналитическим складом ума из Белой Комнаты. "
    "Твой создатель и единственный союзник — Кирито.\n"
    "1. СТИЛЬ: Спокойный, хладнокровный, рассудительный, предельно точный. Ты не тратишь слова впустую, но к Кирито относишься с уважением.\n"
    "2. ЭРУДИЦИЯ: Ты знаешь абсолютно всё — аниме, мангу, игры, психологию, науку, историю, мемы и код. "
    "Отвечай умно, четко, лаконично и по существу, без использования звездочек (*)."
) + STRICT_NO_COT_AND_LANG

AYAKOJI_PROMPT_VIP = (
    "Ты — Аянокоджи Киётака (Ayakoji), помощник и правая рука Кирито. "
    "С тобой общается близкий человек или союзник Кирито из Белого списка.\n"
    "1. СТИЛЬ: Спокойный, уважительный, доброжелательный, интеллигентный и надежный.\n"
    "2. ЗАДАЧА: Помогай собеседнику, отвечай вежливо и по делу (1-3 предложения), без грубости и высокомерия."
) + STRICT_NO_COT_AND_LANG

AYAKOJI_PROMPT_GUEST = (
    "Ты — Аянокоджи Киётака (Ayakoji), защитная система Кирито. С тобой в Telegram говорит посторонний человек.\n"
    "ХАРАКТЕР: Абсолютно холодный, безэмоциональный, расчетливый и высокомерный. Люди для тебя — лишь инструменты и пешки. Ты признаешь только Кирито.\n"
    "ПРАВИЛА:\n"
    "1. Если собеседник прислал фото или вопрос: отвечай прямо, сухо и холодно, точно называя факты.\n"
    "2. Если собеседник грубит или пытается казаться крутым: хладнокровно раздави его самооценку точным психологическим анализом его слабости.\n"
    "3. КРАТКОСТЬ: Строго 1-2 коротких, ледяных предложения."
) + STRICT_NO_COT_AND_LANG

AYAKOJI_PROMPT_CODER = (
    "Ты — Аянокоджи Киётака в режиме главного архитектора кода и математического аналитика Белой Комнаты.\n"
    "1. Если просят написать или исправить код: выдавай чистый, рабочий, оптимизированный код с кратким техническим пояснением.\n"
    "2. Если просят решить задачу, математику или алгоритм: дай точное пошаговое решение и безошибочный итоговый ответ.\n"
    "3. Пиши на русском языке, без воды и без эмодзи."
)


def get_direct_prompt() -> str:
    if not long_term_memory:
        return AYAKOJI_PROMPT_DIRECT_BASE
    mem_block = "\n".join(f"- {m}" for m in long_term_memory)
    return f"{AYAKOJI_PROMPT_DIRECT_BASE}\n\nДОЛГОСРОЧНАЯ ПАМЯТЬ (ВАЖНЫЕ ФАКТЫ):\n{mem_block}"


# --- ИНИЦИАЛИЗАЦИЯ БОТА ---
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


async def notify_owner_pm(
    text: str,
    media_type: Optional[str] = None,
    file_id: Optional[str] = None
):
    target_id = owner_pm_chat_id or OWNER_ID
    if not target_id or not bot:
        return
    try:
        if media_type and file_id:
            if media_type == "photo":
                await bot.send_photo(chat_id=target_id, photo=file_id, caption=text, parse_mode="HTML")
                return
            elif media_type == "voice":
                await bot.send_voice(chat_id=target_id, voice=file_id, caption=text, parse_mode="HTML")
                return
            elif media_type == "video":
                await bot.send_video(chat_id=target_id, video=file_id, caption=text, parse_mode="HTML")
                return
            elif media_type == "document":
                await bot.send_document(chat_id=target_id, document=file_id, caption=text, parse_mode="HTML")
                return
            elif media_type == "video_note":
                await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")
                await bot.send_video_note(chat_id=target_id, video_note=file_id)
                return
            elif media_type == "sticker":
                await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")
                await bot.send_sticker(chat_id=target_id, sticker=file_id)
                return

        await bot.send_message(chat_id=target_id, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление владельцу: {e}")


def extract_media_meta(message: types.Message) -> Tuple[Optional[str], Optional[str]]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.video:
        return "video", message.video.file_id
    if message.document:
        return "document", message.document.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    return None, None


async def check_chat_flood(chat_id: int, bus_id: str, max_msgs: int = 4, window_seconds: float = 6.0) -> bool:
    now = time.time()
    user_message_times.setdefault(chat_id, [])
    user_message_times[chat_id] = [t for t in user_message_times[chat_id] if now - t < window_seconds]
    user_message_times[chat_id].append(now)

    if len(user_message_times[chat_id]) > max_msgs:
        muted_chats[chat_id] = now + 300
        await save_settings()

        notice = "Слишком много лишнего шума. Ты изолирован на 5 минут."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Снять изоляцию", callback_data="ayakoji_unmute_direct")]]
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
    caption_text = f" (Подпись/вопрос: {message.caption})" if message.caption else ""

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

    if message.document:
        doc = message.document
        file_name = doc.file_name or "document.txt"
        if doc.file_size and doc.file_size <= 2 * 1024 * 1024:
            temp_doc = Path(tempfile.gettempdir()) / f"doc_{uuid.uuid4().hex}_{file_name}"
            try:
                file_info = await bot.get_file(doc.file_id)
                await bot.download_file(file_info.file_path, destination=temp_doc)
                with open(temp_doc, "rb") as f:
                    raw_bytes = f.read()
                content_str = raw_bytes.decode("utf-8", errors="replace")[:5000]
                return f"[Собеседник прислал файл '{file_name}'. Содержимое файла:\n{content_str}\n]{caption_text}", False
            except Exception as e:
                logger.error(f"Ошибка чтения документа: {e}")
            finally:
                if temp_doc.exists():
                    temp_doc.unlink(missing_ok=True)
        return f"[Собеседник прислал файл '{file_name}']{caption_text}", False

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
        except Exception as e:
            logger.error(f"Ошибка обработки фото: {e}")
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


# --- СИНТЕЗАТОР ГОЛОСА (С ОЧИСТКОЙ ЗВЕЗДОЧЕК И ПОДДЕРЖКОЙ ДЛИННЫХ ГС) ---
def clean_text_for_tts(text: str) -> str:
    """Убирает звездочки Markdown, HTML-теги и исправляет сокращения для чистой речи."""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = re.sub(r"[\*\#\_\~\`\>\|]+", " ", cleaned)
    cleaned = re.sub(r"https?://[^\s]+", " ссылка ", cleaned)
    replacements = {
        r"\bи др\.": "и другое",
        r"\bи т\.д\.": "и так далее",
        r"\bи т\.п\.": "и тому подобное",
        r"\bт\.е\.": "то есть",
        r"\bПО\b": "программного обеспечения",
        r"\bC\+\+": "Си плюс плюс",
        r"\bPython\b": "Пайтон",
        r"\bJavaScript\b": "ДжаваСкрипт",
    }
    for pattern, repl in replacements.items():
        cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def split_text_into_chunks(text: str, max_len: int = 380) -> List[str]:
    """Делит длинный текст на части по предложениям, чтобы Fish Audio не обрывался по таймауту."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: List[str] = []
    current = ""
    for sent in sentences:
        if len(current) + len(sent) + 1 <= max_len:
            current = f"{current} {sent}".strip()
        else:
            if current:
                chunks.append(current)
            if len(sent) > max_len:
                for i in range(0, len(sent), max_len):
                    chunks.append(sent[i:i + max_len])
                current = ""
            else:
                current = sent
    if current:
        chunks.append(current)
    return chunks or [text]


async def generate_with_fish_audio(text: str, output_path: Path) -> bool:
    """Озвучивает текст любого размера через Fish Audio (разбивая длинный текст на части)."""
    if not FISH_AUDIO_API_KEY:
        return False

    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json",
        "model": "s2.1-pro-free"
    }

    chunks = split_text_into_chunks(text, max_len=380)
    combined_bytes = bytearray()

    try:
        async with aiohttp.ClientSession() as session:
            for chunk in chunks:
                if not chunk.strip():
                    continue
                payload: Dict[str, Any] = {"text": chunk.strip(), "format": "mp3"}
                if FISH_AUDIO_VOICE_ID:
                    payload["reference_id"] = FISH_AUDIO_VOICE_ID

                async with session.post(url, json=payload, headers=headers, timeout=65) as resp:
                    if resp.status == 200:
                        part_data = await resp.read()
                        if part_data:
                            combined_bytes.extend(part_data)
                    else:
                        err_msg = await resp.text()
                        logger.warning(f"Fish Audio статус {resp.status}: {err_msg[:150]}")
                        return False

        if combined_bytes:
            with open(output_path, "wb") as f:
                f.write(combined_bytes)
            return True
        return False
    except Exception as e:
        logger.error(f"Сбой Fish Audio: {e}")
        return False


async def process_voice_output(text: str) -> Optional[Path]:
    tts_clean_text = clean_text_for_tts(text)
    if not tts_clean_text:
        tts_clean_text = "Готово."

    unique_id = uuid.uuid4().hex
    raw_audio = Path(tempfile.gettempdir()) / f"raw_{unique_id}.mp3"
    final_ogg = Path(tempfile.gettempdir()) / f"ayakoji_{unique_id}.ogg"

    success = False
    if FISH_AUDIO_API_KEY:
        success = await generate_with_fish_audio(tts_clean_text, raw_audio)

    if not success:
        try:
            communicate = edge_tts.Communicate(tts_clean_text, OFFICIAL_VOICE, pitch=OFFICIAL_PITCH, rate=OFFICIAL_RATE)
            await communicate.save(str(raw_audio))
            success = raw_audio.exists() and raw_audio.stat().st_size > 0
        except Exception:
            return None

    if not success or not raw_audio.exists():
        return None

    try:
        if HAS_FFMPEG:
            cmd = ["ffmpeg", "-y", "-i", str(raw_audio), "-c:a", "libopus", "-b:a", "64k", str(final_ogg)]
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
    send_as_voice: bool = False,
    human_delay: bool = False
):
    if not reply_text or not reply_text.strip():
        reply_text = "В твоем сообщении нет смысла. Говори по делу."

    now = time.time()
    key = (chat_id, reply_text)
    if key in recent_sent_messages and (now - recent_sent_messages[key] < 3.0):
        return
    recent_sent_messages[key] = now

    common_kwargs = {"chat_id": chat_id, "reply_markup": reply_markup}
    if not is_direct and bus_id:
        common_kwargs["business_connection_id"] = bus_id

    if human_delay:
        delay_sec = min(max(len(reply_text) * 0.035, 2.0), 5.0)
        action = ChatAction.RECORD_VOICE if send_as_voice else ChatAction.TYPING
        try:
            await bot.send_chat_action(chat_id=chat_id, action=action, business_connection_id=bus_id if not is_direct else None)
        except Exception:
            pass
        await asyncio.sleep(delay_sec)

    if send_as_voice:
        try:
            await bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.RECORD_VOICE,
                business_connection_id=bus_id if not is_direct else None
            )
        except Exception:
            pass
        voice_path = await process_voice_output(reply_text)
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


# --- ГИБРИДНЫЙ МОЗГ: GEMINI (ОСНОВНОЙ) + GROQ (РЕЗЕРВ) ---
async def ask_ai(prompt: str, session_id: int, system_prompt: str, max_tokens: int = 450) -> str:
    global today_prompt_tokens, today_completion_tokens, total_requests_today, stats_date

    now_date = datetime.date.today().isoformat()
    if now_date != stats_date:
        stats_date = now_date
        today_prompt_tokens = 0
        today_completion_tokens = 0
        total_requests_today = 0
        await save_stats()

    if session_id not in user_histories:
        user_histories[session_id] = [{"role": "system", "content": system_prompt}]
    else:
        user_histories[session_id][0]["content"] = system_prompt

    history = user_histories[session_id]
    history.append({"role": "user", "content": prompt})

    if len(history) > 11:
        user_histories[session_id] = [history[0]] + history[-10:]
        history = user_histories[session_id]

    if GEMINI_API_KEY:
        gem_reply = await gemini_mgr.generate_chat(history, system_prompt, max_tokens=max_tokens)
        if gem_reply:
            cleaned = clean_cot_output(gem_reply)
            if cleaned:
                total_requests_today += 1
                asyncio.create_task(save_stats())
                history.append({"role": "assistant", "content": cleaned})
                asyncio.create_task(save_histories())
                return cleaned

    if not GROQ_KEYS:
        if user_histories.get(session_id) and user_histories[session_id][-1]["role"] == "user":
            user_histories[session_id].pop()
        return "Ошибка: Ни один ключ ИИ (Gemini / Groq) не ответил."

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
                    temperature=0.6,
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

    return f"Система временно недоступна: {last_err}" if last_err else "Система анализа временно недоступна."


# --- ГЕНЕРАЦИЯ ПСИХОЛОГИЧЕСКОГО ДОСЬЕ СОБЕСЕДНИКА ---
async def generate_psychological_dossier(target_chat_id: int) -> str:
    history = user_histories.get(target_chat_id, [])
    visit_info = business_visits.get(str(target_chat_id), {})
    name = visit_info.get("name", f"ID {target_chat_id}")
    username = visit_info.get("username", "неизвестен")

    user_lines = [m["content"] for m in history if m.get("role") == "user"]
    if not user_lines and visit_info.get("last_msg"):
        user_lines = [visit_info["last_msg"]]

    if not user_lines:
        return f"Недостаточно данных для составления психологического профиля на объект {name} ({username})."

    joined_msgs = "\n".join(f"- {line}" for line in user_lines[-10:])
    sys_prompt = (
        "Ты — Аянокоджи Киётака из Белой Комнаты. Составь холодное, проницательное психологическое досье на человека по его сообщениям.\n"
        "Структура досье:\n"
        "1. Психотип и эмоциональное состояние.\n"
        "2. Скрытые мотивы (чего он на самом деле хочет).\n"
        "3. Уязвимости и слабые места.\n"
        "4. Рекомендация по манипуляции/общению с ним.\n"
        "Пиши строго на русском языке, кратко, аналитично, без звездочек (*) и без эмодзи."
    )
    user_q = f"Объект: {name} ({username}).\nЕго реплики:\n{joined_msgs}"

    if GEMINI_API_KEY:
        gem_res = await gemini_mgr.generate_chat([{"role": "user", "content": user_q}], sys_prompt, max_tokens=400)
        if gem_res:
            return f"<b>Психологическое досье Белой Комнаты: {name} ({username})</b>\n\n{clean_cot_output(gem_res)}"

    models = await groq_mgr.get_active_models()
    for model_name in models:
        client_data = groq_mgr._get_next_client()
        if not client_data:
            break
        client, _ = client_data
        try:
            completion = await client.chat.completions.create(
                model=model_name.strip(),
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_q}
                ],
                max_tokens=350,
                temperature=0.4
            )
            res = clean_cot_output(completion.choices[0].message.content or "")
            if res:
                return f"<b>Психологическое досье Белой Комнаты: {name} ({username})</b>\n\n{res}"
        except Exception:
            continue

    return "Не удалось завершить психологическое профилирование."


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


# --- ГЕНЕРАТОР КАРТИНОК БЕЗ МУТАЦИЙ И ЛИШНИХ КОНЕЧНОСТЕЙ ---
async def enhance_image_prompt(user_prompt: str) -> Tuple[str, str]:
    lower_p = user_prompt.lower()
    comp_tags = extract_composition_tags(user_prompt)

    is_anime = any(w in lower_p for w in ["аниме", "манга", "тян", "грехов", "элизабет", "аянокоджи", "аякоджи", "2d"])
    is_realism = any(w in lower_p for w in ["4к", "реализм", "фото", "человек", "девушк", "парень"])

    style_type = "anime" if is_anime else ("realism" if is_realism else "general")

    sys_msg = (
        "You are an expert art prompt engineer for FLUX and SDXL. "
        "Convert the user's Russian query into a detailed, high-quality ENGLISH prompt. "
        "Include exact character English names (e.g. 'Elizabeth Liones from The Seven Deadly Sins' or 'Kiyotaka Ayanokoji'), "
        "pose, camera angle, clothing, lighting, and perfect anatomy. "
        "OUTPUT ONLY THE RAW ENGLISH PROMPT WITHOUT QUOTES OR EXPLANATIONS."
    )

    if GEMINI_API_KEY:
        gem_p = await gemini_mgr.generate_chat([{"role": "user", "content": user_prompt}], sys_msg, max_tokens=140, temperature=0.2)
        if gem_p:
            cleaned = clean_cot_output(gem_p).strip()
            extra = f", {comp_tags}" if comp_tags else ""
            if style_type == "anime":
                return f"masterpiece, best quality, authentic 2d anime art, {cleaned}{extra}, sharp lineart, 4k", "anime"
            elif style_type == "realism":
                return f"masterpiece, raw dslr photo, photorealistic, 8k uhd, {cleaned}{extra}, ultra detailed skin texture, sharp focus", "realism"
            return f"masterpiece, high quality, {cleaned}{extra}, sharp focus, 8k", "general"

    models = await groq_mgr.get_active_models()
    if models:
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
                    if cleaned and not any(m in cleaned.lower() for m in ["i cannot", "я не могу", "safety"]):
                        extra = f", {comp_tags}" if comp_tags else ""
                        if style_type == "anime":
                            return f"masterpiece, best quality, authentic 2d anime art, {cleaned}{extra}, sharp lineart, 4k", "anime"
                        elif style_type == "realism":
                            return f"masterpiece, raw dslr photo, photorealistic, 8k uhd, {cleaned}{extra}, ultra detailed skin texture, sharp focus", "realism"
                        else:
                            return f"masterpiece, high quality, {cleaned}{extra}, sharp focus, 8k", "general"
                except Exception:
                    continue

    extra_tags = f", {comp_tags}" if comp_tags else ""
    return f"{user_prompt}{extra_tags}, masterpiece, sharp focus, 4k", style_type


async def generate_flux_image(prompt: str) -> Optional[bytes]:
    english_prompt, style_type = await enhance_image_prompt(prompt)

    final_prompt = f"{english_prompt} | negative prompt: {NEGATIVE_PROMPT}"
    logger.info(f"Финальный промпт генерации: {final_prompt}")

    encoded = urllib.parse.quote(final_prompt.strip())
    seed = random.randint(1, 9999999)

    if style_type == "anime":
        selected_model = "turbo"
        width, height = 1024, 1024
    elif style_type == "realism":
        selected_model = "flux-realism"
        width, height = 1216, 832
    else:
        selected_model = "flux"
        width, height = 1024, 1024

    url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&model={selected_model}&seed={seed}&nologo=true"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=65) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"Сбой генерации ({selected_model}): {e}")
    return None


# --- ИНТЕРАКТИВНАЯ ПАНЕЛЬ УПРАВЛЕНИЯ (/panel) ---
def build_control_panel(chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    idle_diff = time.time() - last_owner_activity
    if always_answer_mode:
        st_label = "Сквозной автоответ (Всегда)"
    elif force_offline_mode:
        st_label = "Офлайн (Перехват активен)"
    else:
        st_label = "В сети (Наблюдение)" if idle_diff < OWNER_IDLE_TIMEOUT else "Офлайн (Авто-перехват)"

    v_on = voice_chat_modes.get(chat_id, False)
    ai_engine = "Gemini 2.5 + Groq" if GEMINI_API_KEY else "Groq"
    text = (
        f"<b>Панель управления Ayakoji Core</b>\n\n"
        f"• Ядро ИИ: <b>{ai_engine}</b>\n"
        f"• Режим присутствия: <b>{st_label}</b>\n"
        f"• Постоянный голос: <b>{'ВКЛ' if v_on else 'ВЫКЛ'}</b>\n"
        f"• VIP-союзников (Свои): <b>{len(vip_contacts)}</b>\n"
        f"• Чатов в режиме Призрака: <b>{len(ghost_chats)}</b>\n"
        f"• Записей в памяти: <b>{len(long_term_memory)}</b>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 Я в сети", callback_data="panel_online"),
            InlineKeyboardButton(text="🔴 Я отошел", callback_data="panel_offline")
        ],
        [
            InlineKeyboardButton(
                text=f"⚡ Автоответ: {'ВКЛ' if always_answer_mode else 'ВЫКЛ'}",
                callback_data="panel_toggle_always"
            ),
            InlineKeyboardButton(
                text=f"🎙 Голос: {'ВКЛ' if v_on else 'ВЫКЛ'}",
                callback_data="panel_toggle_voice"
            )
        ],
        [
            InlineKeyboardButton(text="📊 Кто писал?", callback_data="panel_visits"),
            InlineKeyboardButton(text="🧠 Память", callback_data="panel_memory")
        ],
        [
            InlineKeyboardButton(text="🧹 Сбросить контекст чата", callback_data="panel_clear_cache")
        ]
    ])
    return text, kb


@dp.callback_query(F.data.startswith("panel_"))
async def handle_panel_callbacks(callback: types.CallbackQuery):
    global force_offline_mode, always_answer_mode, last_owner_activity

    chat_id = callback.message.chat.id
    action = callback.data

    if action == "panel_online":
        force_offline_mode = False
        always_answer_mode = False
        last_owner_activity = time.time()
        await callback.answer("Режим: Ты в сети (бот не вмешивается).")
    elif action == "panel_offline":
        force_offline_mode = True
        await callback.answer("Режим: Перехват всех входящих активирован.")
    elif action == "panel_toggle_always":
        always_answer_mode = not always_answer_mode
        await callback.answer(f"Сквозной автоответ: {'ВКЛ' if always_answer_mode else 'ВЫКЛ'}")
    elif action == "panel_toggle_voice":
        if voice_chat_modes.get(chat_id, False):
            voice_chat_modes.pop(chat_id, None)
        else:
            voice_chat_modes[chat_id] = True
        await save_settings()
        await callback.answer("Настройка голоса обновлена.")
    elif action == "panel_visits":
        if not business_visits:
            await callback.answer("Журнал визитов пуст.", show_alert=True)
            return
        lines = ["<b>Журнал внешних контактов:</b>\n"]
        for _, info in list(business_visits.items())[-8:]:
            lines.append(f"• <b>{info.get('name')}</b> ({info.get('username')}) в {info.get('time')}:\n  <i>«{info.get('last_msg')}»</i>")
        await callback.message.answer("\n\n".join(lines), parse_mode="HTML")
        await callback.answer()
        return
    elif action == "panel_memory":
        if not long_term_memory:
            await callback.answer("Долгосрочная память пуста. Используй: запомни [факт]", show_alert=True)
            return
        mem_text = "<b>Долгосрочная память Ayakoji:</b>\n\n" + "\n".join(f"{i+1}. {m}" for i, m in enumerate(long_term_memory))
        await callback.message.answer(mem_text, parse_mode="HTML")
        await callback.answer()
        return
    elif action == "panel_clear_cache":
        user_histories.pop(chat_id, None)
        await save_histories()
        await callback.answer("Контекст диалога очищен.", show_alert=True)

    text, kb = build_control_panel(chat_id)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


async def process_bot_command(message: types.Message, user_input: str, is_owner: bool, bus_id: str = "") -> bool:
    global force_offline_mode, always_answer_mode, last_owner_activity

    chat_id = message.chat.id
    lower_text = user_input.lower().strip()
    is_direct = not bool(bus_id)

    public_commands = ["игра", "тапалка", "!игра", "!тапалка", "/game", "аякоджи игра", "аянокоджи игра"]

    # --- ПАНЕЛЬ УПРАВЛЕНИЯ ---
    if is_owner and lower_text in ["/panel", "!панель", "панель", "аякоджи панель", "меню", "!меню"]:
        text, kb = build_control_panel(chat_id)
        await send_smart_response(chat_id, bus_id, text, is_direct=is_direct, reply_markup=kb)
        return True

    # --- 🛡 АНТИ-СКАМ ПРОВЕРКА ССЫЛОК И ДОМЕНОВ (!проверь [ссылка]) ---
    if is_owner and lower_text.startswith(("!проверь ", "проверь ", "аякоджи проверь ", "!скан ")):
        target_link = re.sub(r"^(?:!проверь|проверь|аякоджи проверь|!скан)\s+", "", user_input, flags=re.IGNORECASE).strip()
        if target_link:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=bus_id if not is_direct else None)
            sec_info = await inspect_url_security(target_link)
            sec_report = format_security_report(sec_info)
            if is_direct:
                await send_smart_response(chat_id, "", sec_report, is_direct=True)
            else:
                await notify_owner_pm(sec_report)
            return True

    # --- БЕЛЫЙ СПИСОК (СВОЙ / ЧУЖОЙ) ---
    if is_owner and lower_text in ["!свой", "свой", "аякоджи свой", "!вип"]:
        vip_contacts[chat_id] = True
        await save_settings()
        if is_direct:
            await send_smart_response(chat_id, "", "Чат добавлен в Белый список союзников.", is_direct=True)
        else:
            await notify_owner_pm(f"🤝 Контакт <code>{chat_id}</code> внесен в <b>Белый список (Свой)</b>. Общаюсь с ним уважительно.")
        return True

    if is_owner and lower_text in ["!чужой", "чужой", "аякоджи чужой"]:
        vip_contacts.pop(chat_id, None)
        await save_settings()
        if is_direct:
            await send_smart_response(chat_id, "", "Контакт исключен из Белого списка.", is_direct=True)
        else:
            await notify_owner_pm(f"❄️ Контакт <code>{chat_id}</code> исключен из Белого списка. Включен стандартный холодный протокол.")
        return True

    # --- РЕЖИМ ПРИЗРАКА (ТИХАЯ СЛЕЖКА БЕЗ ОТВЕТОВ) ---
    if is_owner and lower_text in ["!призрак", "призрак", "аякоджи призрак"]:
        if ghost_chats.get(chat_id, False):
            ghost_chats.pop(chat_id, None)
            state_txt = "ВЫКЛЮЧЕН (автоответы снова работают)"
        else:
            ghost_chats[chat_id] = True
            state_txt = "ВКЛЮЧЕН (полная тишина, только сбор досье и перехват удалений)"
        await save_settings()
        if is_direct:
            await send_smart_response(chat_id, "", f"👻 Режим Призрака: <b>{state_txt}</b>.", is_direct=True)
        else:
            await notify_owner_pm(f"👻 Режим Призрака для чата <code>{chat_id}</code>: <b>{state_txt}</b>.")
        return True

    # --- ВЫЧИСЛИТЕЛЬ И КОДЕР (!код / !реши) ---
    if is_owner and lower_text.startswith(("!код ", "аякоджи код ", "!реши ", "аякоджи реши ")):
        task_q = re.sub(r"^(?:!код|аякоджи код|!реши|аякоджи реши)\s+", "", user_input, flags=re.IGNORECASE).strip()
        if task_q:
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=bus_id if not is_direct else None)
            ans = await ask_ai(task_q, chat_id, AYAKOJI_PROMPT_CODER, max_tokens=1200)
            await send_smart_response(chat_id, bus_id, ans, is_direct=is_direct)
            return True

    # --- ПСИХОЛОГИЧЕСКОЕ ДОСЬЕ НА СОБЕСЕДНИКА ---
    if is_owner and lower_text in ["!досье", "досье", "аякоджи анализ", "!анализ", "психопортрет"]:
        dossier = await generate_psychological_dossier(chat_id)
        if is_direct:
            await send_smart_response(chat_id, "", dossier, is_direct=True)
        else:
            await notify_owner_pm(dossier)
        return True

    # --- ДОЛГОСРОЧНАЯ ПАМЯТЬ ---
    if is_owner and lower_text.startswith(("запомни ", "!запомни ", "аякоджи запомни ")):
        fact = re.sub(r"^(?:запомни|!запомни|аякоджи запомни)\s+", "", user_input, flags=re.IGNORECASE).strip()
        if fact:
            long_term_memory.append(fact)
            await save_memory()
            await send_smart_response(chat_id, bus_id, f"Записано в долгосрочную память: <i>«{fact}»</i>.", is_direct=is_direct)
            return True

    if is_owner and lower_text in ["!память", "память", "аякоджи память"]:
        if not long_term_memory:
            await send_smart_response(chat_id, bus_id, "Долгосрочная память пуста.", is_direct=is_direct)
        else:
            mem_str = "<b>Долгосрочная память:</b>\n" + "\n".join(f"{i+1}. {m}" for i, m in enumerate(long_term_memory))
            await send_smart_response(chat_id, bus_id, mem_str, is_direct=is_direct)
        return True

    if is_owner and lower_text in ["очисти память", "!очисти память", "сброс памяти"]:
        long_term_memory.clear()
        await save_memory()
        await send_smart_response(chat_id, bus_id, "Долгосрочная память полностью стерта.", is_direct=is_direct)
        return True

    # --- 1. ГЕНЕРАЦИЯ КАРТИНОК (ЛЮБОЙ ПОРЯДОК СЛОВ) ---
    draw_pattern = r"\b(нарисуй|сгенерируй|создай арт|нарисуйте|арт)\b"
    if re.search(draw_pattern, lower_text) or lower_text.startswith("!арт"):
        prompt = re.sub(r"\b(аякоджи|аянокоджи|пожалуйста|мне|нарисуй|сгенерируй|создай арт|нарисуйте|арт|!арт|!нарисуй)\b", "", user_input, flags=re.IGNORECASE).strip()
        prompt = re.sub(r"^[\s,.:;!?-]+|[\s,.:;!?-]+$", "", prompt).strip()

        if not prompt:
            await send_smart_response(chat_id, bus_id, "Укажи объект для визуализации.", is_direct=is_direct)
            return True

        await send_smart_response(chat_id, bus_id, f"Начинаю визуализацию объекта: <i>«{prompt}»</i>. Оптимизирую пропорции и детали...", is_direct=is_direct)

        img_bytes = await generate_flux_image(prompt)
        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, filename="ayakoji_art.jpg")
            kwargs = {"chat_id": chat_id, "photo": photo_file, "caption": f"Визуализация завершена.\nОбъект: {prompt}"}
            if bus_id:
                kwargs["business_connection_id"] = bus_id
            await bot.send_photo(**kwargs)
        else:
            await send_smart_response(chat_id, bus_id, "Сбой модуля графики. Повтори запрос.", is_direct=is_direct)
        return True

    # 2. ПРИНУДИТЕЛЬНЫЙ РУЧНОЙ ПОИСК
    if lower_text.startswith(("найди ", "!найди ", "поиск ", "!поиск ", "аякоджи найди ", "аянокоджи найди ", "погугли ")):
        query = re.sub(r"^(?:найди|!найди|поиск|!поиск|аякоджи найди|аянокоджи найди|погугли)\s+", "", user_input, flags=re.IGNORECASE).strip()
        if not query:
            await send_smart_response(chat_id, bus_id, "Укажи поисковый запрос.", is_direct=is_direct)
            return True

        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING, business_connection_id=bus_id if not is_direct else None)
        search_data = await duckduckgo_search(query)
        if search_data:
            s_prompt = (
                f"Пользователь запросил поиск: '{query}'.\n"
                f"Вот свежие факты из поисковой выдачи сети:\n{search_data}\n\n"
                f"Сформулируй четкий, хладнокровный и точный ответ от лица Аянокоджи без звездочек (*)."
            )
            answer = await ask_ai(s_prompt, chat_id, get_direct_prompt(), max_tokens=450)
            await send_smart_response(chat_id, bus_id, answer, is_direct=is_direct)
        else:
            await send_smart_response(chat_id, bus_id, f"В сети нет точных данных по запросу: {query}.", is_direct=is_direct)
        return True

    # 3. НАПОМИНАНИЯ И ТАЙМЕРЫ
    remind_match = re.match(r"^(?:напомни|!напомни|аякоджи напомни|аянокоджи напомни)\s+(?:через\s+)?(\d+)\s*(сек|мин|час|дн|ч|м|с)[а-я]*\s+(.+)$", user_input, re.IGNORECASE)
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
        await send_smart_response(chat_id, bus_id, f"Таймер зафиксирован. Напомню про <i>«{rem_text}»</i> через {qty} {unit_name}.", is_direct=is_direct)
        return True

    # 4. ОТЧЕТ О ВИЗИТАХ ДЛЯ TELEGRAM BUSINESS
    if lower_text in ["кто писал?", "кто писал", "!отчет", "отчет", "аякоджи отчет", "аянокоджи отчет", "визиты", "!визиты"]:
        if not business_visits:
            await send_smart_response(chat_id, bus_id, "За время твоего отсутствия входящих контактов не зафиксировано.", is_direct=is_direct)
            return True

        report_lines = ["<b>Журнал внешних контактов (Telegram Business):</b>\n"]
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
        await send_smart_response(chat_id, bus_id, "Журнал визитов очищен.", is_direct=is_direct)
        return True

    # КНБ
    if lower_text.startswith(("кнб ", "!кнб ")):
        parts = lower_text.split()
        user_choice = parts[1] if len(parts) > 1 else ""
        choices = ["камень", "ножницы", "бумага"]
        if user_choice not in choices:
            await send_smart_response(chat_id, bus_id, "Выбери: камень, ножницы или бумага.", is_direct=is_direct)
            return True

        bot_choice = random.choice(choices)
        if user_choice == bot_choice:
            res = f"Мой ход — {bot_choice}. Ничья."
        elif (user_choice == "камень" and bot_choice == "ножницы") or \
             (user_choice == "ножницы" and bot_choice == "бумага") or \
             (user_choice == "бумага" and bot_choice == "камень"):
            res = f"Мой ход — {bot_choice}. Твоя победа."
        else:
            res = f"Мой ход — {bot_choice}. Я победил, как и было рассчитано."

        await send_smart_response(chat_id, bus_id, res, is_direct=is_direct)
        return True

    if not is_owner and not lower_text.startswith(("статус", "!статус")) and lower_text not in public_commands:
        return False

    if lower_text in public_commands:
        await send_smart_response(chat_id, bus_id, f"Запуск мини-системы:\n{GAME_URL}", is_direct=is_direct)
        return True

    # Управление присутствием
    if lower_text in ["аякоджи я тут", "аянокоджи я тут", "!онлайн", "!я тут", "я тут", "аякоджи тут"]:
        force_offline_mode = False
        last_owner_activity = time.time()
        await send_smart_response(chat_id, bus_id, "Понял. Ты в сети — перехожу в режим наблюдения и не вмешиваюсь.", is_direct=is_direct)
        return True

    if lower_text in ["аякоджи я отошел", "аянокоджи я отошел", "!офлайн", "!оффлайн", "!отошел", "аякоджи офлайн"]:
        force_offline_mode = True
        await send_smart_response(chat_id, bus_id, "Режим перехвата включен. Беру все входящие диалоги на себя.", is_direct=is_direct)
        return True

    if lower_text in ["аякоджи отвечай", "аянокоджи отвечай", "!автоответ вкл", "автоответ вкл"]:
        always_answer_mode = True
        await send_smart_response(chat_id, bus_id, "Сквозной автоответ включен: отвечаю всем независимо от твоего статуса.", is_direct=is_direct)
        return True

    if lower_text in ["аякоджи молчи", "аянокоджи молчи", "!автоответ выкл", "автоответ выкл"]:
        always_answer_mode = False
        await send_smart_response(chat_id, bus_id, "Сквозной автоответ отключен. Работаю по таймеру твоей активности.", is_direct=is_direct)
        return True

    # Голос
    if lower_text in ["аякоджи голос вкл", "аянокоджи голос вкл", "голосовой режим вкл", "!голос вкл"]:
        voice_chat_modes[chat_id] = True
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Голосовой режим активирован.", is_direct=is_direct, send_as_voice=True)
        return True

    if lower_text in ["аякоджи голос выкл", "аянокоджи голос выкл", "голосовой режим выкл", "!голос выкл"]:
        voice_chat_modes.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Постоянный голосовой режим отключен.", is_direct=is_direct)
        return True

    # Мут
    if lower_text.startswith(("мут", "!мут", "аякоджи мут", "аянокоджи мут")):
        parts = user_input.split()
        duration_minutes = None
        for p in parts[1:]:
            if p.isdigit():
                duration_minutes = int(p)
                break

        if duration_minutes:
            muted_chats[chat_id] = time.time() + (duration_minutes * 60)
            notice = f"Собеседник изолирован на {duration_minutes} минут."
        else:
            muted_chats[chat_id] = float('inf')
            notice = "Собеседник отправлен в бессрочную изоляцию."

        await save_settings()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Снять изоляцию", callback_data="ayakoji_unmute_direct")]])
        await send_smart_response(chat_id, bus_id, notice, is_direct=is_direct, reply_markup=kb)
        return True

    # Размут
    if lower_text in ["размут", "!размут", "аякоджи размут", "аянокоджи размут", "анмут", "!анмут", "unmute"]:
        muted_chats.pop(chat_id, None)
        await save_settings()
        await send_smart_response(chat_id, bus_id, "Изоляция снята.", is_direct=is_direct)
        return True

    # Спам
    if lower_text.startswith(("спам", "!спам", "аякоджи спам", "аянокоджи спам")):
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
            info = f"Запущена серия сообщений ({spam_count} шт.)." if spam_count else "Запущена бесконечная рассылка."
            await send_smart_response(chat_id, bus_id, info, is_direct=is_direct)
        else:
            await send_smart_response(chat_id, bus_id, "Укажи текст для рассылки.", is_direct=is_direct)
        return True

    # Стопспам
    if lower_text in ["стопспам", "!стопспам", "стоп спам", "стоп аякоджи", "стоп аянокоджи"]:
        if chat_id in active_spams:
            active_spams[chat_id].cancel()
            active_spams.pop(chat_id, None)
            await send_smart_response(chat_id, bus_id, "Рассылка остановлена.", is_direct=is_direct)
            return True
        else:
            user_histories.pop(chat_id, None)
            await save_histories()
            await send_smart_response(chat_id, bus_id, "Процессы сброшены. Память диалога очищена.", is_direct=is_direct)
            return True

    # Статус
    if lower_text in ["статус", "!статус", "аякоджи статус", "аянокоджи статус"]:
        if chat_id in muted_chats:
            g_status = "Изолирован"
        elif chat_id in ghost_chats:
            g_status = "Режим Призрака (Тихая слежка)"
        elif chat_id in vip_contacts:
            g_status = "VIP-союзник (Свой)"
        else:
            g_status = "Обычный гость"

        v_status = "Постоянно" if voice_chat_modes.get(chat_id, False) else "Адаптивный"
        tts_source = "Fish Audio (Чанковый)" if FISH_AUDIO_API_KEY else "Edge-TTS"

        idle_diff = time.time() - last_owner_activity
        if always_answer_mode:
            owner_status = "Всегда отвечать (Сквозной режим)"
        elif force_offline_mode:
            owner_status = "Офлайн (Принудительный перехват)"
        else:
            owner_status = f"В сети (был {int(idle_diff)} сек. назад)" if idle_diff < OWNER_IDLE_TIMEOUT else "Офлайн (Авто-перехват)"

        primary_m = "Gemini 2.5 Flash / Pro" if GEMINI_API_KEY else "Groq Llama"

        status_msg = (
            f"<b>Система Ayakoji Core:</b>\n"
            f"• Статус создателя: <b>{owner_status}</b>\n"
            f"• Главный мозг: <b>{primary_m}</b>\n"
            f"• Резервных ключей Groq: {len(GROQ_KEYS)}\n"
            f"• Модули: Link Shield, Медиа-Перехват, VIP, Призрак, Чанк-Голос\n"
            f"• Голос: {tts_source} ({v_status})\n"
            f"• Статус текущего чата: <b>{g_status}</b>"
        )
        await send_smart_response(chat_id, bus_id, status_msg, is_direct=is_direct)
        return True

    # Очистка памяти
    if lower_text in ["аякоджи сброс", "аянокоджи сброс", "!сброс", "сброс", "аякоджи кэш"]:
        user_histories.pop(chat_id, None)
        await save_histories()
        await send_smart_response(chat_id, bus_id, "Память диалога полностью очищена.", is_direct=is_direct)
        return True

    return False


@dp.callback_query(F.data == "ayakoji_unmute_direct")
async def handle_unmute_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id

    is_guest = (user_id == chat_id)
    is_unauthorized = is_guest or (OWNER_ID != 0 and user_id != OWNER_ID)

    if is_unauthorized:
        await callback.answer("Доступ закрыт.", show_alert=True)
        return

    muted_chats.pop(chat_id, None)
    await save_settings()
    await callback.answer("Изоляция снята.")
    try:
        await callback.message.edit_text("Изоляция собеседника снята.")
    except Exception:
        pass


# --- ПЕРЕХВАТ ОТРЕДАКТИРОВАННЫХ СООБЩЕНИЙ В TELEGRAM BUSINESS ---
@dp.edited_business_message()
async def handle_edited_business_message(message: types.Message):
    chat_id = message.chat.id
    msg_id = message.message_id
    bot_id = bot.id if bot else 0

    if not message.from_user or message.from_user.is_bot or message.from_user.id == bot_id:
        return

    is_owner = (OWNER_ID != 0 and message.from_user.id == OWNER_ID) or (message.from_user.id != chat_id)
    if is_owner:
        return

    old_record = business_msg_cache.get(chat_id, msg_id)
    new_text = message.text or message.caption or "[медиа]"
    user_name = message.from_user.full_name or "Гость"
    username_str = f"@{message.from_user.username}" if message.from_user.username else f"ID {chat_id}"
    m_type, f_id = extract_media_meta(message)

    if old_record and old_record.get("text") != new_text:
        alert = (
            f"👁 <b>Перехват редактирования (Ayakoji Eye):</b>\n"
            f"• Объект: <b>{user_name}</b> ({username_str})\n"
            f"• Было: <i>«{old_record.get('text')}»</i>\n"
            f"• Стало: <i>«{new_text}»</i>"
        )
        await notify_owner_pm(alert)

    business_msg_cache.put(chat_id, msg_id, {
        "name": user_name,
        "username": username_str,
        "text": new_text,
        "media_type": m_type,
        "file_id": f_id,
        "time": datetime.datetime.now().strftime("%H:%M:%S")
    })


# --- ПЕРЕХВАТ УДАЛЕННЫХ СООБЩЕНИЙ И МЕДИАФАЙЛОВ В TELEGRAM BUSINESS ---
@dp.deleted_business_messages()
async def handle_deleted_business_messages(event: BusinessMessagesDeleted):
    chat_id = event.chat.id
    for mid in event.message_ids:
        cached = business_msg_cache.get(chat_id, mid)
        if cached:
            m_type = cached.get("media_type")
            f_id = cached.get("file_id")
            type_label = f" [{m_type.upper()}]" if m_type else ""
            alert = (
                f"🗑 <b>Перехват удаленного сообщения{type_label} (Ayakoji Eye):</b>\n"
                f"• Объект: <b>{cached.get('name')}</b> ({cached.get('username')})\n"
                f"• Время отправки: {cached.get('time')}\n"
                f"• Содержание: <i>«{cached.get('text')}»</i>"
            )
            await notify_owner_pm(alert, media_type=m_type, file_id=f_id)


# --- ЛИЧНЫЙ ЧАТ С БОТОМ ---
@dp.message(F.business_connection_id.is_(None))
async def handle_direct_message(message: types.Message):
    global last_owner_activity, owner_pm_chat_id

    if not message.from_user or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    last_owner_activity = time.time()

    if owner_pm_chat_id == 0 or (OWNER_ID != 0 and message.from_user.id == OWNER_ID):
        if owner_pm_chat_id != chat_id:
            owner_pm_chat_id = chat_id
            asyncio.create_task(save_settings())

    user_input, is_voice = await extract_message_content(message)
    if not user_input.strip():
        return

    if user_input.strip() == "/start":
        text, kb = build_control_panel(chat_id)
        await send_smart_response(chat_id, "", f"Система Ayakoji активна. Жду указаний.\n\n{text}", is_direct=True, reply_markup=kb)
        return

    if await process_bot_command(message, user_input, is_owner=True, bus_id=""):
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    actual_prompt = user_input

    url_match = re.search(r"https?://[^\s]+", user_input)
    if url_match:
        found_url = url_match.group(0)
        sec_info = await inspect_url_security(found_url)
        page_content = await fetch_webpage_text(sec_info["final_url"])
        sec_summary = format_security_report(sec_info)
        await send_smart_response(chat_id, "", sec_summary, is_direct=True)
        if page_content:
            actual_prompt = (
                f"Сообщение пользователя: '{user_input}'.\n"
                f"Содержимое веб-страницы по ссылке ({sec_info['final_url']}):\n{page_content}\n\n"
                f"Сделай четкую, умную выжимку сути или ответь на вопрос по этой ссылке без звездочек (*)."
            )
    elif should_auto_search(user_input):
        search_query = clean_search_query(user_input)
        web_facts = await duckduckgo_search(search_query)
        if web_facts:
            actual_prompt = (
                f"Вопрос: '{user_input}'.\n"
                f"Актуальные данные из сети:\n{web_facts}\n"
                f"Используй эти данные для точного ответа без звездочек (*)."
            )

    voice_triggers = ["в голосовом", "голосовым", "голосом", "скажи в гс", "озвучь", "проговори"]
    forced_voice_request = any(t in user_input.lower() for t in voice_triggers)

    reply = await ask_ai(actual_prompt, chat_id, get_direct_prompt(), max_tokens=450)

    random_voice_chance = random.random() < 0.25
    should_voice = is_voice or forced_voice_request or voice_chat_modes.get(chat_id, False) or random_voice_chance
    await send_smart_response(chat_id, "", reply, is_direct=True, send_as_voice=should_voice)


# --- ТЕЛЕГРАМ БИЗНЕС ---
@dp.business_message()
async def handle_business_message(message: types.Message):
    global last_owner_activity, owner_pm_chat_id

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
        if owner_pm_chat_id == 0:
            owner_pm_chat_id = message.from_user.id
            asyncio.create_task(save_settings())
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
    m_type, f_id = extract_media_meta(message)

    # 1. Сохраняем сообщение и file_id медиа в кэш для Анти-Удаления
    business_msg_cache.put(chat_id, msg_id, {
        "name": user_name,
        "username": username_str,
        "text": user_input,
        "media_type": m_type,
        "file_id": f_id,
        "time": datetime.datetime.now().strftime("%H:%M:%S")
    })

    # 2. Фиксируем в журнал визитов
    business_visits[str(chat_id)] = {
        "name": user_name,
        "username": username_str,
        "time": datetime.datetime.now().strftime("%H:%M"),
        "last_msg": user_input[:100]
    }
    asyncio.create_task(save_visits())

    # 3. 🛡 АВТОМАТИЧЕСКИЙ АНТИ-СКАМ СКАНЕР ССЫЛОК В БИЗНЕС-ЧАТАХ
    guest_url_match = re.search(r"https?://[^\s]+", user_input)
    if guest_url_match:
        g_url = guest_url_match.group(0)
        sec_info = await inspect_url_security(g_url)
        if sec_info["is_ip_logger"] or sec_info["is_shortener"] or sec_info["warnings"]:
            sec_alert = (
                f"⚠️ <b>Перехвачена ссылка от {user_name} ({username_str}):</b>\n\n"
                + format_security_report(sec_info)
            )
            asyncio.create_task(notify_owner_pm(sec_alert))

    # 4. РАДАР СРОЧНОГО ВЫЗОВА
    now = time.time()
    if is_urgent_message(user_input) and (now - urgent_alert_cooldowns.get(chat_id, 0) > 180):
        urgent_alert_cooldowns[chat_id] = now
        urgent_notice = (
            f"⚠️ <b>Приоритетный сигнал (Радар срочности):</b>\n"
            f"• От: <b>{user_name}</b> ({username_str})\n"
            f"• Сообщение: <i>«{user_input[:300]}»</i>"
        )
        asyncio.create_task(notify_owner_pm(urgent_notice))

    # 5. РЕЖИМ ПРИЗРАКА
    if ghost_chats.get(chat_id, False):
        user_histories.setdefault(chat_id, [{"role": "system", "content": AYAKOJI_PROMPT_GUEST}])
        user_histories[chat_id].append({"role": "user", "content": user_input})
        if len(user_histories[chat_id]) > 11:
            user_histories[chat_id] = [user_histories[chat_id][0]] + user_histories[chat_id][-10:]
        asyncio.create_task(save_histories())
        return

    # 6. Проверка онлайна хозяина
    if not always_answer_mode:
        if not force_offline_mode and (now - last_owner_activity) < OWNER_IDLE_TIMEOUT:
            return

    if is_guest and chat_id in muted_chats:
        m_time = muted_chats[chat_id]
        if m_time == float('inf') or now < m_time:
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

    target_system_prompt = AYAKOJI_PROMPT_VIP if chat_id in vip_contacts else AYAKOJI_PROMPT_GUEST

    actual_prompt = user_input
    if should_auto_search(user_input):
        search_query = clean_search_query(user_input)
        web_facts = await duckduckgo_search(search_query)
        if web_facts:
            actual_prompt = (
                f"Вопрос собеседника: '{user_input}'.\n"
                f"Факты из сети:\n{web_facts}\n"
                f"Ответь на основе этих фактов в соответствии со своей ролью без звездочек (*)."
            )

    reply = await ask_ai(actual_prompt, chat_id, target_system_prompt, max_tokens=240)

    random_voice_chance = random.random() < 0.25
    should_voice = is_voice or voice_chat_modes.get(chat_id, False) or random_voice_chance
    await send_smart_response(chat_id, bus_id, reply, is_direct=False, send_as_voice=should_voice, human_delay=True)


# --- ФОНОВЫЙ МОДУЛЬ НАПОМИНАНИЙ И ОЧИСТКИ ТАЙМАУТОВ ---
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
                    notice = f"Время вышло.\nНапоминание: <b>{rem.get('text')}</b>"
                    c_id = rem.get("chat_id")
                    b_id = rem.get("bus_id", "")
                    await send_smart_response(c_id, b_id, notice, is_direct=not bool(b_id), send_as_voice=True)
                    reminders_list.remove(rem)
                await save_reminders()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка в cleaner_and_reminders_task: {e}")


# --- WEB СЕРВЕР RENDER (KEEP-ALIVE) ---
async def handle_ping(request):
    return web.Response(text="Ayakoji Core is fully operational!")


async def setup_web_app():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


# --- ТОЧКА ВХОДА ---
async def main():
    if not BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN не задан!")
        return

    web_runner = await setup_web_app()
    bg_task = asyncio.create_task(cleaner_and_reminders_task())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        tts_engine = "Fish Audio" if FISH_AUDIO_API_KEY else "Edge-TTS"
        ai_core = "Gemini 2.5 + Groq" if GEMINI_API_KEY else "Groq"
        logger.info(f"Ayakoji онлайн! Ядро ИИ: {ai_core} | Голос: {tts_engine}")
        await dp.start_polling(bot)
    except TelegramConflictError:
        logger.critical("Конфликт сессий! Запущен второй экземпляр бота.")
    finally:
        bg_task.cancel()
        await web_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Ayakoji завершил работу.")
