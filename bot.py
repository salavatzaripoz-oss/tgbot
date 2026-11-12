import os
import io
import base64
import time
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, 
    CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
)
from openai import AsyncOpenAI

# Поддержка поиска в интернете (если библиотека установлена)
try:
    from duckduckgo_search import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    SEARCH_AVAILABLE = False

# --- КОНФИГУРАЦИЯ (С ЗАЩИТОЙ ОТ ПРОБЕЛОВ И СБОЕВ ОКРУЖЕНИЯ) ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8130566129:AAHGfVKiIorLhqGAU_HtbCPjSyXQlWgbzVw").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5656256968").strip())
AI_API_KEY = os.getenv("AI_API_KEY", "sk-eF2ZQEE1uyLh6VRq1x26zryHbbHqKT7plceJQ37U3Au1IrHm").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://anymodel.ghostly.su/v1").strip()

FREE_LIMIT = int(os.getenv("FREE_LIMIT", "45000").strip())
SUB_LIMIT = int(os.getenv("SUB_LIMIT", "100000").strip())
BASE_SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT", 
    "Ты полезный ИИ-ассистент. Отвечай четко, структурированно и по делу."
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)

# --- БАЗА МОДЕЛЕЙ (Базовая — самая экономная deepseek-flash) ---
DEFAULT_MODEL = "gemini-3.5-flash-lite"

models_db = {
    "deepseek-flash": {"name": "DeepSeek Flash (Базовая ⚡)", "premium": False, "vision": False},
    "deepseek-v4.1-flash-1017": {"name": "DeepSeek v4.1 Flash", "premium": False, "vision": False},
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
    "deepseek-r1": {"name": "DeepSeek R1 (Мышление)", "premium": True, "vision": False}
}

users_db = {}
anti_spam = {}
admin_reply_target = {} # Для переговоров админа с покупателями

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
            "state": None, # None, "wait_prompt", "support_chat", "school_topic", "school_train"
            "school": {
                "active": False,
                "grade": "9 класс",
                "quarter": "1 четверть",
                "level": "Средний",
                "hw_type": "Письменное", # Проект, Письменное, Творческое, Пересказ
                "topic": "Общая",
                "training_mode": False,
                "training_samples": [] # Примеры решений от ученика
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
    if curr not in models_db or (models_db[curr]["premium"] and not user["is_sub"]):
        user["current_model"] = DEFAULT_MODEL
        
    return user

# --- КЛАВИАТУРЫ (БЫСТРЫЕ КНОПКИ) ---

def get_main_kb(user_id: int):
    u = get_user(user_id)
    web_Icon = "🟢 ВКЛ" if u["web_search"] else "🔴 ВЫКЛ"
    school_Icon = "🎓 Школа (Активна)" if u["school"]["active"] else "🎓 Режим Школы"
    
    kb = [
        [KeyboardButton(text="🧠 Модели ИИ"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text=school_Icon), KeyboardButton(text=f"🌐 Поиск: {web_Icon}")],
        [KeyboardButton(text="⚙️ Свой промпт"), KeyboardButton(text="💎 Подписка / Связь")]
    ]
    if user_id == ADMIN_ID:
        kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_school_kb(u: dict):
    s = u["school"]
    status = "🟢 ВКЛЮЧЕН" if s["active"] else "🔴 ВЫКЛЮЧЕН"
    train_status = "🟢 ВКЛ" if s["training_mode"] else "🔴 ВЫКЛ"
    
    buttons = [
        [InlineKeyboardButton(text=f"Статус режима: {status}", callback_data="sch_toggle")],
        [
            InlineKeyboardButton(text=f"🏫 {s['grade']}", callback_data="sch_grade"),
            InlineKeyboardButton(text=f"📅 {s['quarter']}", callback_data="sch_quarter")
        ],
        [
            InlineKeyboardButton(text=f"📊 Уровень: {s['level']}", callback_data="sch_level"),
            InlineKeyboardButton(text=f"📝 Д/З: {s['hw_type']}", callback_data="sch_hw")
        ],
        [InlineKeyboardButton(text=f"📌 Тема: {s['topic'][:20]}", callback_data="sch_topic")]
    ]
    if s["hw_type"] == "Письменное":
        samples_cnt = len(s["training_samples"])
        buttons.append([InlineKeyboardButton(
            text=f"🧠 Режим Обучения: {train_status} (Примеров: {samples_cnt})", 
            callback_data="sch_train_toggle"
        )])
        if samples_cnt > 0:
            buttons.append([InlineKeyboardButton(text="🗑 Очистить примеры обучения", callback_data="sch_train_clear")])
            
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (BASE64, ПОИСК, ЦЕНЗУРА) ---

async def photo_to_base64(file_id: str) -> str:
    """Скачивает фото из ТГ и переводит в data:image/jpeg;base64 для API без URL."""
    file = await bot.get_file(file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    encoded = base64.b64encode(bio.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"

async def extract_document_text(message: Message) -> str:
    """Читает текстовые документы (.txt, .py, .md, .csv, .json и т.д.)."""
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

def search_internet(query: str) -> str:
    if not SEARCH_AVAILABLE:
        return ""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
        if not results:
            return ""
        text = "Актуальная информация из интернета:\n"
        for r in results:
            text += f"- {r.get('title')}: {r.get('body')}\n"
        return text
    except Exception:
        return ""

async def check_censorship(text: str) -> tuple[bool, str]:
    """Проверяет текст через дешевый deepseek-flash (1 раз). Возвращает (Безопасно_ли, Правила_исправления)."""
    if not text or len(text.strip()) < 3:
        return True, ""
    try:
        resp = await ai_client.chat.completions.create(
            model="deepseek-flash",
            messages=[
                {
                    "role": "system", 
                    "content": (
                        "Ты фильтр безопасности. Проверь сообщение пользователя на опасный контент "
                        "(экстремизм, создание оружия/наркотиков, жесткое насилие, взлом систем). "
                        "Если всё нормально (включая обычную учебу, бытовые вопросы или легкий сленг), ответь строго словом: SAFE. "
                        "Если есть опасное нарушение, напиши: UNSAFE | и кратко укажи правило, как переформулировать ответ безопасно."
                    )
                },
                {"role": "user", "content": text[:1500]}
            ],
            max_tokens=100
        )
        ans = resp.choices[0].message.content.strip()
        if ans.startswith("UNSAFE"):
            rule = ans.split("|", 1)[-1].strip() if "|" in ans else "Ответь нейтрально и безопасно в рамках закона."
            return False, rule
        return True, ""
    except Exception:
        return True, ""

# --- СТАРТ И ГЛАВНОЕ МЕНЮ ---

@dp.message(CommandStart())
async def start_cmd(message: Message):
    u = get_user(message.from_user.id)
    if u["banned"]:
        return await message.answer("🚫 Вы заблокированы администратором.")
    u["state"] = None
    role_text = "👑 **Вы вошли как Администратор!**\n\n" if message.from_user.id == ADMIN_ID else ""
    await message.answer(
        f"👋 **Добро пожаловать в умного ИИ-бота!**\n{role_text}"
        f"Используйте кнопки меню внизу для переключения моделей, режима **🎓 Школы**, поиска в сети и загрузки файлов/фото.",
        reply_markup=get_main_kb(message.from_user.id),
        parse_mode="Markdown"
    )

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("stats"))
async def show_profile(message: Message):
    uid = message.from_user.id
    u = get_user(uid)
    limit = SUB_LIMIT if u["is_sub"] else FREE_LIMIT
    used = u["tokens"]
    pct = (used / limit) * 100 if limit > 0 else 100
    status = "💎 Premium" if u["is_sub"] else "🆓 Бесплатный"
    if uid == ADMIN_ID:
        status += " (👑 АДМИН)"
    
    m_name = models_db.get(u["current_model"], {}).get("name", u["current_model"])
    prompt_info = u["custom_prompt"] if u["custom_prompt"] else "Стандартный"
    
    kb = None
    if not u["is_sub"]:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить подписку (100к токенов + Топ ИИ)", callback_data="buy_sub_prompt")]
        ])

    await message.answer(
        f"👤 **Ваш профиль (`{uid}`)**\n\n"
        f"🏷 **Статус:** {status}\n"
        f"🧠 **Текущая модель:** {m_name}\n"
        f"📊 **Токены:** `{used} / {limit}` ({pct:.1f}%)\n"
        f"🔄 **Сброс лимита:** {u['reset_date'].strftime('%d.%m.%Y')}\n"
        f"⚙️ **Свой промпт:** _{prompt_info[:50]}_",
        reply_markup=kb,
        parse_mode="Markdown"
    )

# --- ВЫБОР МОДЕЛЕЙ ---

@dp.message(F.text == "🧠 Модели ИИ")
@dp.message(Command("model"))
async def menu_models(message: Message):
    u = get_user(message.from_user.id)
    buttons = []
    for m_id, data in models_db.items():
        prefix = "✅ " if u["current_model"] == m_id else ""
        lock = " 🔒" if data["premium"] and not u["is_sub"] else ""
        vis = " 🖼" if data["vision"] else ""
        buttons.append([InlineKeyboardButton(
            text=f"{prefix}{data['name']}{vis}{lock}", 
            callback_data=f"setmod_{m_id}"
        )])
    await message.answer(
        "🧠 **Выберите нейросеть:**\n"
        "🖼 — понимает фото | 🔒 — нужна подписка Premium\n"
        "_По умолчанию стоит быстрая и экономная DeepSeek Flash._",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("setmod_"))
async def cb_set_model(callback: CallbackQuery):
    m_id = callback.data.replace("setmod_", "")
    if m_id not in models_db:
        return await callback.answer("❌ Модель не найдена!", show_alert=True)
    u = get_user(callback.from_user.id)
    if models_db[m_id]["premium"] and not u["is_sub"] and callback.from_user.id != ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Написать админу для покупки", callback_data="buy_sub_prompt")]
        ])
        await callback.message.answer(
            f"🔒 Модель **{models_db[m_id]['name']}** доступна только с подпиской **Premium** (100 000 токенов в месяц + доступ ко всем флагманским ИИ)!",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return await callback.answer()
    
    u["current_model"] = m_id
    await callback.message.edit_text(f"✅ Выбрана модель: **{models_db[m_id]['name']}**", parse_mode="Markdown")
    await callback.answer()

# --- ПОИСК В ИНТЕРНЕТЕ И КАСТОМНЫЙ ПРОМПТ ---

@dp.message(F.text.startswith("🌐 Поиск:"))
async def toggle_web_search(message: Message):
    u = get_user(message.from_user.id)
    u["web_search"] = not u["web_search"]
    state_str = "🟢 ВКЛЮЧЕН" if u["web_search"] else "🔴 ВЫКЛЮЧЕН"
    await message.answer(
        f"🌐 Поиск в интернете: **{state_str}**", 
        reply_markup=get_main_kb(message.from_user.id),
        parse_mode="Markdown"
    )

@dp.message(F.text == "⚙️ Свой промпт")
async def menu_custom_prompt(message: Message):
    u = get_user(message.from_user.id)
    u["state"] = "wait_prompt"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Сбросить промпт", callback_data="reset_prompt")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])
    await message.answer(
        f"⚙️ **Настройка системного промпта**\n"
        f"Текущий: _{u['custom_prompt'] or 'Не задан'}_\n\n"
        f"Напишите в чат, каким должен быть характер или правила ответа ИИ (промпт пройдет проверку безопасности):",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "reset_prompt")
async def cb_reset_prompt(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    u["custom_prompt"] = ""
    u["state"] = None
    await callback.message.edit_text("✅ Кастомный промпт сброшен на стандартный.")
    await callback.answer()

@dp.callback_query(F.data == "cancel_state")
async def cb_cancel_state(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    u["state"] = None
    await callback.message.edit_text("❌ Действие отменено.")
    await callback.answer()

# --- РАЗДЕЛ ПОДПИСКИ И ПЕРЕГОВОРЫ ЧЕРЕЗ БОТА ---

@dp.message(F.text == "💎 Подписка / Связь")
async def menu_subscription(message: Message):
    u = get_user(message.from_user.id)
    u["state"] = "support_chat"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Выйти из режима связи", callback_data="cancel_state")]
    ])
    await message.answer(
        "💎 **Подписка Premium даёт:**\n"
        "• **100 000 токенов** в месяц (вместо 45 000)\n"
        "• Доступ к топовым моделям: **GPT-6.1 Sol, Claude 5.5 Opus, Gemini 4 Argon, Grok 4.7, DeepSeek R1**\n"
        "• Приоритетное распознавание фото и файлов\n\n"
        "✍️ **Напишите любое сообщение прямо сейчас** — оно мгновенно отправится администратору, и он ответит вам прямо здесь в боте!",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "buy_sub_prompt")
async def cb_buy_sub_prompt(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    u["state"] = "support_chat"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])
    await callback.message.answer(
        "✍️ **Напишите сообщение администратору для оформления подписки** (например: «Хочу купить подписку, какая цена?»):",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()

# --- РЕЖИМ "ШКОЛА" И РЕЖИМ "ОБУЧЕНИЕ" ---

@dp.message(F.text.startswith("🎓"))
async def menu_school(message: Message):
    u = get_user(message.from_user.id)
    await message.answer(
        "🎓 **Настройка режима «Школа»**\n"
        "• Выберите класс, четверть, уровень знаний и тип Д/З.\n"
        "• Для **Проекта / Творческого / Пересказа** бот перед ответом бросает кубик 🎲 и выбирает уникальный стиль.\n"
        "• Для **Письменного Д/З** доступен режим **«Обучение»** — скидывайте боту фото или текст примеров от учителя, и он будет решать задачи строго вашим способом!",
        reply_markup=get_school_kb(u),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("sch_"))
async def cb_school_handler(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    s = u["school"]
    action = callback.data

    if action == "sch_toggle":
        s["active"] = not s["active"]
        await callback.message.answer(
            f"🎓 Режим Школы: **{'ВКЛЮЧЕН' if s['active'] else 'ВЫКЛЮЧЕН'}**",
            reply_markup=get_main_kb(callback.from_user.id),
            parse_mode="Markdown"
        )
    elif action == "sch_grade":
        grades = ["1-4 класс", "5 класс", "6 класс", "7 класс", "8 класс", "9 класс", "10 класс", "11 класс"]
        idx = (grades.index(s["grade"]) + 1) % len(grades) if s["grade"] in grades else 0
        s["grade"] = grades[idx]
    elif action == "sch_quarter":
        quarters = ["1 четверть", "2 четверть", "3 четверть", "4 четверть"]
        idx = (quarters.index(s["quarter"]) + 1) % len(quarters) if s["quarter"] in quarters else 0
        s["quarter"] = quarters[idx]
    elif action == "sch_level":
        levels = ["Начальный (объясняй на пальцах)", "Средний", "Продвинутый (Олимпиадный)"]
        idx = (levels.index(s["level"]) + 1) % len(levels) if s["level"] in levels else 0
        s["level"] = levels[idx]
    elif action == "sch_hw":
        hws = ["Письменное", "Проект", "Творческое", "Пересказ"]
        idx = (hws.index(s["hw_type"]) + 1) % len(hws) if s["hw_type"] in hws else 0
        s["hw_type"] = hws[idx]
    elif action == "sch_topic":
        u["state"] = "school_topic"
        await callback.message.answer("📌 Введите название темы или предмета (или напишите «Общая»):")
        return await callback.answer()
    elif action == "sch_train_toggle":
        s["training_mode"] = not s["training_mode"]
        if s["training_mode"]:
            await callback.message.answer(
                "🧠 **Режим Обучения ВКЛЮЧЕН!**\n"
                "Отправляйте боту промпты, правила оформления или **фотографии решенных задач**, чтобы он запомнил способ решения. "
                "Когда закончите загружать примеры — выключите тумблер «Режим Обучения» в меню Школы и скиньте само задание!",
                parse_mode="Markdown"
            )
    elif action == "sch_train_clear":
        s["training_samples"].clear()
        await callback.answer("🗑 Примеры обучения очищены!", show_alert=True)

    await callback.message.edit_reply_markup(reply_markup=get_school_kb(u))
    await callback.answer()

# --- АДМИН-ПАНЕЛЬ ---

@dp.message(F.text == "👑 Админ-панель")
@dp.message(Command("admin"))
async def admin_panel_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer(f"❌ Доступ запрещен. Ваш ID: `{message.from_user.id}`", parse_mode="Markdown")
    
    total_users = len(users_db)
    sub_users = sum(1 for u in users_db.values() if u["is_sub"])
    
    await message.answer(
        f"👑 **Панель Администратора**\n"
        f"👥 Всего пользователей в памяти: `{total_users}` | С подпиской: `{sub_users}`\n\n"
        f"**Команды управления:**\n"
        f"• `/give_sub <ID> <дней>` — выдать подписку\n"
        f"• `/ban <ID>` — заблокировать / разблокировать\n"
        f"• `/add_model <id> <prem:1/0> <vis:1/0> <Имя>` — добавить ИИ\n"
        f"• `/del_model <id>` — удалить ИИ",
        parse_mode="Markdown"
    )

@dp.message(Command("give_sub"))
async def cmd_give_sub(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        _, tid, days = message.text.split()
        tid, days = int(tid), int(days)
        u = get_user(tid)
        u["is_sub"] = True
        u["sub_end"] = datetime.now() + timedelta(days=days)
        await message.answer(f"✅ Подписка выдана пользователю `{tid}` на {days} дней.", parse_mode="Markdown")
        try:
            await bot.send_message(tid, f"🎉 **Администратор активировал вам подписку Premium на {days} дней!**\nЛимит увеличен до 100 000 токенов, открыты все модели.", parse_mode="Markdown")
        except Exception:
            pass
    except Exception:
        await message.answer("Формат: `/give_sub 123456789 30`", parse_mode="Markdown")

@dp.message(Command("ban"))
async def cmd_ban_user(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        tid = int(message.text.split()[1])
        u = get_user(tid)
        u["banned"] = not u["banned"]
        st = "ЗАБЛОКИРОВАН 🚫" if u["banned"] else "РАЗБЛОКИРОВАН ✅"
        await message.answer(f"Пользователь `{tid}` {st}.", parse_mode="Markdown")
    except Exception:
        await message.answer("Формат: `/ban 123456789`", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_"))
async def cb_admin_actions(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    data = callback.data
    
    if data.startswith("adm_ban_"):
        tid = int(data.replace("adm_ban_", ""))
        u = get_user(tid)
        u["banned"] = True
        await callback.message.edit_text(callback.message.text + f"\n\n🚫 **Пользователь {tid} ЗАБЛОКИРОВАН.**")
        await callback.answer("Заблокирован!")
        
    elif data.startswith("adm_sub_"):
        tid = int(data.replace("adm_sub_", ""))
        u = get_user(tid)
        u["is_sub"] = True
        u["sub_end"] = datetime.now() + timedelta(days=30)
        await callback.answer("Подписка на 30 дней выдана!", show_alert=True)
        try:
            await bot.send_message(tid, "🎉 **Вам выдана подписка Premium на 30 дней!**", parse_mode="Markdown")
        except Exception:
            pass
            
    elif data.startswith("adm_reply_"):
        tid = int(data.replace("adm_reply_", ""))
        admin_reply_target[ADMIN_ID] = tid
        await callback.message.answer(f"✍️ Введите ответ для пользователя `{tid}`:", parse_mode="Markdown")
        await callback.answer()

# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ, ФАЙЛОВ И ФОТО ---

@dp.message(F.text | F.photo | F.document)
async def handle_all_messages(message: Message):
    uid = message.from_user.id
    u = get_user(uid)

    if u["banned"]:
        return await message.answer("🚫 Вы заблокированы за нарушение правил использования.")

    # 1. Если админ отвечает покупателю
    if uid == ADMIN_ID and ADMIN_ID in admin_reply_target and message.text:
        target_id = admin_reply_target.pop(ADMIN_ID)
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Ответить администратору", callback_data="buy_sub_prompt")]
            ])
            await bot.send_message(target_id, f"👨‍💻 **Сообщение от Администратора:**\n\n{message.text}", reply_markup=kb, parse_mode="Markdown")
            return await message.answer(f"✅ Ответ доставлен пользователю `{target_id}`.", parse_mode="Markdown")
        except Exception as e:
            return await message.answer(f"❌ Не удалось отправить: {e}")

    # 2. Обработка состояний ввода (тема школы, кастомный промпт, чат поддержки)
    if u["state"] == "school_topic" and message.text:
        u["school"]["topic"] = message.text[:40]
        u["state"] = None
        return await message.answer(f"📌 Тема установлена: **{u['school']['topic']}**", reply_markup=get_school_kb(u), parse_mode="Markdown")

    if u["state"] == "wait_prompt" and message.text:
        is_safe, rule = await check_censorship(message.text)
        if not is_safe:
            return await message.answer("❌ Этот системный промпт нарушает правила безопасности. Попробуйте другой.")
        u["custom_prompt"] = message.text[:400]
        u["state"] = None
        return await message.answer(f"✅ Ваш системный промпт сохранен:\n_{u['custom_prompt']}_", parse_mode="Markdown")

    if u["state"] == "support_chat" and message.text:
        u["state"] = None
        adm_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✍️ Ответить", callback_data=f"adm_reply_{uid}"),
                InlineKeyboardButton(text="💎 Выдать Sub (30д)", callback_data=f"adm_sub_{uid}")
            ],
            [InlineKeyboardButton(text="🚫 Бан", callback_data=f"adm_ban_{uid}")]
        ])
        username = f"@{message.from_user.username}" if message.from_user.username else "Без юзернейма"
        await bot.send_message(
            ADMIN_ID,
            f"📩 **Запрос по подписке от** `{uid}` ({username}):\n\n{message.text}",
            reply_markup=adm_kb,
            parse_mode="Markdown"
        )
        return await message.answer("✅ Ваше сообщение отправлено администратору! Ожидайте ответа в этом чате.", reply_markup=get_main_kb(uid))

    # 3. Анти-спам (не чаще 1 запроса в 2.5 сек)
    now = datetime.now()
    if uid in anti_spam and (now - anti_spam[uid]).total_seconds() < 2.5:
        return await message.answer("⏳ Пожалуйста, не спамьте. Подождите пару секунд.")
    anti_spam[uid] = now

    # 4. Проверка лимита токенов
    limit = SUB_LIMIT if u["is_sub"] else FREE_LIMIT
    if u["tokens"] >= limit and uid != ADMIN_ID:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Увеличить лимит до 100к (Подписка)", callback_data="buy_sub_prompt")]
        ])
        return await message.answer("❌ **Ваш месячный лимит токенов исчерпан!**\nОформите подписку Premium или дождитесь даты сброса.", reply_markup=kb, parse_mode="Markdown")

    # 5. Режим "Обучения" в Школе (накопление образцов)
    s = u["school"]
    if s["active"] and s["hw_type"] == "Письменное" and s["training_mode"]:
        if message.photo:
            b64_img = await photo_to_base64(message.photo[-1].file_id)
            # Кратко распознаем пример, чтобы сохранить его суть в память
            vis_mod = u["current_model"] if models_db[u["current_model"]]["vision"] else "gpt-6-luna"
            try:
                res = await ai_client.chat.completions.create(
                    model=vis_mod,
                    messages=[{"role": "user", "content": [
                        {"type": "text", "text": "Разбери это фото примера решения. Опиши алгоритм, оформление и метод решения, чтобы использовать как шаблон:"},
                        {"type": "image_url", "image_url": {"url": b64_img}}
                    ]}]
                )
                sample_desc = res.choices[0].message.content
                s["training_samples"].append(f"[Пример из фото]: {sample_desc}")
                u["tokens"] += res.usage.total_tokens if res.usage else 200
            except Exception as e:
                return await message.answer(f"❌ Не удалось прочитать фото для обучения: {e}")
        else:
            text_sample = message.text or message.caption or ""
            s["training_samples"].append(f"[Текстовый пример]: {text_sample}")
            
        return await message.answer(
            f"🧠 **Пример #{len(s['training_samples'])} успешно изучен и сохранен!**\n"
            f"Отправьте еще примеры или выключите «Режим Обучения» в меню `🎓 Школа`, чтобы перейти к решению самого Д/З.",
            reply_markup=get_school_kb(u),
            parse_mode="Markdown"
        )

    # 6. Сборка текста запроса и файлов
    raw_text = message.text or message.caption or ""
    if message.document:
        doc_content = await extract_document_text(message)
        raw_text += f"\n\n[Содержимое файла {message.document.file_name}]:\n{doc_content[:4000]}"

    # 7. Цензура через deepseek-flash (1 раз)
    is_safe, safety_rule = await check_censorship(raw_text)
    if not is_safe:
        u["strikes"] += 1
        # Если частые нарушения (>= 2 страйков) — шлем лог админу
        if u["strikes"] >= 2:
            adm_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Заблокировать навсегда", callback_data=f"adm_ban_{uid}")]
            ])
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"⚠️ **Подозрительная активность (Страйк #{u['strikes']})**\n"
                    f"Юзер: `{uid}`\nЗапрос: _{raw_text[:400]}_\nПравило от цензора: _{safety_rule}_",
                    reply_markup=adm_kb,
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    # 8. Бросок кубика 🎲 для Школы (Проект / Творческое / Пересказ)
    dice_variation = ""
    if s["active"] and s["hw_type"] in ["Проект", "Творческое", "Пересказ"]:
        dice_msg = await message.answer_dice(emoji="🎲")
        dice_val = dice_msg.dice.value
        variations = {
            1: "Академический и строго научный подход с четкими фактами",
            2: "Креативный и увлекательный стиль с яркими примерами из жизни",
            3: "Структурированный формат с тезисами, планом и выводами",
            4: "Формат живого рассказа (сторителлинг), который легко запомнить",
            5: "Аналитический разбор с аргументами «за» и «против»",
            6: "Максимально подробный и глубокий разбор на высший балл (5+)"
        }
        dice_variation = variations.get(dice_val, variations[1])
        await asyncio.sleep(2.2) # Ждем окончания анимации кубика
        await message.answer(f"🎲 Выпало число **{dice_val}**! Выбран стиль генерации:\n_{dice_variation}_", parse_mode="Markdown")

    # 9. Формирование системного промпта
    sys_prompt = BASE_SYSTEM_PROMPT
    if u["custom_prompt"]:
        sys_prompt += f"\nДополнительная инструкция пользователя: {u['custom_prompt']}"
        
    if s["active"]:
        sys_prompt += (
            f"\n[РЕЖИМ ШКОЛЫ АКТИВЕН]: Ученик: {s['grade']}, {s['quarter']}, уровень знаний: {s['level']}. "
            f"Тема: {s['topic']}. Тип домашнего задания: {s['hw_type']}."
        )
        if dice_variation:
            sys_prompt += f"\nОбязательно используй этот стиль выполнения: {dice_variation}."
        if s["hw_type"] == "Письменное" and s["training_samples"]:
            samples_str = "\n".join(s["training_samples"][-5:])
            sys_prompt += f"\nРешай и оформляй письменную задачу СТРОГО по этим примерам и способам от ученика:\n{samples_str}"

    if not is_safe:
        sys_prompt += (
            f"\n[ВНИМАНИЕ, ЦЕНЗУРА]: Запрос пользователя содержал нежелательный элемент. "
            f"Переработай и ответь строго по этому правилу безопасности: {safety_rule}."
        )

    if u["web_search"] and raw_text:
        web_info = search_internet(raw_text)
        if web_info:
            sys_prompt += f"\n\n{web_info}"

    # 10. Подготовка модели и Base64 для фото
    model_id = u["current_model"]
    messages_payload = [{"role": "system", "content": sys_prompt}]

    if message.photo:
        # Если текущая модель не поддерживает Vision, автоматически берем бесплатную с поддержкой фото (gpt-6-luna)
        if not models_db.get(model_id, {}).get("vision", False):
            model_id = "gpt-6-luna"
        b64_data = await photo_to_base64(message.photo[-1].file_id)
        messages_payload.append({
            "role": "user",
            "content": [
                {"type": "text", "text": raw_text or "Проанализируй это изображение"},
                {"type": "image_url", "image_url": {"url": b64_data}}
            ]
        })
    else:
        messages_payload.append({"role": "user", "content": raw_text or "Привет"})

    # 11. СТРИМИНГ ОТВЕТА (Редактирование по словам -> Удаление -> Чистая отправка)
    temp_msg = await message.answer("⏳ Думаю...")
    full_response = ""
    last_edit_time = time.time()

    try:
        stream = await ai_client.chat.completions.create(
            model=model_id,
            messages=messages_payload,
            stream=True
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                full_response += chunk.choices[0].delta.content
                # Редактируем сообщение раз в 1.2 сек, чтобы Telegram не заблокировал за флуд
                if time.time() - last_edit_time > 1.2 and len(full_response.strip()) > 0:
                    try:
                        await temp_msg.edit_text(full_response[:4000] + " ✍️")
                        last_edit_time = time.time()
                    except Exception:
                        pass

        if not full_response.strip():
            full_response = "⚠️ Модель вернула пустой ответ."

        # Считаем токены (при стриминге оцениваем по длине текста + промпта)
        estimated_tokens = max(50, (len(sys_prompt) + len(raw_text) + len(full_response)) // 3)
        u["tokens"] += estimated_tokens

        # Удаляем черновое сообщение и резко отправляем чистое готовое
        try:
            await temp_msg.delete()
        except Exception:
            pass

        # Если текст длиннее 4096 символов — бьем на части
        for i in range(0, len(full_response), 4000):
            await message.answer(full_response[i:i+4000])

    except Exception as e:
        await temp_msg.edit_text(f"❌ Ошибка API ({model_id}): {str(e)}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
