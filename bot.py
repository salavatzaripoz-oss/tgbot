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
