import os
import io
import re
import html
import base64
import time
import asyncio
import urllib.parse
from datetime import datetime, timedelta

from dotenv import load_dotenv
load_dotenv()

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, 
    BufferedInputFile, WebAppInfo, MenuButtonWebApp
)
from openai import AsyncOpenAI

# Поддержка поиска в интернете
try:
    from duckduckgo_search import DDGS
    DDGS_AVAILABLE = True
except ImportError:
    DDGS_AVAILABLE = False

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8130566129:AAHGfVKiIorLhqGAU_HtbCPjSyXQlWgbzVw").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5656256968").strip())

AI_API_KEY = os.getenv("AI_API_KEY", "sk-eF2ZQEE1uyLh6VRq1x26zryHbbHqKT7plceJQ37U3Au1IrHm").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://anymodel.ghostly.su/v1").strip()

# Ключ и ссылка для генерации изображений
PHOTO_GENERATIONAL_KEY = os.getenv("PHOTO_GENERATIONAL_KEY", AI_API_KEY).strip()
PHOTO_BASE_URL = os.getenv("PHOTO_BASE_URL", "https://api.podpiski-neiroseti.shop").strip()
PHOTO_MODEL = os.getenv("PHOTO_MODEL", "dall-e-3").strip()

# Настройки Web App (Telegram Mini App)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://shkolo-ai.loca.lt").strip()
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080"))

# Динамические настройки (можно менять прямо из Админ-панели)
bot_config = {
    "free_limit": int(os.getenv("FREE_LIMIT", "45000").strip()),
    "sub_limit": int(os.getenv("SUB_LIMIT", "1000000").strip()),
    "photo_model": PHOTO_MODEL
}

BASE_SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты полезный ИИ-ассистент в Telegram-боте. Отвечай четко, структурированно и по делу.\n"
    "ВАЖНЫЕ ПРАВИЛА ФОРМАТИРОВАНИЯ ДЛЯ TELEGRAM:\n"
    "1. Используй **жирный текст** для важного и `моноширинный` для коротких команд.\n"
    "2. Если пишешь программный код, ОБЯЗАТЕЛЬНО оборачивай его в тройные кавычки с указанием языка, например:\n"
    "```python\nкод\n```\n"
    "Так пользователь сможет скопировать код в один клик."
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- УМНАЯ РАБОТА С URL ---
def get_url_variants(raw_url: str) -> list[str]:
    clean = raw_url.strip().rstrip("/")
    if clean.endswith("/v1"):
        base_no_v1 = clean[:-3].rstrip("/")
        return [clean, base_no_v1]
    else:
        return [f"{clean}/v1", clean]

AI_URL_VARIANTS = get_url_variants(AI_BASE_URL)
PHOTO_URL_VARIANTS = get_url_variants(PHOTO_BASE_URL) + get_url_variants(AI_BASE_URL)

ai_clients = [AsyncOpenAI(api_key=AI_API_KEY, base_url=u) for u in AI_URL_VARIANTS]

async def create_chat_completion(**kwargs):
    last_err = None
    for client in ai_clients:
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as e:
            last_err = e
    raise last_err

# ================= НАДЁЖНАЯ ГЕНЕРАЦИЯ ФОТО =================
async def generate_image_api(prompt: str) -> tuple[bytes | str, bool]:
    """
    Генерирует фото через OpenAI SDK и через прямые HTTP-запросы с фоллбэком 
    на оба домена (основной и фото), с отключенной блокировкой SSL.
    """
    last_err = None

    # 1. Пробуем через AsyncOpenAI по всем адресам
    for base_u in PHOTO_URL_VARIANTS:
        try:
            c = AsyncOpenAI(api_key=PHOTO_GENERATIONAL_KEY, base_url=base_u)
            resp = await c.images.generations.create(
                model=bot_config["photo_model"],
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            item = resp.data[0]
            if getattr(item, "b64_json", None):
                return base64.b64decode(item.b64_json), True
            if getattr(item, "url", None):
                return item.url, False
        except Exception as e:
            last_err = e

    # 2. Резервный прямой HTTP-запрос
    headers = {
        "Authorization": f"Bearer {PHOTO_GENERATIONAL_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": bot_config["photo_model"],
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024"
    }

    connector = aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(connector=connector) as session:
        for base_u in PHOTO_URL_VARIANTS:
            clean_u = base_u.rstrip("/")
            endpoints = [f"{clean_u}/images/generations"]
            if not clean_u.endswith("/v1"):
                endpoints.append(f"{clean_u}/v1/images/generations")

            for ep in endpoints:
                try:
                    async with session.post(ep, json=payload, headers=headers, timeout=60) as r:
                        if r.status == 200:
                            data = await r.json()
                            if "data" in data and len(data["data"]) > 0:
                                first = data["data"][0]
                                if "b64_json" in first and first["b64_json"]:
                                    return base64.b64decode(first["b64_json"]), True
                                if "url" in first and first["url"]:
                                    return first["url"], False
                            elif "url" in data:
                                return data["url"], False
                        else:
                            err_txt = await r.text()
                            last_err = Exception(f"HTTP {r.status} ({ep}): {err_txt[:150]}")
                except Exception as e:
                    last_err = e

    raise last_err or Exception("Не удалось сгенерировать изображение. Проверьте модель и баланс ключа.")

# ================= БАЗА МОДЕЛЕЙ =================
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
anti_spam = {}
admin_reply_target = {}
admin_state = {}

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
    user = users_db[user_id]

    if user["is_sub"] and user["sub_end"] and now > user["sub_end"]:
        user["is_sub"] = False
        user["sub_end"] = None

    if now >= user["reset_date"]:
        user["tokens"] = 0
        user["reset_date"] = now + timedelta(days=30)

    curr = user["current_model"]
    if curr not in models_db or (models_db[curr]["premium"] and not user["is_sub"] and user_id != ADMIN_ID):
        user["current_model"] = DEFAULT_MODEL if DEFAULT_MODEL in models_db else next(iter(models_db))

    return user

# ================= ФОРМАТИРОВАНИЕ ТЕКСТА И ВЫНОС КОДА =================
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
                await message.answer(chunk, parse_mode="HTML")
            except Exception:
                await message.answer(main_text[i:i + 4000])

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

# ================= КЛАВИАТУРЫ =================
def get_main_kb(user_id: int):
    u = get_user(user_id)
    web_icon = "🟢 ВКЛ" if u["web_search"] else "🔴 ВЫКЛ"
    school_icon = "🎓 Школа (Активна)" if u["school"]["active"] else "🎓 Режим Школы"

    kb = [
        # Кнопка запуска Mini App
        [KeyboardButton(text="📱 Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_URL))],
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

# ================= УМНЫЙ И РАБОЧИЙ ПОИСК В СЕТИ =================
def sanitize_search_query(text: str) -> str:
    """Убирает вводные слова и лишние фразы из длинного вопроса пользователя."""
    clean = re.sub(r"(?i)^(привет|найди|найди в интернете|поищи|гугл|расскажи про|что такое|реши задачу|реши)\s+", "", text.strip())
    words = clean.split()
    return " ".join(words[:12])

async def search_internet(query: str) -> str:
    clean_q = sanitize_search_query(query)
    if not clean_q:
        return ""

    results_list = []

    # 1. Попытка через duckduckgo_search
    if DDGS_AVAILABLE:
        def fetch_ddgs():
            with DDGS() as ddgs:
                return list(ddgs.text(clean_q, region="ru-ru", safesearch="moderate", max_results=4))
        try:
            ddg_res = await asyncio.to_thread(fetch_ddgs)
            for r in ddg_res or []:
                title = r.get("title", "")
                body = r.get("body", "")
                href = r.get("href", "")
                if body or title:
                    results_list.append(f"• {title}: {body} ({href})")
        except Exception:
            pass

    # 2. Резервный POST-запрос к DuckDuckGo Lite (не блокирует датацентры)
    if not results_list:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://lite.duckduckgo.com/"
            }
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
                async with session.post("https://lite.duckduckgo.com/lite/", data={"q": clean_q}, timeout=8) as r:
                    if r.status == 200:
                        raw_html = await r.text()
                        snippets = re.findall(r'<td class="result-snippet">(.*?)</td>', raw_html, re.DOTALL)
                        titles = re.findall(r'<a class="result-link"[^>]*>(.*?)</a>', raw_html, re.DOTALL)
                        for t, s in list(zip(titles, snippets))[:4]:
                            clean_t = re.sub(r"<[^>]+>", "", t).strip()
                            clean_s = re.sub(r"<[^>]+>", "", s).strip()
                            results_list.append(f"• {html.unescape(clean_t)}: {html.unescape(clean_s)}")
        except Exception:
            pass

    # 3. Резерв через Wikipedia OpenSearch
    if not results_list:
        try:
            wiki_url = f"https://ru.wikipedia.org/w/api.php?action=opensearch&search={urllib.parse.quote(clean_q)}&limit=3&namespace=0&format=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(wiki_url, timeout=6) as resp:
                    if resp.status == 200:
                        wdata = await resp.json()
                        if len(wdata) >= 3 and wdata[2]:
                            for title, desc in zip(wdata[1], wdata[2]):
                                if desc:
                                    results_list.append(f"• [Википедия: {title}]: {desc}")
        except Exception:
            pass

    if not results_list:
        return ""

    return "АКТУАЛЬНЫЕ ДАННЫЕ ИЗ ИНТЕРНЕТА:\n" + "\n".join(results_list)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def extract_document_text(message: Message) -> str:
    doc = message.document
    if doc.file_size > 3 * 1024 * 1024:
        return "[Файл слишком большой (макс 3 МБ)]"
    file = await bot.get_file(doc.file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    try:
        return bio.getvalue().decode("utf-8")
    except UnicodeDecodeError:
        return bio.getvalue().decode("cp1251", errors="ignore")

async def photo_to_base64(file_id: str) -> str:
    file = await bot.get_file(file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    encoded = base64.b64encode(bio.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"

async def check_censorship(text: str) -> tuple[bool, str]:
    if not text or len(text.strip()) < 3:
        return True, ""
    try:
        resp = await create_chat_completion(
            model=DEFAULT_MODEL if DEFAULT_MODEL in models_db else next(iter(models_db)),
            messages=[
                {
                    "role": "system",
                    "content": "Проверь текст на опасный контент. Если всё норм - пиши SAFE. Если нарушение (насилие, ЦП, взлом, наркотики) - UNSAFE | причина"
                },
                {"role": "user", "content": text[:1000]}
            ],
            max_tokens=20
        )
        ans = resp.choices[0].message.content.strip()
        if ans.startswith("UNSAFE"):
            if "|" in ans:
                return False, ans.split("|", 1)[-1].strip()
            return False, "Ответь безопасно."
        return True, ""
    except Exception:
        return True, ""

# ================= ВЕБ-СЕРВЕР TELEGRAM MINI APP (HTML/JS) =================
HTML_MINI_APP = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>ШколоAI App</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --bg: #090d16;
            --card: #151d2f;
            --primary: #4f46e5;
            --primary-hover: #6366f1;
            --text: #f8fafc;
            --subtext: #94a3b8;
            --border: rgba(255,255,255,0.08);
        }
        body {
            margin: 0;
            padding: 16px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            box-sizing: border-box;
        }
        .header { text-align: center; margin-bottom: 20px; }
        .header h1 { margin: 0; font-size: 22px; font-weight: 700; color: #fff; }
        .header p { margin: 4px 0 0; font-size: 13px; color: var(--subtext); }
        .card {
            background: var(--card);
            border-radius: 14px;
            padding: 16px;
            margin-bottom: 14px;
            border: 1px solid var(--border);
        }
        .card h2 { margin-top: 0; font-size: 15px; margin-bottom: 12px; }
        input, select {
            width: 100%;
            background: #090d16;
            border: 1px solid #2d3748;
            color: #fff;
            padding: 12px;
            border-radius: 10px;
            box-sizing: border-box;
            margin-bottom: 10px;
            font-size: 14px;
        }
        button {
            width: 100%;
            background: var(--primary);
            color: #fff;
            border: none;
            padding: 12px;
            border-radius: 10px;
            font-weight: 600;
            font-size: 14px;
            cursor: pointer;
            transition: 0.2s opacity;
        }
        button:active { opacity: 0.8; }
        button.secondary { background: #242f48; }
        .btn-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>⚡ ШколоAI App</h1>
        <p>Быстрое управление нейросетями</p>
    </div>

    <div class="card">
        <h2>🎨 Генератор изображений</h2>
        <input type="text" id="imgPrompt" placeholder="Опишите желаемую картинку...">
        <button onclick="sendAction('/imagine ' + document.getElementById('imgPrompt').value)">Сгенерировать</button>
    </div>

    <div class="card">
        <h2>🎓 Режим Школы</h2>
        <select id="gradeSelect">
            <option>7 класс</option>
            <option>8 класс</option>
            <option selected>9 класс</option>
            <option>10 класс</option>
            <option>11 класс</option>
        </select>
        <div class="btn-grid">
            <button class="secondary" onclick="sendAction('🎓 Режим Школы')">Меню Д/З</button>
            <button onclick="sendAction('🌐 Поиск: 🟢 ВКЛ')">Вкл. поиск</button>
        </div>
    </div>

    <div class="card">
        <h2>🧹 Контекст</h2>
        <button class="secondary" onclick="sendAction('🧹 Очистить память')">Очистить память диалога</button>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.expand();

        function sendAction(cmd) {
            tg.sendData(cmd);
            tg.close();
        }
    </script>
</body>
</html>
"""

async def webapp_handler(request):
    return web.Response(text=HTML_MINI_APP, content_type="text/html")

async def start_background_webserver():
    app = web.Application()
    app.router.add_get("/", webapp_handler)
    app.router.add_get("/webapp", webapp_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
    await site.start()

# ================= БАЗОВЫЕ КОМАНДЫ =================
@dp.message(CommandStart())
async def start_cmd(message: Message):
    u = get_user(message.from_user.id)
    if u["banned"]:
        return await message.answer("🚫 Вы заблокированы.")
    u["state"] = None

    try:
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(text="Mini App 📱", web_app=WebAppInfo(url=WEBAPP_URL))
        )
    except Exception:
        pass

    await message.answer(
        "👋 <b>Добро пожаловать в ШколоAI!</b>\n"
        "Задавайте вопросы, решайте ДЗ, ищите информацию в сети или генерируйте изображения.\n\n"
        "📱 Вы также можете нажать <b>Открыть Mini App</b>, чтобы управлять функциями через визуальное окно!",
        reply_markup=get_main_kb(message.from_user.id),
        parse_mode="HTML"
    )

# Прием данных из Mini App
@dp.message(F.web_app_data)
async def web_app_receive(message: Message):
    data = message.web_app_data.data
    await message.answer(f"📱 Выбрано действие: <b>{html.escape(data)}</b>", parse_mode="HTML")
    message.text = data
    await handle_requests(message)

@dp.message(F.text == "🧹 Очистить память")
async def clear_memory(message: Message):
    u = get_user(message.from_user.id)
    u["history"] = []
    u["state"] = None
    await message.answer("🧹 <b>Контекст очищен!</b> ИИ забыл предыдущий диалог.", parse_mode="HTML")

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

    u["state"] = "wait_image_prompt"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])
    await message.answer(
        "🎨 <b>Режим генерации изображений</b>\n\n"
        "Опишите подробно картинку, которую нужно сгенерировать:",
        reply_markup=kb,
        parse_mode="HTML"
    )

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
    await callback.message.edit_text(f"✅ Выбрана модель: <b>{models_db[m_id]['name']}</b>", parse_mode="HTML")

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
    await callback.message.edit_text("✅ Промпт сброшен.")

@dp.callback_query(F.data == "cancel_state")
async def cb_cancel_state(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    u["state"] = None
    if callback.from_user.id == ADMIN_ID:
        admin_state.pop(ADMIN_ID, None)
    await callback.message.edit_text("❌ Действие отменено.")

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

# ================= АДМИН-ПАНЕЛЬ =================
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
        "🧠 <b>Управление моделями ИИ</b>\nНажмите на модель, чтобы изменить её статус или удалить:",
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
        f"⚙️ <b>Настройка модели</b>\nID: <code>{m_id}</code>\nНазвание: <b>{info['name']}</b>",
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
        return await cb_adm_model_detail(callback)

    elif data.startswith("adm_mod_tvis_"):
        m_id = data.replace("adm_mod_tvis_", "")
        if m_id in models_db:
            models_db[m_id]["vision"] = not models_db[m_id]["vision"]
        return await cb_adm_model_detail(callback)

    elif data.startswith("adm_mod_del_"):
        m_id = data.replace("adm_mod_del_", "")
        if len(models_db) <= 1:
            return await callback.answer("Нельзя удалить последнюю модель!", show_alert=True)
        models_db.pop(m_id, None)
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
        await callback.answer("Пользователь забанен!")
    elif data.startswith("adm_sub_"):
        tid = int(data.replace("adm_sub_", ""))
        u = get_user(tid)
        u["is_sub"] = True
        u["sub_end"] = datetime.now() + timedelta(days=30)
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

# ================= ШКОЛА =================
@dp.message(F.text.startswith("🎓"))
async def menu_school(message: Message):
    u = get_user(message.from_user.id)
    await message.answer("🎓 <b>Режим Школа</b>", reply_markup=get_school_main_kb(u), parse_mode="HTML")

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

    await callback.message.edit_reply_markup(reply_markup=get_school_main_kb(u))

# --- ПЕРЕКЛЮЧАТЕЛЬ ПОИСКА ---
@dp.message(F.text.startswith("🌐 Поиск:"))
async def toggle_web(message: Message):
    u = get_user(message.from_user.id)
    u["web_search"] = not u["web_search"]
    status_text = "🟢 ВКЛ" if u["web_search"] else "🔴 ВЫКЛ"
    await message.answer(f"🌐 Поиск в интернете: <b>{status_text}</b>", reply_markup=get_main_kb(message.from_user.id), parse_mode="HTML")

# ================= ГЛАВНЫЙ ОБРАБОТЧИК ЗАПРОСОВ =================
@dp.message(F.text | F.photo | F.document)
async def handle_requests(message: Message):
    uid = message.from_user.id
    u = get_user(uid)

    if u["banned"]:
        return

    # 1. Состояния Админ-панели
    if uid == ADMIN_ID and ADMIN_ID in admin_state and message.text:
        st = admin_state.pop(ADMIN_ID)
        act = st.get("action")

        if act == "adm_edit_free_lim":
            if message.text.isdigit():
                bot_config["free_limit"] = int(message.text)
                return await message.answer(f"✅ Бесплатный лимит изменен на <code>{bot_config['free_limit']}</code>", reply_markup=get_admin_main_kb(), parse_mode="HTML")
            return await message.answer("❌ Введите число.")

        elif act == "adm_edit_sub_lim":
            if message.text.isdigit():
                bot_config["sub_limit"] = int(message.text)
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
                return await message.answer(f"✅ Модель <b>{m_name}</b> (<code>{m_id}</code>) успешно добавлена!", reply_markup=get_admin_main_kb(), parse_mode="HTML")
            except Exception:
                return await message.answer("❌ Ошибка формата. Пример:\n<code>gpt-5 | GPT-5 | 1 | 1</code>", parse_mode="HTML")

        elif act == "set_photo_model":
            bot_config["photo_model"] = message.text.strip()
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

    # 2. Ответ админа юзеру
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

    # 4. Генерация фото (по состоянию или команде /imagine)
    is_img_cmd = message.text and message.text.startswith("/imagine ")
    if (u["state"] == "wait_image_prompt" or is_img_cmd) and message.text:
        u["state"] = None
        prompt_txt = message.text.replace("/imagine ", "").strip()
        if not prompt_txt:
            return await message.answer("❌ Напишите описание картинки.")

        if not u["is_sub"] and uid != ADMIN_ID:
            return await message.answer("🔒 Генерация изображений доступна только с подпиской Premium!")

        wait_msg = await message.answer("🎨 <b>Генерирую изображение...</b> Это займет 10–25 секунд.", parse_mode="HTML")
        try:
            img_data, is_bytes = await generate_image_api(prompt_txt)
            await wait_msg.delete()
            caption = f"🎨 <b>Запрос:</b> <i>{html.escape(prompt_txt[:200])}</i>"
            if is_bytes:
                photo_file = BufferedInputFile(img_data, filename="generated.png")
                await message.answer_photo(photo=photo_file, caption=caption, parse_mode="HTML")
            else:
                await message.answer_photo(photo=img_data, caption=caption, parse_mode="HTML")
            u["tokens"] += 1000
            return
        except Exception as e:
            return await wait_msg.edit_text(f"❌ Ошибка генерации изображения: {e}")

    # 5. Свой промпт
    if u["state"] == "wait_prompt" and message.text:
        is_safe, _ = await check_censorship(message.text)
        if not is_safe:
            return await message.answer("❌ Промпт не прошел цензуру.")
        u["custom_prompt"] = message.text[:400]
        u["state"] = None
        return await message.answer("✅ Промпт сохранен!")

    # 6. Тема школы
    if u["state"] == "school_topic" and message.text:
        u["school"]["topic"] = message.text[:50]
        u["state"] = None
        return await message.answer(f"✅ Тема Школы: {u['school']['topic']}", reply_markup=get_school_main_kb(u))

    # Анти-спам
    now = datetime.now()
    if uid in anti_spam and (now - anti_spam[uid]).total_seconds() < 2.5:
        return await message.answer("⏳ Пожалуйста, не спамьте. Подождите пару секунд.")
    anti_spam[uid] = now

    # Лимиты
    limit = bot_config["sub_limit"] if u["is_sub"] else bot_config["free_limit"]
    if u["tokens"] >= limit and uid != ADMIN_ID:
        return await message.answer("❌ Лимит токенов исчерпан. Оформите подписку Premium для продолжения.")

    # Документы
    raw_text = message.text or message.caption or ""
    if message.document:
        doc_content = await extract_document_text(message)
        raw_text += f"\n\n[Документ {message.document.file_name}]:\n{doc_content[:4000]}"

    # Обучение (Школа)
    s = u["school"]
    if s["active"] and s["hw_type"] == "Письменное" and s["training_mode"]:
        if message.photo:
            b64_img = await photo_to_base64(message.photo[-1].file_id)
            vis_mod = u["current_model"] if models_db.get(u["current_model"], {}).get("vision") else "gpt-6-luna"
            try:
                res = await create_chat_completion(
                    model=vis_mod,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Разбери алгоритм и оформление решения с этого фото:"},
                                {"type": "image_url", "image_url": {"url": b64_img}}
                            ]
                        }
                    ]
                )
                s["training_samples"].append(f"[Пример]: {res.choices[0].message.content}")
                u["tokens"] += res.usage.total_tokens if res.usage else 200
            except Exception as e:
                return await message.answer(f"❌ Ошибка распознавания: {e}")
        else:
            s["training_samples"].append(f"[Пример текста]: {raw_text}")

        return await message.answer(
            f"🧠 Пример #{len(s['training_samples'])} сохранен! Отключи Режим Обучения для решения ДЗ.",
            reply_markup=get_school_main_kb(u)
        )

    # Цензура
    is_safe, _ = await check_censorship(raw_text)
    if not is_safe:
        u["strikes"] += 1
        if u["strikes"] >= 3:
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Бан-лог: {uid} | Запрос: {raw_text[:200]}")
            except Exception:
                pass

    # Кубик Школы
    dice_style = ""
    if s["active"] and s["hw_type"] in ["Проект", "Творческое", "Пересказ"]:
        dm = await message.answer_dice("🎲")
        await asyncio.sleep(2.2)
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
    sys_prompt = BASE_SYSTEM_PROMPT
    if u["custom_prompt"]:
        sys_prompt += f"\nДополнительная инструкция пользователя: {u['custom_prompt']}"

    if s["active"]:
        sys_prompt += f"\n[ШКОЛА]: Ученик {s['grade']}, {s['quarter']}, уровень: {s['level']}. Тема: {s['topic']}. Тип: {s['hw_type']}."
        if dice_style:
            sys_prompt += f" Стиль выполнения: {dice_style}."

        if s["topic"] != "Общая":
            edu_ctx = await search_internet(f"школьная программа {s['grade']} тема {s['topic']} что должны знать")
            if edu_ctx:
                sys_prompt += f"\n[ЗНАНИЯ КЛАССА]: {edu_ctx}"

        if s["hw_type"] == "Письменное" and s["training_samples"]:
            sys_prompt += "\nИспользуй строго этот метод решения: " + "\n".join(s["training_samples"][-5:])

    temp = await message.answer("⏳ Думаю...")

    # Поиск в интернете
    if u["web_search"] and raw_text:
        try:
            await temp.edit_text("🌐 Ищу актуальную информацию в интернете...")
        except Exception:
            pass
        net = await search_internet(raw_text)
        if net:
            sys_prompt += (
                f"\n\n[ВНИМАНИЕ! ТЕБЕ ДАН ДОСТУП В ИНТЕРНЕТ]\n"
                f"Актуальные результаты поиска по запросу пользователя:\n{net}\n"
                f"ОБЯЗАТЕЛЬНО опирайся на эти данные в ответе и никогда не пиши, что у тебя нет доступа к сети!"
            )
        else:
            sys_prompt += "\n\n[Системное сообщение: Поиск в сети не дал прямых совпадений, ответь на основе своих знаний.]"

    messages_payload = [{"role": "system", "content": sys_prompt}]

    for msg in u["history"]:
        messages_payload.append(msg)

    if message.photo:
        b64 = await photo_to_base64(message.photo[-1].file_id)
        current_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": raw_text or "Опиши фото и реши задание, если оно есть"},
                {"type": "image_url", "image_url": {"url": b64}}
            ]
        }
        messages_payload.append(current_msg)
    else:
        current_msg = {"role": "user", "content": raw_text}
        messages_payload.append(current_msg)

    resp_text = ""
    last_ed = time.time()

    try:
        active_model = u["current_model"]
        if message.photo and not models_db.get(active_model, {}).get("vision", False):
            active_model = "gpt-6-luna"

        stream = await create_chat_completion(
            model=active_model,
            messages=messages_payload,
            stream=True
        )

        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                resp_text += chunk.choices[0].delta.content
                if time.time() - last_ed > 1.3 and resp_text:
                    try:
                        await temp.edit_text(resp_text[:3900] + " ✍️")
                        last_ed = time.time()
                    except Exception:
                        pass

        try:
            await temp.delete()
        except Exception:
            pass

        if not resp_text.strip():
            resp_text = "⚠️ Модель вернула пустой ответ."

        await send_smart_ai_response(message, resp_text)

        user_hist = raw_text if not message.photo else "[Фото/Документ]"
        u["history"].append({"role": "user", "content": user_hist})
        u["history"].append({"role": "assistant", "content": resp_text})

        if len(u["history"]) > 10:
            u["history"] = u["history"][-10:]

        u["tokens"] += max(50, len(resp_text) // 3)

    except Exception as e:
        await temp.edit_text(f"❌ Ошибка API: {str(e)}")

# ================= ЗАПУСК =================
async def main():
    # Запускаем встроенный фоновый веб-сервер Mini App
    await start_background_webserver()
    print(f"🚀 Telegram Mini App сервер активен на порту {WEBAPP_PORT}!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
