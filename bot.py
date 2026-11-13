import os
import io
import re
import json
import html
import base64
import time
import random
import asyncio
import urllib.parse
from datetime import datetime, timedelta

import aiohttp
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, BufferedInputFile
)
from openai import AsyncOpenAI

# Подгружаем переменные из .env (если файл есть)
load_dotenv()

# Поддержка поиска в интернете
try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8130566129:AAHGfVKiIorLhqGAU_HtbCPjSyXQlWgbzVw").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5656256968").strip())

AI_API_KEY = os.getenv("AI_API_KEY", "sk-eF2ZQEE1uyLh6VRq1x26zryHbbHqKT7plceJQ37U3Au1IrHm").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://anymodel.ghostly.su/v1").strip()

# Ключ и ссылка для генерации изображений
PHOTO_GENERATIONAL_KEY = os.getenv("PHOTO_GENERATIONAL_KEY", AI_API_KEY).strip()
PHOTO_BASE_URL = os.getenv("PHOTO_BASE_URL", "https://api.podpiski-neiroseti.shop").strip()
PHOTO_MODEL = os.getenv("PHOTO_MODEL", "dall-e-3").strip()

DATA_FILE = "bot_data.json"

# Динамические настройки (сохраняются в БД)
bot_config = {
    "free_limit": int(os.getenv("FREE_LIMIT", "45000").strip()),
    "sub_limit": int(os.getenv("SUB_LIMIT", "1000000").strip()),
    "photo_model": PHOTO_MODEL
}

BASE_SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты умный, точный и полезный ИИ-ассистент в Telegram-боте. Отвечай четко, структурированно и по делу.\n"
    "ВАЖНЫЕ ПРАВИЛА ФОРМАТИРОВАНИЯ ДЛЯ TELEGRAM:\n"
    "1. Используй **жирный текст** для важного и `моноширинный` для коротких команд или формул.\n"
    "2. Если пишешь программный код, ОБЯЗАТЕЛЬНО оборачивай его в тройные кавычки с указанием языка:\n"
    "```python\nкод\n```\n"
    "3. Если пользователь прислал изображение с задачей, текстом или вопросом — внимательно прочитай всё содержимое на фото и дай полное решение."
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- УМНАЯ РАБОТА С URL (С /v1 И БЕЗ /v1) ---
def get_url_variants(raw_url: str) -> list[str]:
    clean = raw_url.strip().rstrip("/")
    if clean.endswith("/v1"):
        base_no_v1 = clean[:-3].rstrip("/")
        return [clean, base_no_v1]
    else:
        return [f"{clean}/v1", clean]


AI_URL_VARIANTS = get_url_variants(AI_BASE_URL)
PHOTO_URL_VARIANTS = get_url_variants(PHOTO_BASE_URL)

ai_clients = [AsyncOpenAI(api_key=AI_API_KEY, base_url=u, timeout=90.0) for u in AI_URL_VARIANTS]
photo_clients = [AsyncOpenAI(api_key=PHOTO_GENERATIONAL_KEY, base_url=u, timeout=90.0) for u in PHOTO_URL_VARIANTS]


# --- БАЗА МОДЕЛЕЙ ---
DEFAULT_MODEL = "gpt-6-luna"

models_db = {
    "deepseek-flash": {"name": "DeepSeek Flash", "premium": False, "vision": False},
    "deepseek-v4.1-flash-1017": {"name": "DeepSeek v4.1", "premium": False, "vision": False},
    "gemini-3.5-flash-lite": {"name": "Gemini 3.5 Lite", "premium": False, "vision": False},
    "gemini-3.6-flash": {"name": "Gemini 3.6 Flash", "premium": False, "vision": True},
    "gemini-3.8-flash": {"name": "Gemini 3.8 Flash", "premium": False, "vision": True},
    "gpt-6-luna": {"name": "GPT-6 Luna", "premium": False, "vision": True},
    "gpt-6-so": {"name": "GPT-6 SO", "premium": False, "vision": True},
    "sonnet-5": {"name": "Claude 5 Sonnet", "premium": False, "vision": True},
    "fable-5.1": {"name": "Claude 5.1 Fable", "premium": False, "vision": False},
    "gpt-6.1-sol": {"name": "GPT-6.1 Sol", "premium": True, "vision": True},
    "gpt-6-astra": {"name": "GPT-6 Astra", "premium": True, "vision": True},
    "opus-5.5": {"name": "Claude 5.5 Opus", "premium": True, "vision": True},
    "opus-5": {"name": "Claude 5 Opus", "premium": True, "vision": True},
    "sonnet-5.5": {"name": "Claude 5.5 Sonnet", "premium": True, "vision": True},
    "gemini-4-argon": {"name": "Gemini 4 Argon", "premium": True, "vision": True},
    "grok-4.7": {"name": "Grok 4.7", "premium": True, "vision": True},
    "deepseek-r1": {"name": "DeepSeek R1", "premium": True, "vision": False}
}

users_db = {}
user_image_cache = {}  # Кэш последнего фото юзера в оперативной памяти: {uid: {"b64": str, "time": datetime}}
user_last_img_prompt = {}  # Последний промпт генерации фото для кнопки повтора
anti_spam = {}
admin_reply_target = {}
admin_state = {}


# --- СОХРАНЕНИЕ И ЗАГРУЗКА БАЗЫ ДАННЫХ (JSON) ---
def save_data():
    """Сохраняет пользователей, настройки и модели в JSON-файл."""
    try:
        serializable_users = {}
        for uid, u in users_db.items():
            u_copy = dict(u)
            u_copy["sub_end"] = u["sub_end"].isoformat() if u.get("sub_end") else None
            u_copy["reset_date"] = u["reset_date"].isoformat() if u.get("reset_date") else None
            serializable_users[str(uid)] = u_copy

        payload = {
            "bot_config": bot_config,
            "models_db": models_db,
            "users_db": serializable_users
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[DB Warning] Ошибка сохранения данных: {e}")


def load_data():
    """Загружает данные при старте бота."""
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)

        if "bot_config" in payload:
            bot_config.update(payload["bot_config"])
        if "models_db" in payload and payload["models_db"]:
            models_db.update(payload["models_db"])
        if "users_db" in payload:
            for uid_str, u in payload["users_db"].items():
                uid = int(uid_str)
                u["sub_end"] = datetime.fromisoformat(u["sub_end"]) if u.get("sub_end") else None
                u["reset_date"] = datetime.fromisoformat(u["reset_date"]) if u.get("reset_date") else (datetime.now() + timedelta(days=30))
                users_db[uid] = u
    except Exception as e:
        print(f"[DB Warning] Ошибка чтения данных: {e}")


# --- УМНЫЙ ВЫЗОВ ИИ С ФОЛЛБЭКАМИ ---
async def create_chat_completion(**kwargs):
    """Пробует выполнить запрос к ИИ по разным вариантам URL и с запасной моделью при сбое."""
    last_err = None
    for client in ai_clients:
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as e:
            last_err = e

    # Если выбранная модель временно недоступна на прокси — пробуем дефолтную модель
    req_model = kwargs.get("model")
    if req_model and req_model != DEFAULT_MODEL:
        kwargs["model"] = DEFAULT_MODEL
        for client in ai_clients:
            try:
                return await client.chat.completions.create(**kwargs)
            except Exception as e:
                last_err = e

    raise last_err


# --- БЕЗОТКАЗНОЕ РАСПОЗНАВАНИЕ ИЗОБРАЖЕНИЙ (VISION) ---
VISION_REFUSAL_MARKERS = (
    "не могу просматривать", "не вижу изображен", "не могу увидеть",
    "cannot see the image", "can't view images", "unable to view",
    "я текстовая модель", "не умею работать с изображениями"
)


def get_vision_fallback_models(preferred_model: str) -> list[str]:
    """Возвращает список Vision-моделей для перебора, начиная с выбранной пользователем."""
    candidates = []
    if models_db.get(preferred_model, {}).get("vision"):
        candidates.append(preferred_model)

    priority_vision = [
        "gemini-3.8-flash", "gemini-3.6-flash", "gpt-6-luna",
        "gpt-6-so", "sonnet-5", "gpt-6.1-sol", "gemini-4-argon"
    ]
    for m in priority_vision:
        if m in models_db and m not in candidates:
            candidates.append(m)

    for m, info in models_db.items():
        if info.get("vision") and m not in candidates:
            candidates.append(m)

    return candidates or [DEFAULT_MODEL]


async def create_vision_completion(
    preferred_model: str,
    sys_prompt: str,
    history: list[dict],
    user_text: str,
    b64_image: str
) -> str:
    """
    Гарантированно обрабатывает изображение:
    1. Формирует корректный мультимодальный запрос.
    2. Пробует сначала без стриминга (на прокси стриминг с base64 часто обрывается).
    3. Если модель выдала ошибку или отказ («я не вижу фото»), автоматически переключается на следующую Vision-модель.
    """
    prompt_text = (
        user_text.strip()
        if user_text and user_text.strip()
        else "Внимательно изучи это изображение. Если на нем задание, тест, код или задача — подробно реши его. Если текст — разбери и ответь. Если просто картинка — подробно опиши."
    )

    vision_models = get_vision_fallback_models(preferred_model)
    last_err = None

    # Очищаем историю от слишком старых сообщений, чтобы не перегружать прокси с картинкой
    short_history = history[-4:] if history else []

    for mod in vision_models:
        # Вариант 1: Полный запрос с историей
        messages_payload = [{"role": "system", "content": sys_prompt}]
        for h in short_history:
            if isinstance(h.get("content"), str):
                messages_payload.append(h)

        messages_payload.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": b64_image}}
            ]
        })

        for client in ai_clients:
            try:
                resp = await client.chat.completions.create(
                    model=mod,
                    messages=messages_payload,
                    stream=False
                )
                ans = (resp.choices[0].message.content or "").strip()
                if ans and not any(marker in ans.lower() for marker in VISION_REFUSAL_MARKERS):
                    return ans
            except Exception as e:
                last_err = e

        # Вариант 2: Упрощенный запрос (некоторые прокси не переваривают role="system" вместе с image_url)
        simple_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"{sys_prompt}\n\nЗапрос пользователя: {prompt_text}"},
                    {"type": "image_url", "image_url": {"url": b64_image}}
                ]
            }
        ]
        for client in ai_clients:
            try:
                resp = await client.chat.completions.create(
                    model=mod,
                    messages=simple_messages,
                    stream=False
                )
                ans = (resp.choices[0].message.content or "").strip()
                if ans and not any(marker in ans.lower() for marker in VISION_REFUSAL_MARKERS):
                    return ans
            except Exception as e:
                last_err = e

    if last_err:
        raise last_err
    return "⚠️ Не удалось распознать изображение. Попробуйте выбрать другую модель с иконкой 🖼."


# --- БЕЗОТКАЗНАЯ ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ---
async def download_image_bytes(url: str) -> bytes | None:
    """Скачивает картинку по URL в байты, чтобы Telegram гарантированно отправил фото."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=60, allow_redirects=True) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 800:  # Проверка, что это реально картинка, а не пустой ответ
                        return data
    except Exception:
        pass
    return None


async def translate_prompt_for_image(prompt: str) -> str:
    """Переводит и улучшает промпт на английский для идеальной генерации фото."""
    try:
        resp = await create_chat_completion(
            model=DEFAULT_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Translate the user's image description into English and enhance it slightly "
                        "for an AI image generator (detailed, high quality, 8k). "
                        "Output ONLY the English prompt, nothing else."
                    )
                },
                {"role": "user", "content": prompt[:500]}
            ],
            max_tokens=150,
            stream=False
        )
        ans = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
        if ans and len(ans) > 3:
            return ans
    except Exception:
        pass
    return prompt


async def generate_image_api(prompt: str) -> bytes:
    """
    Многоуровневая генерация изображений (возвращает чистые байты картинки):
    1. Пробует PHOTO_BASE_URL через OpenAI SDK и прямой POST.
    2. Пробует основной AI_BASE_URL через OpenAI SDK.
    3. Если прокси вернул ошибку или недоступен — автоматически генерирует через Flux (Pollinations AI).
    """
    models_to_try = [bot_config["photo_model"]]
    for fallback_m in ["dall-e-3", "flux", "sdxl"]:
        if fallback_m not in models_to_try:
            models_to_try.append(fallback_m)

    # 1. Пробуем через клиентов PHOTO_BASE_URL и AI_BASE_URL
    all_clients = photo_clients + ai_clients
    for client in all_clients:
        for mod in models_to_try[:2]:
            try:
                resp = await client.images.generations.create(
                    model=mod,
                    prompt=prompt,
                    n=1,
                    size="1024x1024"
                )
                if resp and getattr(resp, "data", None) and len(resp.data) > 0:
                    item = resp.data[0]
                    if getattr(item, "b64_json", None):
                        return base64.b64decode(item.b64_json)
                    if getattr(item, "url", None):
                        img_bytes = await download_image_bytes(item.url)
                        if img_bytes:
                            return img_bytes
            except Exception:
                continue

    # 2. Прямые HTTP-запросы к PHOTO_BASE_URL и AI_BASE_URL
    base_candidates = [
        (PHOTO_BASE_URL, PHOTO_GENERATIONAL_KEY),
        (AI_BASE_URL, AI_API_KEY)
    ]

    async with aiohttp.ClientSession() as session:
        for raw_base, key in base_candidates:
            base_clean = raw_base.rstrip("/")
            if base_clean.endswith("/v1"):
                base_clean = base_clean[:-3].rstrip("/")

            endpoints = [
                f"{base_clean}/v1/images/generations",
                f"{base_clean}/images/generations"
            ]
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": bot_config["photo_model"],
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024"
            }

            for ep in endpoints:
                try:
                    async with session.post(ep, json=payload, headers=headers, timeout=45) as r:
                        if r.status == 200:
                            data = await r.json(content_type=None)
                            url_found = None
                            if isinstance(data, dict):
                                if "data" in data and isinstance(data["data"], list) and data["data"]:
                                    first = data["data"][0]
                                    if first.get("b64_json"):
                                        return base64.b64decode(first["b64_json"])
                                    url_found = first.get("url")
                                elif data.get("url"):
                                    url_found = data["url"]
                            if url_found:
                                img_bytes = await download_image_bytes(url_found)
                                if img_bytes:
                                    return img_bytes
                except Exception:
                    continue

    # 3. Безотказный резервный генератор (Flux / Pollinations)
    en_prompt = await translate_prompt_for_image(prompt)
    seed = random.randint(1000, 9999999)
    encoded_p = urllib.parse.quote(en_prompt)

    fallback_urls = [
        f"https://image.pollinations.ai/prompt/{encoded_p}?width=1024&height=1024&model=flux&nologo=true&seed={seed}",
        f"https://image.pollinations.ai/prompt/{encoded_p}?width=1024&height=1024&nologo=true&seed={seed}"
    ]

    for fb_url in fallback_urls:
        img_bytes = await download_image_bytes(fb_url)
        if img_bytes:
            return img_bytes

    raise Exception("Все серверы генерации изображений временно перегружены. Попробуйте еще раз через минуту.")


# --- УПРАВЛЕНИЕ ЮЗЕРАМИ ---
def get_user(user_id: int) -> dict:
    now = datetime.now()
    if user_id not in users_db:
        users_db[user_id] = {
            "is_sub": False,
            "sub_end": None,
            "tokens": 0,
            "reset_date": now + timedelta(days=30),
            "current_model": DEFAULT_MODEL,
            "custom_prompt": "",
            "web_search": False,
            "banned": False,
            "strikes": 0,
            "history": [],
            "state": None,
            "school": {
                "active": False,
                "grade": "9 класс",
                "quarter": "1 четверть",
                "level": "Средний",
                "hw_type": "Письменное",
                "topic": "Общая",
                "training_mode": False,
                "training_samples": []
            }
        }
        save_data()

    user = users_db[user_id]

    if user["is_sub"] and user["sub_end"] and now > user["sub_end"]:
        user["is_sub"] = False
        user["sub_end"] = None
        save_data()

    if not user.get("reset_date") or now >= user["reset_date"]:
        user["tokens"] = 0
        user["reset_date"] = now + timedelta(days=30)
        save_data()

    curr = user["current_model"]
    if curr not in models_db or (models_db[curr]["premium"] and not user["is_sub"] and user_id != ADMIN_ID):
        user["current_model"] = DEFAULT_MODEL if DEFAULT_MODEL in models_db else next(iter(models_db))

    return user


# --- ФОРМАТИРОВАНИЕ ТЕКСТА И КОДА ДЛЯ TELEGRAM ---
CODE_BLOCK_RE = re.compile(r"```([a-zA-Z0-9_+-]*)\n?(.*?)```", re.DOTALL)


def extract_code_blocks_and_clean_text(text: str) -> tuple[str, list[tuple[str, str]]]:
    code_blocks = []

    def replacer(match):
        lang = (match.group(1) or "text").strip()
        code = match.group(2).strip("\n")
        if not code.strip():
            return ""
        code_blocks.append((lang, code))
        idx = len(code_blocks)
        return f"\n📌 <i>[Код #{idx} ({lang}) вынесен в следующее сообщение для удобного копирования 👇]</i>\n"

    cleaned_text = CODE_BLOCK_RE.sub(replacer, text)
    return cleaned_text.strip(), code_blocks


def markdown_to_tg_html(text: str) -> str:
    placeholders = {}

    def save_pl(val: str) -> str:
        key = f"@@PL{len(placeholders)}@@"
        placeholders[key] = val
        return key

    text = re.sub(
        r"📌 <i>\[Код #\d+ \([^\)]+\) вынесен в следующее сообщение для удобного копирования 👇\]</i>",
        lambda m: save_pl(m.group(0)),
        text
    )

    def repl_inline(m):
        return save_pl(f"<code>{html.escape(m.group(1))}</code>")

    text = re.sub(r"`([^`\n]+)`", repl_inline, text)
    text = html.escape(text)

    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)

    for k, v in placeholders.items():
        text = text.replace(k, v)

    return text


async def send_smart_ai_response(message: Message, full_text: str):
    main_text, code_blocks = extract_code_blocks_and_clean_text(full_text)

    if main_text:
        html_main = markdown_to_tg_html(main_text)
        for i in range(0, len(html_main), 4000):
            chunk = html_main[i:i + 4000]
            try:
                await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                await message.answer(main_text[i:i + 4000], disable_web_page_preview=True)

    for idx, (lang, code) in enumerate(code_blocks, start=1):
        safe_lang = re.sub(r"[^a-zA-Z0-9_+-]", "", lang) or "python"
        if len(code) <= 3700:
            escaped_code = html.escape(code)
            msg_html = (
                f"💻 <b>Код #{idx} ({safe_lang})</b> — <i>нажмите на блок ниже, чтобы скопировать:</i>\n"
                f"<pre><code class=\"language-{safe_lang}\">{escaped_code}</code></pre>"
            )
            try:
                await message.answer(msg_html, parse_mode="HTML")
            except Exception:
                await message.answer(f"```{safe_lang}\n{code}\n```", parse_mode="Markdown")
        else:
            for part_num, start_pos in enumerate(range(0, len(code), 3600), start=1):
                part_code = html.escape(code[start_pos:start_pos + 3600])
                msg_html = (
                    f"💻 <b>Код #{idx} ({safe_lang}) — Часть {part_num}:</b>\n"
                    f"<pre><code class=\"language-{safe_lang}\">{part_code}</code></pre>"
                )
                try:
                    await message.answer(msg_html, parse_mode="HTML")
                except Exception:
                    await message.answer(code[start_pos:start_pos + 3600])


# --- КЛАВИАТУРЫ ---
def get_main_kb(user_id: int):
    u = get_user(user_id)
    web_icon = "🟢 ВКЛ" if u["web_search"] else "🔴 ВЫКЛ"
    school_icon = "🎓 Школа (Активна)" if u["school"]["active"] else "🎓 Режим Школы"

    kb = [
        [KeyboardButton(text="🧠 Модели ИИ"), KeyboardButton(text="🎨 Генерация фото")],
        [KeyboardButton(text=school_icon), KeyboardButton(text=f"🌐 Поиск: {web_icon}")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⚙️ Свой промпт")],
        [KeyboardButton(text="🧹 Очистить память"), KeyboardButton(text="💎 Подписка / Связь")]
    ]

    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ-панель")])

    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_school_main_kb(u: dict):
    s = u["school"]
    status = "🟢 ВКЛЮЧЕН" if s["active"] else "🔴 ВЫКЛЮЧЕН"

    buttons = [
        [InlineKeyboardButton(text=f"Статус режима: {status}", callback_data="sch_toggle")],
        [
            InlineKeyboardButton(text=f"🏫 Класс: {s['grade']}", callback_data="sch_menu_grade"),
            InlineKeyboardButton(text=f"📅 Четверть: {s['quarter']}", callback_data="sch_menu_quarter")
        ],
        [
            InlineKeyboardButton(text=f"📊 Уровень: {s['level']}", callback_data="sch_menu_level"),
            InlineKeyboardButton(text=f"📝 Д/З: {s['hw_type']}", callback_data="sch_menu_hw")
        ],
        [InlineKeyboardButton(text=f"📌 Тема: {s['topic'][:20]}", callback_data="sch_topic")]
    ]

    if s["hw_type"] == "Письменное":
        train_st = "🟢 ВКЛ" if s["training_mode"] else "🔴 ВЫКЛ"
        cnt = len(s["training_samples"])
        buttons.append([InlineKeyboardButton(text=f"🧠 Режим Обучения: {train_st} (Примеров: {cnt})", callback_data="sch_train_toggle")])
        if cnt > 0:
            buttons.append([InlineKeyboardButton(text="🗑 Очистить примеры", callback_data="sch_train_clear")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm_panel_stats"),
            InlineKeyboardButton(text="🎚 Лимиты токенов", callback_data="adm_panel_limits")
        ],
        [
            InlineKeyboardButton(text="🧠 Управление моделями", callback_data="adm_panel_models"),
            InlineKeyboardButton(text="👤 Управление юзером", callback_data="adm_panel_user")
        ],
        [
            InlineKeyboardButton(text="🎨 Модель фото", callback_data="adm_panel_photomod"),
            InlineKeyboardButton(text="📢 Рассылка всем", callback_data="adm_panel_broadcast")
        ]
    ])


# =========================================================================
# --- МОЩНЫЙ МНОГОУРОВНЕВЫЙ ПОИСК В ИНТЕРНЕТЕ ---
# =========================================================================
async def build_search_query(raw_query: str) -> str:
    """Формирует четкий поисковый запрос из разговорного сообщения пользователя."""
    cleaned = re.sub(r"^(привет|здравствуй|подскажи|расскажи|пожалуйста|найди|поищи|что такое|кто такой)[,\s!]*", "", raw_query.strip(), flags=re.I)
    cleaned = cleaned.strip()[:180]

    # Если запрос короткий и конкретный — не тратим время на переформулировку
    if len(cleaned.split()) <= 6:
        return cleaned or raw_query[:100]

    try:
        today_str = datetime.now().strftime("%Y")
        resp = await asyncio.wait_for(
            create_chat_completion(
                model=DEFAULT_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"Преобразуй сообщение пользователя в короткий, точный поисковый запрос для поисковика (3-7 ключевых слов). "
                            f"Текущий год: {today_str}. Выведи ТОЛЬКО сам поисковый запрос без кавычек и пояснений."
                        )
                    },
                    {"role": "user", "content": raw_query[:400]}
                ],
                max_tokens=40,
                stream=False
            ),
            timeout=4.0
        )
        ans = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
        if ans and len(ans) >= 2:
            return ans[:150]
    except Exception:
        pass

    return cleaned or raw_query[:120]


async def search_internet(user_raw_query: str) -> str:
    """
    Ищет актуальную информацию сразу по нескольким каналам:
    1. Умная формулировка поискового запроса.
    2. DuckDuckGo Search (перебор бэкендов api / html / lite + поиск свежих новостей).
    3. Прямой POST-парсинг DuckDuckGo Lite и HTML (обходит блокировки GET).
    4. Полные выдержки из Википедии (RU + EN).
    """
    if not user_raw_query or not user_raw_query.strip():
        return ""

    search_q = await build_search_query(user_raw_query)
    results_list = []
    seen_titles = set()

    def add_result(title: str, snippet: str, url: str = ""):
        t_clean = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title))).strip()
        s_clean = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", snippet))).strip()
        if not s_clean or t_clean.lower() in seen_titles:
            return
        seen_titles.add(t_clean.lower())
        src = f" ({url})" if url else ""
        results_list.append(f"• {t_clean}: {s_clean}{src}")

    # 1. Поиск через библиотеку DDGS (с перебором всех бэкендов + новости)
    if DDGS_AVAILABLE:
        def fetch_ddgs_all(q: str):
            found = []
            for backend in ("api", "html", "lite"):
                try:
                    with DDGS() as ddgs:
                        res = list(ddgs.text(q, region="ru-ru", safesearch="off", backend=backend, max_results=6))
                        if res:
                            found.extend(res)
                            break
                except Exception:
                    continue
            # Дополнительно проверяем свежие новости
            try:
                with DDGS() as ddgs:
                    news_res = list(ddgs.news(q, region="ru-ru", safesearch="off", max_results=3))
                    for n in news_res or []:
                        found.append({
                            "title": f"[Новости {n.get('date', '')[:10]}] {n.get('title', '')}",
                            "body": n.get("body", ""),
                            "href": n.get("url", "")
                        })
            except Exception:
                pass
            return found

        try:
            ddg_items = await asyncio.to_thread(fetch_ddgs_all, search_q)
            for r in ddg_items or []:
                add_result(r.get("title", ""), r.get("body", ""), r.get("href", ""))
        except Exception:
            pass

    # 2. Прямой HTTP POST к DuckDuckGo HTML / Lite (работает даже при блокировке библиотеки)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        if len(results_list) < 3:
            try:
                async with session.post(
                    "https://html.duckduckgo.com/html/",
                    data={"q": search_q, "kl": "ru-ru"},
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        raw_html = await resp.text()
                        titles = re.findall(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', raw_html, re.DOTALL)
                        snippets = re.findall(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', raw_html, re.DOTALL)
                        for t, s in list(zip(titles, snippets))[:6]:
                            add_result(t, s)
            except Exception:
                pass

        if len(results_list) < 3:
            try:
                async with session.post(
                    "https://lite.duckduckgo.com/lite/",
                    data={"q": search_q, "kl": "ru-ru"},
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        raw_lite = await resp.text()
                        titles = re.findall(r'<a[^>]*class="result-link"[^>]*>(.*?)</a>', raw_lite, re.DOTALL)
                        snippets = re.findall(r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>', raw_lite, re.DOTALL)
                        for t, s in list(zip(titles, snippets))[:6]:
                            add_result(t, s)
            except Exception:
                pass

        # 3. Полные выжимки из Википедии (RU + EN) для точных фактов
        for lang in ("ru", "en"):
            if len(results_list) >= 8:
                break
            try:
                wiki_search_url = (
                    f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search"
                    f"&srsearch={urllib.parse.quote(search_q)}&utf8=&format=json&srlimit=2"
                )
                async with session.get(wiki_search_url, timeout=7) as resp:
                    if resp.status == 200:
                        wdata = await resp.json()
                        search_hits = wdata.get("query", {}).get("search", [])
                        for hit in search_hits:
                            page_title = hit.get("title", "")
                            if not page_title:
                                continue
                            # Запрашиваем развернутый абзац статьи
                            extract_url = (
                                f"https://{lang}.wikipedia.org/w/api.php?action=query&prop=extracts"
                                f"&exintro&explaintext&exchars=600&titles={urllib.parse.quote(page_title)}&format=json"
                            )
                            async with session.get(extract_url, timeout=6) as ex_resp:
                                if ex_resp.status == 200:
                                    ex_data = await ex_resp.json()
                                    pages = ex_data.get("query", {}).get("pages", {})
                                    for p_info in pages.values():
                                        extract_txt = p_info.get("extract", "") or hit.get("snippet", "")
                                        if extract_txt:
                                            add_result(f"[Википедия] {page_title}", extract_txt)
            except Exception:
                pass

    if not results_list:
        return ""

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    return (
        f"📅 Текущая дата и время: {now_str}\n"
        f"🔎 Поисковый запрос: «{search_q}»\n"
        f"🌐 НАЙДЕННЫЕ ДАННЫЕ ИЗ ИНТЕРНЕТА:\n" + "\n".join(results_list[:8])
    )


# --- РАБОТА С ФАЙЛАМИ И ФОТО ---
def is_image_document(message: Message) -> bool:
    """Проверяет, является ли присланный документ изображением без сжатия."""
    if not message.document:
        return False
    mime = (message.document.mime_type or "").lower()
    fname = (message.document.file_name or "").lower()
    return mime.startswith("image/") or fname.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp"))


async def extract_document_text(message: Message) -> str:
    doc = message.document
    if doc.file_size > 4 * 1024 * 1024:
        return "[Файл слишком большой (макс 4 МБ)]"
    file = await bot.get_file(doc.file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    raw_bytes = bio.getvalue()
    for enc in ("utf-8", "utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="ignore")


async def file_id_to_base64(file_id: str) -> str:
    """Скачивает фото из Telegram и кодирует в корректный Data URI base64."""
    file = await bot.get_file(file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    encoded = base64.b64encode(bio.getvalue()).decode("utf-8")

    ext = (file.file_path or "").lower().split(".")[-1]
    mime = "image/png" if ext == "png" else ("image/webp" if ext == "webp" else "image/jpeg")
    return f"data:{mime};base64,{encoded}"


async def extract_message_image_b64(message: Message) -> str | None:
    """Извлекает изображение из сообщения (будь то обычное фото или фото-документ) в оптимальном размере."""
    if message.photo:
        # Выбираем качественное фото, но не превышающее ~1.2 МБ, чтобы прокси не падал с ошибкой 413
        chosen = message.photo[-1]
        for p in reversed(message.photo):
            if not p.file_size or p.file_size <= 1_200_000:
                chosen = p
                break
        return await file_id_to_base64(chosen.file_id)

    if is_image_document(message):
        if message.document.file_size and message.document.file_size <= 5 * 1024 * 1024:
            return await file_id_to_base64(message.document.file_id)

    return None


# Быстрая локальная проверка на запрещенный контент (без задержки API)
BAD_PATTERNS = re.compile(
    r"\b(детск\w+\s+порн|цп|child\s*porn|как\s+сделать\s+бомбу|синтез\s+мефедрон|варить\s+мет)\b",
    re.IGNORECASE
)


def check_censorship_fast(text: str) -> bool:
    if not text:
        return True
    return not bool(BAD_PATTERNS.search(text))


# --- БАЗОВЫЕ КОМАНДЫ ---
@dp.message(CommandStart())
async def start_cmd(message: Message):
    u = get_user(message.from_user.id)
    if u["banned"]:
        return await message.answer("🚫 Вы заблокированы.")
    u["state"] = None
    await message.answer(
        "👋 <b>Добро пожаловать в ШколоAI!</b>\n\n"
        "• 🧠 Задавайте любые вопросы и пишите код\n"
        "• 🖼 Отправляйте фото задач, тестов и конспектов — бот всё распознает и решит\n"
        "• 🌐 Включайте поиск в интернете для актуальной информации\n"
        "• 🎨 Генерируйте изображения через кнопку или команду <code>/imagine запрос</code>",
        reply_markup=get_main_kb(message.from_user.id),
        parse_mode="HTML"
    )


@dp.message(F.text == "🧹 Очистить память")
async def clear_memory(message: Message):
    uid = message.from_user.id
    u = get_user(uid)
    u["history"] = []
    u["state"] = None
    user_image_cache.pop(uid, None)
    save_data()
    await message.answer("🧹 <b>Контекст и память о фото очищены!</b> ИИ готов к новому диалогу.", parse_mode="HTML")


@dp.message(F.text == "👤 Профиль")
@dp.message(Command("stats"))
async def show_profile(message: Message):
    uid = message.from_user.id
    u = get_user(uid)
    limit = bot_config["sub_limit"] if u["is_sub"] else bot_config["free_limit"]
    pct = (u["tokens"] / limit) * 100 if limit > 0 else 100
    st = "💎 Premium" if u["is_sub"] else "🆓 Бесплатно"
    sub_until = f"\n⏳ До: {u['sub_end'].strftime('%d.%m.%Y %H:%M')}" if u["is_sub"] and u["sub_end"] else ""

    kb = None
    if not u["is_sub"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить подписку", callback_data="buy_sub_prompt")]
        ])

    await message.answer(
        f"👤 <b>Профиль</b> (ID: <code>{uid}</code>)\n"
        f"🏷 Статус: {st}{sub_until}\n"
        f"🧠 Модель: {models_db.get(u['current_model'], {}).get('name', u['current_model'])}\n"
        f"🎨 Генерация фото: {'✅ Доступна' if (u['is_sub'] or uid == ADMIN_ID) else '🔒 Только Premium'}\n"
        f"📊 Токены: <code>{u['tokens']} / {limit}</code> ({pct:.1f}%)\n"
        f"🔄 Сброс лимита: {u['reset_date'].strftime('%d.%m.%Y')}",
        reply_markup=kb,
        parse_mode="HTML"
    )


# --- ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ ---
async def run_image_generation(message: Message, uid: int, prompt: str):
    """Выполняет генерацию фото и отправляет результат с кнопкой перегенерации."""
    u = get_user(uid)
    user_last_img_prompt[uid] = prompt

    wait_msg = await message.answer("🎨 <b>Генерирую изображение...</b> Это займет около 10–25 секунд.", parse_mode="HTML")
    try:
        img_bytes = await generate_image_api(prompt)
        try:
            await wait_msg.delete()
        except Exception:
            pass

        caption = f"🎨 <b>Запрос:</b> <i>{html.escape(prompt[:200])}</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Перегенерировать", callback_data="regen_photo"),
                InlineKeyboardButton(text="✨ Новый запрос", callback_data="new_photo_prompt")
            ]
        ])
        photo_file = BufferedInputFile(img_bytes, filename="generated.png")
        await message.answer_photo(photo=photo_file, caption=caption, reply_markup=kb, parse_mode="HTML")
        u["tokens"] += 1000
        save_data()
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка генерации изображения: {e}")


@dp.message(F.text == "🎨 Генерация фото")
@dp.message(Command("imagine"))
async def menu_generate_photo(message: Message):
    uid = message.from_user.id
    u = get_user(uid)

    if not u["is_sub"] and uid != ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Оформить Premium", callback_data="buy_sub_prompt")]
        ])
        return await message.answer(
            "🔒 <b>Генерация изображений доступна только с подпиской Premium!</b>\n\n"
            "Оформите подписку, чтобы создавать любые картинки и арты через нейросеть.",
            reply_markup=kb,
            parse_mode="HTML"
        )

    # Если написали /imagine кот в сапогах — сразу генерируем
    if message.text and message.text.startswith("/imagine"):
        inline_prompt = message.text[len("/imagine"):].strip()
        if inline_prompt:
            return await run_image_generation(message, uid, inline_prompt)

    u["state"] = "wait_image_prompt"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])
    await message.answer(
        "🎨 <b>Режим генерации изображений</b>\n\n"
        "Опишите подробно картинку, которую нужно сгенерировать (на русском или английском языке):",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "regen_photo")
async def cb_regen_photo(callback: CallbackQuery):
    uid = callback.from_user.id
    u = get_user(uid)
    if not u["is_sub"] and uid != ADMIN_ID:
        return await callback.answer("🔒 Нужна подписка Premium!", show_alert=True)

    prompt = user_last_img_prompt.get(uid)
    if not prompt:
        return await callback.answer("Запрос не найден, введите заново.", show_alert=True)

    await callback.answer("🎨 Запускаю повторную генерацию...")
    await run_image_generation(callback.message, uid, prompt)


@dp.callback_query(F.data == "new_photo_prompt")
async def cb_new_photo_prompt(callback: CallbackQuery):
    uid = callback.from_user.id
    u = get_user(uid)
    if not u["is_sub"] and uid != ADMIN_ID:
        return await callback.answer("🔒 Нужна подписка Premium!", show_alert=True)

    u["state"] = "wait_image_prompt"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])
    await callback.message.answer("🎨 <b>Введите новое описание для генерации картинки:</b>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# --- МЕНЮ МОДЕЛЕЙ ---
@dp.message(F.text == "🧠 Модели ИИ")
async def menu_models(message: Message):
    u = get_user(message.from_user.id)
    buttons = []

    for m_id, data in models_db.items():
        prefix = "✅ " if u["current_model"] == m_id else ""
        lock = " 🔒" if data["premium"] and not u["is_sub"] and message.from_user.id != ADMIN_ID else ""
        vis = " 🖼" if data["vision"] else ""
        buttons.append([InlineKeyboardButton(text=f"{prefix}{data['name']}{vis}{lock}", callback_data=f"setmod_{m_id}")])

    await message.answer(
        "🧠 <b>Выберите нейросеть:</b>\n<i>(🖼 — понимает фото, 🔒 — нужна подписка Premium)</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("setmod_"))
async def cb_set_model(callback: CallbackQuery):
    m_id = callback.data.replace("setmod_", "")
    if m_id not in models_db:
        return await callback.answer("Модель не найдена!", show_alert=True)

    u = get_user(callback.from_user.id)
    if models_db[m_id]["premium"] and not u["is_sub"] and callback.from_user.id != ADMIN_ID:
        return await callback.answer("🔒 Для этой модели нужна подписка Premium!", show_alert=True)

    u["current_model"] = m_id
    save_data()
    await callback.message.edit_text(f"✅ Выбрана модель: <b>{models_db[m_id]['name']}</b>", parse_mode="HTML")


# --- СВОЙ ПРОМПТ ---
@dp.message(F.text == "⚙️ Свой промпт")
async def menu_custom_prompt(message: Message):
    u = get_user(message.from_user.id)
    u["state"] = "wait_prompt"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Сбросить промпт", callback_data="reset_prompt")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])

    current_prompt = html.escape(u["custom_prompt"]) if u["custom_prompt"] else "Стандартный"
    await message.answer(
        f"⚙️ <b>Ваш текущий промпт:</b> <i>{current_prompt}</i>\n\n"
        f"Напишите в чат новый системный промпт:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "reset_prompt")
async def cb_reset_prompt(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    u["custom_prompt"] = ""
    u["state"] = None
    save_data()
    await callback.message.edit_text("✅ Промпт сброшен.")


@dp.callback_query(F.data == "cancel_state")
async def cb_cancel_state(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    u["state"] = None
    if callback.from_user.id == ADMIN_ID:
        admin_state.pop(ADMIN_ID, None)
    await callback.message.edit_text("❌ Действие отменено.")


# --- ПОДПИСКА И СВЯЗЬ ---
@dp.message(F.text == "💎 Подписка / Связь")
async def menu_subscription(message: Message):
    u = get_user(message.from_user.id)
    u["state"] = "support_chat"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])

    await message.answer(
        f"💎 <b>Подписка Premium</b>\n"
        f"• Лимит: <code>{bot_config['sub_limit']}</code> токенов\n"
        f"• Доступ ко всем топовым моделям ИИ\n"
        f"• Доступ к генерации изображений 🎨\n\n"
        f"✍️ Напишите любое сообщение ниже, и оно сразу уйдет администратору:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "buy_sub_prompt")
async def cb_buy_sub_prompt(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    u["state"] = "support_chat"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])

    await callback.message.answer("✍️ <b>Напишите сообщение администратору для оформления Premium:</b>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# =========================================================================
# --- АДМИН-ПАНЕЛЬ ---
# =========================================================================
@dp.message(F.text == "👑 Админ-панель")
@dp.message(Command("admin"))
async def admin_panel_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    admin_state.pop(ADMIN_ID, None)
    await message.answer(
        "👑 <b>Панель управления ботом</b>\nВыберите нужный раздел:",
        reply_markup=get_admin_main_kb(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_back_main")
async def cb_adm_back_main(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    admin_state.pop(ADMIN_ID, None)
    await callback.message.edit_text(
        "👑 <b>Панель управления ботом</b>\nВыберите нужный раздел:",
        reply_markup=get_admin_main_kb(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_panel_stats")
async def cb_adm_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    total_u = len(users_db)
    sub_u = sum(1 for v in users_db.values() if v["is_sub"])
    ban_u = sum(1 for v in users_db.values() if v["banned"])
    total_tokens = sum(v["tokens"] for v in users_db.values())

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back_main")]
    ])
    await callback.message.edit_text(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Всего пользователей: <code>{total_u}</code>\n"
        f"💎 С подпиской Premium: <code>{sub_u}</code>\n"
        f"🚫 Заблокировано: <code>{ban_u}</code>\n"
        f"🔥 Потрачено токенов: <code>{total_tokens}</code>\n\n"
        f"🆓 Бесплатный лимит: <code>{bot_config['free_limit']}</code>\n"
        f"💎 Premium лимит: <code>{bot_config['sub_limit']}</code>\n"
        f"🎨 Модель генерации фото: <code>{bot_config['photo_model']}</code>",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_panel_limits")
async def cb_adm_limits(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✏️ Изменить FREE лимит ({bot_config['free_limit']})", callback_data="adm_edit_free_lim")],
        [InlineKeyboardButton(text=f"✏️ Изменить SUB лимит ({bot_config['sub_limit']})", callback_data="adm_edit_sub_lim")],
        [InlineKeyboardButton(text="🔄 Сбросить токены ВСЕМ юзерам", callback_data="adm_reset_all_tokens")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back_main")]
    ])
    await callback.message.edit_text(
        "🎚 <b>Управление лимитами токенов</b>\nВыберите параметр для изменения:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data.in_({"adm_edit_free_lim", "adm_edit_sub_lim", "adm_reset_all_tokens"}))
async def cb_adm_limits_action(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    if callback.data == "adm_reset_all_tokens":
        for u in users_db.values():
            u["tokens"] = 0
        save_data()
        await callback.answer("✅ Токены всех пользователей сброшены!", show_alert=True)
        return await cb_adm_limits(callback)

    admin_state[ADMIN_ID] = {"action": callback.data}
    label = "бесплатного (FREE)" if callback.data == "adm_edit_free_lim" else "подписочного (SUB)"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back_main")]])
    await callback.message.edit_text(f"🔢 Введите новое числовое значение для <b>{label}</b> лимита:", reply_markup=kb, parse_mode="HTML")


def render_admin_models_kb():
    buttons = []
    for m_id, info in models_db.items():
        prem = "💎" if info["premium"] else "🆓"
        vis = "🖼" if info["vision"] else "📝"
        buttons.append([
            InlineKeyboardButton(text=f"{info['name']} ({prem}/{vis})", callback_data=f"adm_mod_open_{m_id}")
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить новую модель", callback_data="adm_mod_add")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "adm_panel_models")
async def cb_adm_models(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.edit_text(
        "🧠 <b>Управление моделями ИИ</b>\nНажмите на модель, чтобы изменить её статус (Free/Premium, Vision) или удалить:",
        reply_markup=render_admin_models_kb(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("adm_mod_open_"))
async def cb_adm_model_detail(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    m_id = callback.data.replace("adm_mod_open_", "")
    if m_id not in models_db:
        return await cb_adm_models(callback)

    info = models_db[m_id]
    prem_txt = "💎 Только Premium" if info["premium"] else "🆓 Доступна всем"
    vis_txt = "🖼 Да (Vision)" if info["vision"] else "❌ Нет"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Доступ: {prem_txt} (Переключить)", callback_data=f"adm_mod_tprem_{m_id}")],
        [InlineKeyboardButton(text=f"Фото: {vis_txt} (Переключить)", callback_data=f"adm_mod_tvis_{m_id}")],
        [InlineKeyboardButton(text="🗑 Удалить модель", callback_data=f"adm_mod_del_{m_id}")],
        [InlineKeyboardButton(text="🔙 К списку моделей", callback_data="adm_panel_models")]
    ])
    await callback.message.edit_text(
        f"⚙️ <b>Настройка модели</b>\n"
        f"ID: <code>{m_id}</code>\n"
        f"Название: <b>{info['name']}</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("adm_mod_tprem_") | F.data.startswith("adm_mod_tvis_") | F.data.startswith("adm_mod_del_"))
async def cb_adm_model_modify(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    data = callback.data
    if data.startswith("adm_mod_tprem_"):
        m_id = data.replace("adm_mod_tprem_", "")
        if m_id in models_db:
            models_db[m_id]["premium"] = not models_db[m_id]["premium"]
            save_data()
        return await cb_adm_model_detail(callback)

    elif data.startswith("adm_mod_tvis_"):
        m_id = data.replace("adm_mod_tvis_", "")
        if m_id in models_db:
            models_db[m_id]["vision"] = not models_db[m_id]["vision"]
            save_data()
        return await cb_adm_model_detail(callback)

    elif data.startswith("adm_mod_del_"):
        m_id = data.replace("adm_mod_del_", "")
        if len(models_db) <= 1:
            return await callback.answer("Нельзя удалить последнюю модель!", show_alert=True)
        models_db.pop(m_id, None)
        save_data()
        await callback.answer("Модель удалена!")
        return await cb_adm_models(callback)


@dp.callback_query(F.data == "adm_mod_add")
async def cb_adm_model_add(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    admin_state[ADMIN_ID] = {"action": "add_model"}
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_panel_models")]])
    await callback.message.edit_text(
        "➕ <b>Добавление новой модели</b>\n\n"
        "Отправьте данные в формате:\n"
        "<code>id_модели | Название | premium(1 или 0) | vision(1 или 0)</code>\n\n"
        "Пример:\n<code>gpt-5-turbo | GPT-5 Turbo | 1 | 1</code>",
        reply_markup=kb,
        parse_mode="HTML"
    )


def render_user_manage_card(tid: int) -> tuple[str, InlineKeyboardMarkup]:
    u = get_user(tid)
    sub_str = f"💎 Активна (до {u['sub_end'].strftime('%d.%m.%Y')})" if (u["is_sub"] and u["sub_end"]) else ("💎 Активна" if u["is_sub"] else "🆓 Нет")
    ban_str = "🚫 Забанен" if u["banned"] else "✅ Активен"
    text = (
        f"👤 <b>Карточка пользователя</b> <code>{tid}</code>\n\n"
        f"Статус: {ban_str}\n"
        f"Подписка: {sub_str}\n"
        f"Токены: <code>{u['tokens']}</code>\n"
        f"Текущая модель: <code>{u['current_model']}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💎 +30 дней Sub", callback_data=f"adm_u_sub30_{tid}"),
            InlineKeyboardButton(text="❌ Забрать Sub", callback_data=f"adm_u_unsub_{tid}")
        ],
        [
            InlineKeyboardButton(text="🚫 Бан / Разбан", callback_data=f"adm_u_ban_{tid}"),
            InlineKeyboardButton(text="🔄 Сброс токенов", callback_data=f"adm_u_rst_{tid}")
        ],
        [InlineKeyboardButton(text="✉️ Написать пользователю", callback_data=f"adm_reply_{tid}")],
        [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="adm_back_main")]
    ])
    return text, kb


@dp.callback_query(F.data == "adm_panel_user")
async def cb_adm_panel_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    admin_state[ADMIN_ID] = {"action": "find_user"}
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back_main")]])
    await callback.message.edit_text("🔍 Введите <b>Telegram ID</b> пользователя для управления:", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_u_"))
async def cb_adm_user_card_actions(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    parts = callback.data.split("_")
    act = parts[2]
    tid = int(parts[3])
    u = get_user(tid)

    if act == "sub30":
        u["is_sub"] = True
        u["sub_end"] = datetime.now() + timedelta(days=30)
        await callback.answer("Выдана подписка на 30 дней!")
        try:
            await bot.send_message(tid, "🎉 Вам выдана подписка <b>Premium</b> на 30 дней!", parse_mode="HTML")
        except Exception:
            pass
    elif act == "unsub":
        u["is_sub"] = False
        u["sub_end"] = None
        await callback.answer("Подписка отключена!")
    elif act == "ban":
        u["banned"] = not u["banned"]
        await callback.answer("Статус бана изменен!")
    elif act == "rst":
        u["tokens"] = 0
        await callback.answer("Токены пользователя обнулены!")

    save_data()
    text, kb = render_user_manage_card(tid)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_panel_photomod")
async def cb_adm_photomod(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    admin_state[ADMIN_ID] = {"action": "set_photo_model"}
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back_main")]])
    await callback.message.edit_text(
        f"🎨 Текущая модель для генерации изображений: <code>{bot_config['photo_model']}</code>\n\n"
        f"Введите название новой модели (например: <code>dall-e-3</code>, <code>flux</code>, <code>midjourney</code>):",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_panel_broadcast")
async def cb_adm_broadcast(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    admin_state[ADMIN_ID] = {"action": "broadcast"}
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back_main")]])
    await callback.message.edit_text("📢 Введите текст рассылки, который получат все пользователи бота:", reply_markup=kb, parse_mode="HTML")


@dp.message(Command("give_sub"))
async def cmd_give_sub(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        _, tid, days = message.text.split()
        tid, days = int(tid), int(days)
        u = get_user(tid)
        u["is_sub"] = True
        u["sub_end"] = datetime.now() + timedelta(days=days)
        save_data()
        await message.answer(f"✅ Выдано <code>{tid}</code> на {days} дней.", parse_mode="HTML")
        try:
            await bot.send_message(tid, f"🎉 Вам выдана подписка Premium на {days} дней!")
        except Exception:
            pass
    except Exception:
        await message.answer("Формат: <code>/give_sub ID 30</code>", parse_mode="HTML")


@dp.message(Command("ban"))
async def cmd_ban_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        tid = int(message.text.split()[1])
        u = get_user(tid)
        u["banned"] = not u["banned"]
        save_data()
        await message.answer(f"✅ Статус бана <code>{tid}</code> изменен.", parse_mode="HTML")
    except Exception:
        await message.answer("Формат: <code>/ban ID</code>", parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_ban_") | F.data.startswith("adm_sub_") | F.data.startswith("adm_reply_"))
async def cb_admin_quick_actions(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    data = callback.data
    if data.startswith("adm_ban_"):
        tid = int(data.replace("adm_ban_", ""))
        u = get_user(tid)
        u["banned"] = True
        save_data()
        await callback.answer("Пользователь забанен!")
    elif data.startswith("adm_sub_"):
        tid = int(data.replace("adm_sub_", ""))
        u = get_user(tid)
        u["is_sub"] = True
        u["sub_end"] = datetime.now() + timedelta(days=30)
        save_data()
        await callback.answer("Подписка выдана!")
        try:
            await bot.send_message(tid, "🎉 Вам выдана подписка Premium на 30 дней!")
        except Exception:
            pass
    elif data.startswith("adm_reply_"):
        tid = int(data.replace("adm_reply_", ""))
        admin_reply_target[ADMIN_ID] = tid
        await callback.message.answer(f"✍️ Напишите ответ для <code>{tid}</code>:", parse_mode="HTML")
        await callback.answer()


# --- РЕЖИМ ШКОЛЫ ---
@dp.message(F.text.startswith("🎓"))
async def menu_school(message: Message):
    u = get_user(message.from_user.id)
    await message.answer(
        "🎓 <b>Режим Школа</b>",
        reply_markup=get_school_main_kb(u),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("sch_menu_"))
async def cb_school_menus(callback: CallbackQuery):
    action = callback.data.replace("sch_menu_", "")
    buttons = []

    if action == "grade":
        row = []
        for i in range(1, 12):
            row.append(InlineKeyboardButton(text=f"{i} кл", callback_data=f"sch_set_grade_{i} класс"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
    elif action == "quarter":
        for i in range(1, 5):
            buttons.append([InlineKeyboardButton(text=f"{i} четверть", callback_data=f"sch_set_quarter_{i} четверть")])
    elif action == "level":
        levels = ["Начальный (просто)", "Средний", "Продвинутый (Олимпиада)"]
        for l in levels:
            buttons.append([InlineKeyboardButton(text=l, callback_data=f"sch_set_level_{l}")])
    elif action == "hw":
        hws = ["Письменное", "Проект", "Творческое", "Пересказ"]
        for h in hws:
            buttons.append([InlineKeyboardButton(text=h, callback_data=f"sch_set_hw_{h}")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sch_back_main")])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("sch_set_"))
async def cb_school_set(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    param, val = callback.data.replace("sch_set_", "").split("_", 1)
    if param == "hw":
        u["school"]["hw_type"] = val
    elif param in ["grade", "quarter", "level"]:
        u["school"][param] = val
    save_data()
    await callback.message.edit_reply_markup(reply_markup=get_school_main_kb(u))


@dp.callback_query(F.data == "sch_back_main")
async def cb_school_back(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    await callback.message.edit_reply_markup(reply_markup=get_school_main_kb(u))


@dp.callback_query(F.data.startswith("sch_"))
async def cb_school_others(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    if callback.data == "sch_toggle":
        u["school"]["active"] = not u["school"]["active"]
    elif callback.data == "sch_topic":
        u["state"] = "school_topic"
        return await callback.message.answer("📌 Введите название темы:")
    elif callback.data == "sch_train_toggle":
        u["school"]["training_mode"] = not u["school"]["training_mode"]
    elif callback.data == "sch_train_clear":
        u["school"]["training_samples"].clear()

    save_data()
    await callback.message.edit_reply_markup(reply_markup=get_school_main_kb(u))


# --- ПЕРЕКЛЮЧАТЕЛЬ ПОИСКА ---
@dp.message(F.text.startswith("🌐 Поиск:"))
async def toggle_web(message: Message):
    u = get_user(message.from_user.id)
    u["web_search"] = not u["web_search"]
    save_data()
    status_text = "🟢 ВКЛ" if u["web_search"] else "🔴 ВЫКЛ"
    await message.answer(f"🌐 Поиск в интернете: <b>{status_text}</b>", reply_markup=get_main_kb(message.from_user.id), parse_mode="HTML")


# =========================================================================
# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ, ФОТО И ФАЙЛОВ ---
# =========================================================================
@dp.message(F.text | F.photo | F.document)
async def handle_requests(message: Message):
    uid = message.from_user.id
    u = get_user(uid)

    if u["banned"]:
        return

    # 1. Обработка состояний Админ-панели
    if uid == ADMIN_ID and ADMIN_ID in admin_state and message.text:
        st = admin_state.pop(ADMIN_ID)
        act = st.get("action")

        if act == "adm_edit_free_lim":
            if message.text.isdigit():
                bot_config["free_limit"] = int(message.text)
                save_data()
                return await message.answer(f"✅ Бесплатный лимит изменен на <code>{bot_config['free_limit']}</code>", reply_markup=get_admin_main_kb(), parse_mode="HTML")
            return await message.answer("❌ Введите число.")

        elif act == "adm_edit_sub_lim":
            if message.text.isdigit():
                bot_config["sub_limit"] = int(message.text)
                save_data()
                return await message.answer(f"✅ Лимит подписки изменен на <code>{bot_config['sub_limit']}</code>", reply_markup=get_admin_main_kb(), parse_mode="HTML")
            return await message.answer("❌ Введите число.")

        elif act == "find_user":
            if message.text.strip().isdigit():
                target_id = int(message.text.strip())
                text, kb = render_user_manage_card(target_id)
                return await message.answer(text, reply_markup=kb, parse_mode="HTML")
            return await message.answer("❌ ID должен состоять из цифр.")

        elif act == "add_model":
            try:
                parts = [p.strip() for p in message.text.split("|")]
                m_id, m_name, is_prem, is_vis = parts[0], parts[1], parts[2] == "1", parts[3] == "1"
                models_db[m_id] = {"name": m_name, "premium": is_prem, "vision": is_vis}
                save_data()
                return await message.answer(f"✅ Модель <b>{m_name}</b> (<code>{m_id}</code>) успешно добавлена!", reply_markup=get_admin_main_kb(), parse_mode="HTML")
            except Exception:
                return await message.answer("❌ Ошибка формата. Пример:\n<code>gpt-5 | GPT-5 | 1 | 1</code>", parse_mode="HTML")

        elif act == "set_photo_model":
            bot_config["photo_model"] = message.text.strip()
            save_data()
            return await message.answer(f"✅ Модель для генерации фото изменена на: <code>{bot_config['photo_model']}</code>", reply_markup=get_admin_main_kb(), parse_mode="HTML")

        elif act == "broadcast":
            sent = 0
            for target_uid in list(users_db.keys()):
                try:
                    await bot.send_message(target_uid, f"📢 <b>Объявление:</b>\n\n{message.text}", parse_mode="HTML")
                    sent += 1
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
            return await message.answer(f"✅ Рассылка завершена! Доставлено: {sent} юзерам.", reply_markup=get_admin_main_kb())

    # 2. Ответ админа конкретному юзеру
    if uid == ADMIN_ID and ADMIN_ID in admin_reply_target and message.text:
        target_id = admin_reply_target.pop(ADMIN_ID)
        try:
            await bot.send_message(target_id, f"👨‍💻 <b>Сообщение от администратора:</b>\n\n{html.escape(message.text)}", parse_mode="HTML")
            return await message.answer("✅ Ответ доставлен.")
        except Exception as e:
            return await message.answer(f"❌ Ошибка отправки: {e}")

    # 3. Чат поддержки
    if u["state"] == "support_chat" and message.text:
        u["state"] = None
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✍️ Ответить", callback_data=f"adm_reply_{uid}"),
                InlineKeyboardButton(text="💎 Выдать Sub (30д)", callback_data=f"adm_sub_{uid}")
            ],
            [InlineKeyboardButton(text="🚫 Бан", callback_data=f"adm_ban_{uid}")]
        ])
        await bot.send_message(ADMIN_ID, f"📩 <b>Обращение от <code>{uid}</code></b>:\n\n{html.escape(message.text)}", reply_markup=kb, parse_mode="HTML")
        return await message.answer("✅ Сообщение отправлено администратору!", reply_markup=get_main_kb(uid))

    # 4. Генерация фото (из состояния ожидания промпта)
    if u["state"] == "wait_image_prompt" and message.text:
        u["state"] = None
        if not u["is_sub"] and uid != ADMIN_ID:
            return await message.answer("🔒 Генерация изображений доступна только с подпиской Premium!")
        return await run_image_generation(message, uid, message.text)

    # 5. Свой промпт
    if u["state"] == "wait_prompt" and message.text:
        if not check_censorship_fast(message.text):
            return await message.answer("❌ Промпт содержит запрещенные слова.")
        u["custom_prompt"] = message.text[:400]
        u["state"] = None
        save_data()
        return await message.answer("✅ Промпт сохранен!")

    # 6. Тема школы
    if u["state"] == "school_topic" and message.text:
        u["school"]["topic"] = message.text[:50]
        u["state"] = None
        save_data()
        return await message.answer(f"✅ Тема Школы: {u['school']['topic']}", reply_markup=get_school_main_kb(u))

    # Анти-спам
    now = datetime.now()
    if uid in anti_spam and (now - anti_spam[uid]).total_seconds() < 2.0:
        return await message.answer("⏳ Пожалуйста, не спамьте. Подождите пару секунд.")
    anti_spam[uid] = now

    # Проверка лимитов токенов
    limit = bot_config["sub_limit"] if u["is_sub"] else bot_config["free_limit"]
    if u["tokens"] >= limit and uid != ADMIN_ID:
        return await message.answer("❌ Лимит токенов исчерпан. Оформите подписку Premium для продолжения.")

    # Определяем, прислано ли изображение (как фото или как файл-картинка)
    image_b64 = await extract_message_image_b64(message)
    if image_b64:
        user_image_cache[uid] = {"b64": image_b64, "time": now}

    raw_text = (message.text or message.caption or "").strip()

    # Если прислан текстовый документ (не картинка)
    if message.document and not is_image_document(message):
        doc_content = await extract_document_text(message)
        raw_text += f"\n\n[Содержимое файла {message.document.file_name}]:\n{doc_content[:6000]}"

    # Если пользователь в текстовом сообщении ссылается на только что отправленное фото
    if not image_b64 and uid in user_image_cache:
        cached = user_image_cache[uid]
        if (now - cached["time"]).total_seconds() < 600 and re.search(r"\b(на фото|с фото|на картинке|на скрине|в этом задании|номер \d|пункт \d|задач[ауи] \d)\b", raw_text.lower()):
            image_b64 = cached["b64"]

    # Режим обучения в Школе
    s = u["school"]
    if s["active"] and s["hw_type"] == "Письменное" and s["training_mode"]:
        if image_b64:
            wait_train = await message.answer("🖼 <b>Анализирую пример решения с фото...</b>", parse_mode="HTML")
            try:
                ans = await create_vision_completion(
                    preferred_model=u["current_model"],
                    sys_prompt="Ты учитель. Внимательно перепиши условие, алгоритм и стиль оформления решения с этого фото.",
                    history=[],
                    user_text="Разбери пошагово алгоритм и оформление решения с этого фото.",
                    b64_image=image_b64
                )
                s["training_samples"].append(f"[Пример с фото]: {ans}")
                u["tokens"] += 300
                save_data()
                await wait_train.delete()
            except Exception as e:
                return await wait_train.edit_text(f"❌ Ошибка распознавания: {e}")
        else:
            s["training_samples"].append(f"[Пример текста]: {raw_text}")
            save_data()

        return await message.answer(
            f"🧠 Пример #{len(s['training_samples'])} сохранен! Отключите Режим Обучения, когда будете готовы решать ДЗ.",
            reply_markup=get_school_main_kb(u)
        )

    # Быстрая цензура
    if not check_censorship_fast(raw_text):
        u["strikes"] += 1
        save_data()
        return await message.answer("🚫 Запрос нарушает правила безопасности.")

    # Кубик Школы
    dice_style = ""
    if s["active"] and s["hw_type"] in ["Проект", "Творческое", "Пересказ"]:
        dm = await message.answer_dice("🎲")
        await asyncio.sleep(2.0)
        styles = {
            1: "Строго научно",
            2: "Креативно с примерами",
            3: "С тезисами и планом",
            4: "Сторителлинг",
            5: "Аналитически за и против",
            6: "Подробно на 5+"
        }
        dice_style = styles.get(dm.dice.value, "")
        await message.answer(f"🎲 Выпало {dm.dice.value}! Стиль: {dice_style}")

    # Сборка системного промпта
    today_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    sys_prompt = f"{BASE_SYSTEM_PROMPT}\nТекущая дата и время: {today_str}."

    if u["custom_prompt"]:
        sys_prompt += f"\nДополнительная инструкция пользователя: {u['custom_prompt']}"

    if s["active"]:
        sys_prompt += f"\n[РЕЖИМ ШКОЛА]: Ученик {s['grade']}, {s['quarter']}, уровень знаний: {s['level']}. Тема: {s['topic']}. Тип работы: {s['hw_type']}."
        if dice_style:
            sys_prompt += f" Стиль выполнения: {dice_style}."

        if s["hw_type"] == "Письменное" and s["training_samples"]:
            sys_prompt += "\nОформляй решение СТРОГО по этим примерам учителя:\n" + "\n".join(s["training_samples"][-4:])

    temp = await message.answer("🖼 Анализирую изображение..." if image_b64 else "⏳ Думаю...")

    # Поиск в интернете (если включен)
    web_context = ""
    if u["web_search"] and raw_text:
        try:
            await temp.edit_text("🌐 Ищу актуальную информацию в интернете...")
        except Exception:
            pass
        web_context = await search_internet(raw_text)
        if web_context:
            sys_prompt += (
                "\n\n[ВАЖНО: У ТЕБЯ ЕСТЬ ДОСТУП К АКТУАЛЬНОМУ ИНТЕРНЕТУ!]\n"
                "Ниже приведены свежие результаты поиска по запросу пользователя. "
                "Используй эти факты, цифры и даты как основной источник правды. "
                "Никогда не пиши, что у тебя нет доступа к интернету или данным в реальном времени."
            )

    try:
        # ВЕТКА 1: ЗАПРОС С ИЗОБРАЖЕНИЕМ (VISION)
        if image_b64:
            final_user_text = raw_text
            if web_context:
                final_user_text = f"{raw_text}\n\n[Данные из интернета]:\n{web_context}"

            resp_text = await create_vision_completion(
                preferred_model=u["current_model"],
                sys_prompt=sys_prompt,
                history=u["history"],
                user_text=final_user_text,
                b64_image=image_b64
            )
            try:
                await temp.delete()
            except Exception:
                pass

            await send_smart_ai_response(message, resp_text)

            # Сохраняем содержательную историю, чтобы следующие вопросы по фото имели полный контекст
            u["history"].append({
                "role": "user",
                "content": f"[Пользователь прислал изображение. Подпись: {raw_text or 'без подписи'}]"
            })
            u["history"].append({"role": "assistant", "content": resp_text})

        # ВЕТКА 2: ТЕКСТОВЫЙ ЗАПРОС (С ПОДДЕРЖКОЙ СТРИМИНГА И АВТО-ФОЛЛБЭКА НА ОБЫЧНЫЙ ЗАПРОС)
        else:
            messages_payload = [{"role": "system", "content": sys_prompt}]
            for msg in u["history"][-10:]:
                messages_payload.append(msg)

            user_content = raw_text
            if web_context:
                user_content = (
                    f"{web_context}\n\n"
                    f"---\nИспользуя найденную выше информацию из интернета, ответь на вопрос пользователя:\n{raw_text}"
                )

            messages_payload.append({"role": "user", "content": user_content})

            resp_text = ""
            last_ed = time.time()
            active_model = u["current_model"]

            try:
                stream = await create_chat_completion(
                    model=active_model,
                    messages=messages_payload,
                    stream=True
                )
                async for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta:
                        delta = chunk.choices[0].delta
                        piece = getattr(delta, "content", None) or ""
                        resp_text += piece
                        if time.time() - last_ed > 1.4 and resp_text.strip():
                            try:
                                await temp.edit_text(resp_text[:3900] + " ✍️")
                                last_ed = time.time()
                            except Exception:
                                pass
            except Exception:
                # Если прокси не поддержал stream=True для данной модели — выполняем обычный запрос
                resp_text = ""

            if not resp_text.strip():
                resp = await create_chat_completion(
                    model=active_model,
                    messages=messages_payload,
                    stream=False
                )
                resp_text = (resp.choices[0].message.content or "").strip()

            try:
                await temp.delete()
            except Exception:
                pass

            if not resp_text:
                resp_text = "⚠️ Модель вернула пустой ответ. Попробуйте переформулировать запрос или сменить модель."

            await send_smart_ai_response(message, resp_text)

            u["history"].append({"role": "user", "content": raw_text})
            u["history"].append({"role": "assistant", "content": resp_text})

        if len(u["history"]) > 12:
            u["history"] = u["history"][-12:]

        u["tokens"] += max(50, len(resp_text) // 3)
        save_data()

    except Exception as e:
        await temp.edit_text(f"❌ Ошибка API: {str(e)[:300]}")


async def main():
    load_data()
    print("✅ Бот запущен и готов к работе!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
