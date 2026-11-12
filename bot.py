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

# Поддержка поиска в интернете
try:
    from duckduckgo_search import DDGS
    SEARCH_AVAILABLE = True
except ImportError:
    SEARCH_AVAILABLE = False

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "8130566129:AAHGfVKiIorLhqGAU_HtbCPjSyXQlWgbzVw").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "5656256968").strip())
AI_API_KEY = os.getenv("AI_API_KEY", "sk-eF2ZQEE1uyLh6VRq1x26zryHbbHqKT7plceJQ37U3Au1IrHm").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://anymodel.ghostly.su/v1").strip()

FREE_LIMIT = int(os.getenv("FREE_LIMIT", "45000").strip())
SUB_LIMIT = int(os.getenv("SUB_LIMIT", "100000").strip())
BASE_SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "Ты полезный ИИ-ассистент. Отвечай четко, структурированно и по делу.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)

# --- БАЗА МОДЕЛЕЙ (С поддержкой статуса доступности) ---
DEFAULT_MODEL = "gpt-6-luna"

models_db = {
    "deepseek-flash": {"name": "DeepSeek Flash", "premium": False, "vision": False, "available": True},
    "deepseek-v4.1-flash-1017": {"name": "DeepSeek v4.1", "premium": False, "vision": False, "available": True},
    "gemini-3.5-flash-lite": {"name": "Gemini 3.5 Lite", "premium": False, "vision": False, "available": True},
    "gemini-3.6-flash": {"name": "Gemini 3.6 Flash", "premium": False, "vision": True, "available": True},
    "gemini-3.8-flash": {"name": "Gemini 3.8 Flash", "premium": False, "vision": True, "available": True},
    "gpt-6-luna": {"name": "GPT-6 Luna", "premium": False, "vision": True, "available": True},
    "gpt-6-so": {"name": "GPT-6 SO", "premium": False, "vision": True, "available": True},
    "sonnet-5": {"name": "Claude 5 Sonnet", "premium": False, "vision": True, "available": True},
    "fable-5.1": {"name": "Claude 5.1 Fable", "premium": False, "vision": False, "available": True},
    "gpt-6.1-sol": {"name": "GPT-6.1 Sol", "premium": True, "vision": True, "available": True},
    "gpt-6-astra": {"name": "GPT-6 Astra", "premium": True, "vision": True, "available": True},
    "opus-5.5": {"name": "Claude 5.5 Opus", "premium": True, "vision": True, "available": True},
    "opus-5": {"name": "Claude 5 Opus", "premium": True, "vision": True, "available": True},
    "sonnet-5.5": {"name": "Claude 5.5 Sonnet", "premium": True, "vision": True, "available": True},
    "gemini-4-argon": {"name": "Gemini 4 Argon", "premium": True, "vision": True, "available": True},
    "grok-4.7": {"name": "Grok 4.7", "premium": True, "vision": True, "available": True},
    "deepseek-r1": {"name": "DeepSeek R1", "premium": True, "vision": False, "available": True}
}

users_db = {}
anti_spam = {}
admin_reply_target = {}

# --- ФОНОВЫЙ ПИНГЕР МОДЕЛЕЙ ---
async def model_checker_loop():
    global DEFAULT_MODEL
    while True:
        for m_id in list(models_db.keys()):
            try:
                # Отправляем микро-запрос для проверки работоспособности
                await ai_client.chat.completions.create(
                    model=m_id,
                    messages=[{"role": "user", "content": "1"}],
                    max_tokens=1
                )
                models_db[m_id]["available"] = True
            except Exception:
                models_db[m_id]["available"] = False
                # Если дефолтная модель упала — ищем первую рабочую бесплатную
                if m_id == DEFAULT_MODEL:
                    for alt_id, alt_data in models_db.items():
                        if alt_data["available"] and not alt_data["premium"]:
                            DEFAULT_MODEL = alt_id
                            break
            await asyncio.sleep(3) # Пауза между проверками моделей

# --- УПРАВЛЕНИЕ ЮЗЕРАМИ ---
def get_user(user_id: int) -> dict:
    now = datetime.now()
    if user_id not in users_db:
        users_db[user_id] = {
            "is_sub": False, "sub_end": None, "tokens": 0, "reset_date": now + timedelta(days=30),
            "current_model": DEFAULT_MODEL, "custom_prompt": "", "web_search": False,
            "banned": False, "strikes": 0, "history": [], "state": None,
            "school": {
                "active": False, "grade": "9 класс", "quarter": "1 четверть",
                "level": "Средний", "hw_type": "Письменное", "topic": "Общая",
                "training_mode": False, "training_samples": []
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
    if curr not in models_db or not models_db[curr]["available"] or (models_db[curr]["premium"] and not user["is_sub"]):
        user["current_model"] = DEFAULT_MODEL
        
    return user

# --- КЛАВИАТУРЫ ---
def get_main_kb(user_id: int):
    u = get_user(user_id)
    web_Icon = "🟢 ВКЛ" if u["web_search"] else "🔴 ВЫКЛ"
    school_Icon = "🎓 Школа (Активна)" if u["school"]["active"] else "🎓 Режим Школы"
    kb = [
        [KeyboardButton(text="🧠 Модели ИИ"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text=school_Icon), KeyboardButton(text=f"🌐 Поиск: {web_Icon}")],
        [KeyboardButton(text="⚙️ Свой промпт"), KeyboardButton(text="🧹 Очистить память")],
        [KeyboardButton(text="💎 Подписка / Связь")]
    ]
    if user_id == ADMIN_ID: kb.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_school_main_kb(u: dict):
    s = u["school"]
    status = "🟢 ВКЛЮЧЕН" if s["active"] else "🔴 ВЫКЛЮЧЕН"
    buttons = [
        [InlineKeyboardButton(text=f"Статус режима: {status}", callback_data="sch_toggle")],
        [InlineKeyboardButton(text=f"🏫 Класс: {s['grade']}", callback_data="sch_menu_grade"),
         InlineKeyboardButton(text=f"📅 Четверть: {s['quarter']}", callback_data="sch_menu_quarter")],
        [InlineKeyboardButton(text=f"📊 Уровень: {s['level']}", callback_data="sch_menu_level"),
         InlineKeyboardButton(text=f"📝 Д/З: {s['hw_type']}", callback_data="sch_menu_hw")],
        [InlineKeyboardButton(text=f"📌 Тема: {s['topic'][:20]}", callback_data="sch_topic")]
    ]
    if s["hw_type"] == "Письменное":
        train_st = "🟢 ВКЛ" if s["training_mode"] else "🔴 ВЫКЛ"
        cnt = len(s["training_samples"])
        buttons.append([InlineKeyboardButton(text=f"🧠 Режим Обучения: {train_st} (Примеров: {cnt})", callback_data="sch_train_toggle")])
        if cnt > 0:
            buttons.append([InlineKeyboardButton(text="🗑 Очистить примеры", callback_data="sch_train_clear")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
async def extract_document_text(message: Message) -> str:
    doc = message.document
    if doc.file_size > 3 * 1024 * 1024:
        return "[Файл слишком большой (макс 3 МБ)]"
    file = await bot.get_file(doc.file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    try: return bio.getvalue().decode("utf-8")
    except UnicodeDecodeError: return bio.getvalue().decode("cp1251", errors="ignore")

async def search_internet(query: str) -> str:
    if not SEARCH_AVAILABLE: return ""
    def fetch():
        with DDGS() as ddgs: return list(ddgs.text(query, max_results=3))
    try:
        results = await asyncio.to_thread(fetch)
        if not results: return ""
        text = "АКТУАЛЬНЫЕ ДАННЫЕ ИЗ СЕТИ:\n"
        for r in results: text += f"- {r.get('body')}\n"
        return text
    except Exception: return ""

async def photo_to_base64(file_id: str) -> str:
    file = await bot.get_file(file_id)
    bio = io.BytesIO()
    await bot.download_file(file.file_path, destination=bio)
    encoded = base64.b64encode(bio.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{encoded}"

async def check_censorship(text: str) -> tuple[bool, str]:
    if not text or len(text.strip()) < 3: return True, ""
    try:
        resp = await ai_client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": "Проверь текст на опасный контент. Если всё норм - пиши SAFE. Если нарушение (насилие, ЦП, взлом, наркотики) - UNSAFE | причина"},
                {"role": "user", "content": text[:1000]}
            ],
            max_tokens=20
        )
        ans = resp.choices[0].message.content.strip()
        if ans.startswith("UNSAFE"): return False, ans.split("|", 1)[-1].strip() if "|" in ans else "Ответь безопасно."
        return True, ""
    except: return True, ""

# --- БАЗОВЫЕ КОМАНДЫ ---
@dp.message(CommandStart())
async def start_cmd(message: Message):
    u = get_user(message.from_user.id)
    if u["banned"]: return await message.answer("🚫 Вы заблокированы.")
    u["state"] = None
    await message.answer("👋 **Добро пожаловать в ШколоAI!**", reply_markup=get_main_kb(message.from_user.id), parse_mode="Markdown")

@dp.message(F.text == "🧹 Очистить память")
async def clear_memory(message: Message):
    get_user(message.from_user.id)["history"] = []
    await message.answer("🧹 **Контекст очищен!** ИИ забыл предыдущий диалог (полезно при смене темы).", parse_mode="Markdown")

@dp.message(F.text == "👤 Профиль")
@dp.message(Command("stats"))
async def show_profile(message: Message):
    uid = message.from_user.id; u = get_user(uid)
    limit = SUB_LIMIT if u["is_sub"] else FREE_LIMIT
    pct = (u["tokens"] / limit) * 100 if limit > 0 else 100
    st = "💎 Premium" if u["is_sub"] else "🆓 Бесплатно"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💎 Купить подписку", callback_data="buy_sub_prompt")]]) if not u["is_sub"] else None
    await message.answer(f"👤 **Профиль**\n🏷 Статус: {st}\n🧠 Модель: {models_db.get(u['current_model'], {}).get('name')}\n📊 Токены: `{u['tokens']} / {limit}` ({pct:.1f}%)\n🔄 Сброс: {u['reset_date'].strftime('%d.%m.%Y')}", reply_markup=kb, parse_mode="Markdown")

# --- МЕНЮ МОДЕЛЕЙ ---
@dp.message(F.text == "🧠 Модели ИИ")
async def menu_models(message: Message):
    u = get_user(message.from_user.id)
    buttons = []
    for m_id, data in models_db.items():
        if not data["available"]:
            buttons.append([InlineKeyboardButton(text=f"🔴 {data['name']} (Офлайн)", callback_data="ignore")])
            continue
        prefix = "✅ " if u["current_model"] == m_id else ""
        lock = " 🔒" if data["premium"] and not u["is_sub"] else ""
        vis = " 🖼" if data["vision"] else ""
        buttons.append([InlineKeyboardButton(text=f"{prefix}{data['name']}{vis}{lock}", callback_data=f"setmod_{m_id}")])
    await message.answer("🧠 **Выберите нейросеть:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("setmod_"))
async def cb_set_model(callback: CallbackQuery):
    m_id = callback.data.replace("setmod_", "")
    u = get_user(callback.from_user.id)
    if models_db[m_id]["premium"] and not u["is_sub"] and callback.from_user.id != ADMIN_ID:
        return await callback.answer("🔒 Нужна подписка Premium!", show_alert=True)
    u["current_model"] = m_id
    await callback.message.edit_text(f"✅ Выбрана модель: **{models_db[m_id]['name']}**", parse_mode="Markdown")

# --- СВОЙ ПРОМПТ ---
@dp.message(F.text == "⚙️ Свой промпт")
async def menu_custom_prompt(message: Message):
    u = get_user(message.from_user.id)
    u["state"] = "wait_prompt"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Сбросить промпт", callback_data="reset_prompt")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]
    ])
    await message.answer(f"⚙️ **Ваш текущий промпт:** _{u['custom_prompt'] or 'Стандартный'}_\nНапишите в чат новый системный промпт:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "reset_prompt")
async def cb_reset_prompt(callback: CallbackQuery):
    get_user(callback.from_user.id)["custom_prompt"] = ""
    await callback.message.edit_text("✅ Промпт сброшен.")

@dp.callback_query(F.data == "cancel_state")
async def cb_cancel_state(callback: CallbackQuery):
    get_user(callback.from_user.id)["state"] = None
    await callback.message.edit_text("❌ Действие отменено.")

# --- ПОДПИСКА И СВЯЗЬ ---
@dp.message(F.text == "💎 Подписка / Связь")
async def menu_subscription(message: Message):
    get_user(message.from_user.id)["state"] = "support_chat"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]])
    await message.answer("💎 **Premium (100к токенов + Топ ИИ)**\n\n✍️ Напишите любое сообщение админу прямо сейчас для оформления или связи:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data == "buy_sub_prompt")
async def cb_buy_sub_prompt(callback: CallbackQuery):
    get_user(callback.from_user.id)["state"] = "support_chat"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state")]])
    await callback.message.answer("✍️ **Напишите сообщение администратору:**", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

# --- АДМИН ПАНЕЛЬ ---
@dp.message(F.text == "👑 Админ-панель")
@dp.message(Command("admin"))
async def admin_panel_cmd(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("👑 **Админка**\n`/give_sub <ID> <дней>`\n`/ban <ID>`", parse_mode="Markdown")

@dp.message(Command("give_sub"))
async def cmd_give_sub(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        _, tid, days = message.text.split()
        tid, days = int(tid), int(days)
        u = get_user(tid)
        u["is_sub"], u["sub_end"] = True, datetime.now() + timedelta(days=days)
        await message.answer(f"✅ Выдано `{tid}` на {days} дней.", parse_mode="Markdown")
        try: await bot.send_message(tid, f"🎉 Вам выдана подписка Premium на {days} дней!")
        except: pass
    except: await message.answer("Формат: `/give_sub ID 30`", parse_mode="Markdown")

@dp.message(Command("ban"))
async def cmd_ban_user(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        tid = int(message.text.split()[1])
        get_user(tid)["banned"] = not get_user(tid)["banned"]
        await message.answer(f"✅ Статус бана `{tid}` изменен.", parse_mode="Markdown")
    except: await message.answer("Формат: `/ban ID`", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_"))
async def cb_admin_actions(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    data = callback.data
    if data.startswith("adm_ban_"):
        get_user(int(data.replace("adm_ban_", "")))["banned"] = True
        await callback.answer("Пользователь забанен!")
    elif data.startswith("adm_sub_"):
        tid = int(data.replace("adm_sub_", ""))
        u = get_user(tid)
        u["is_sub"], u["sub_end"] = True, datetime.now() + timedelta(days=30)
        await callback.answer("Подписка выдана!")
        try: await bot.send_message(tid, "🎉 Выдана подписка Premium!")
        except: pass
    elif data.startswith("adm_reply_"):
        tid = int(data.replace("adm_reply_", ""))
        admin_reply_target[ADMIN_ID] = tid
        await callback.message.answer(f"✍️ Напишите ответ для `{tid}`:", parse_mode="Markdown")
        await callback.answer()

# --- ШКОЛА СЕТКИ ---
@dp.message(F.text.startswith("🎓"))
async def menu_school(message: Message):
    await message.answer("🎓 **Режим Школа**", reply_markup=get_school_main_kb(get_user(message.from_user.id)), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("sch_menu_"))
async def cb_school_menus(callback: CallbackQuery):
    action = callback.data.replace("sch_menu_", "")
    buttons = []
    if action == "grade":
        row = []
        for i in range(1, 12):
            row.append(InlineKeyboardButton(text=f"{i} кл", callback_data=f"sch_set_grade_{i} класс"))
            if len(row) == 4: buttons.append(row); row = []
        if row: buttons.append(row)
    elif action == "quarter":
        for i in range(1, 5): buttons.append([InlineKeyboardButton(text=f"{i} четверть", callback_data=f"sch_set_quarter_{i} четверть")])
    elif action == "level":
        for l in ["Начальный (просто)", "Средний", "Продвинутый (Олимпиада)"]: buttons.append([InlineKeyboardButton(text=l, callback_data=f"sch_set_level_{l}")])
    elif action == "hw":
        for h in ["Письменное", "Проект", "Творческое", "Пересказ"]: buttons.append([InlineKeyboardButton(text=h, callback_data=f"sch_set_hw_{h}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="sch_back_main")])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("sch_set_"))
async def cb_school_set(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    param, val = callback.data.replace("sch_set_", "").split("_", 1)
    if param in ["grade", "quarter", "level", "hw"]: u["school"][param] = val
    await callback.message.edit_reply_markup(reply_markup=get_school_main_kb(u))

@dp.callback_query(F.data == "sch_back_main")
async def cb_school_back(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=get_school_main_kb(get_user(callback.from_user.id)))

@dp.callback_query(F.data.startswith("sch_"))
async def cb_school_others(callback: CallbackQuery):
    u = get_user(callback.from_user.id)
    if callback.data == "sch_toggle": u["school"]["active"] = not u["school"]["active"]
    elif callback.data == "sch_topic":
        u["state"] = "school_topic"
        return await callback.message.answer("📌 Введите название темы:")
    elif callback.data == "sch_train_toggle": u["school"]["training_mode"] = not u["school"]["training_mode"]
    elif callback.data == "sch_train_clear": u["school"]["training_samples"].clear()
    await callback.message.edit_reply_markup(reply_markup=get_school_main_kb(u))

# --- ПЕРЕКЛЮЧАТЕЛИ ---
@dp.message(F.text.startswith("🌐 Поиск:"))
async def toggle_web(message: Message):
    u = get_user(message.from_user.id)
    u["web_search"] = not u["web_search"]
    await message.answer(f"🌐 Поиск: {'ВКЛ' if u['web_search'] else 'ВЫКЛ'}", reply_markup=get_main_kb(message.from_user.id))

# --- ГЛАВНЫЙ ОБРАБОТЧИК ---
@dp.message(F.text | F.photo | F.document)
async def handle_requests(message: Message):
    uid = message.from_user.id; u = get_user(uid)
    if u["banned"]: return

    # Ответ админа
    if uid == ADMIN_ID and ADMIN_ID in admin_reply_target and message.text:
        target_id = admin_reply_target.pop(ADMIN_ID)
        try:
            await bot.send_message(target_id, f"👨‍💻 **Администратор:**\n\n{message.text}", parse_mode="Markdown")
            return await message.answer("✅ Ответ доставлен.")
        except Exception as e: return await message.answer(f"❌ Ошибка отправки: {e}")

    # Чат поддержки
    if u["state"] == "support_chat" and message.text:
        u["state"] = None
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"adm_reply_{uid}"), InlineKeyboardButton(text="💎 Выдать Sub (30д)", callback_data=f"adm_sub_{uid}")],
            [InlineKeyboardButton(text="🚫 Бан", callback_data=f"adm_ban_{uid}")]
        ])
        await bot.send_message(ADMIN_ID, f"📩 **Связь от `{uid}`**:\n\n{message.text}", reply_markup=kb, parse_mode="Markdown")
        return await message.answer("✅ Сообщение отправлено администратору!", reply_markup=get_main_kb(uid))

    # Свой промпт
    if u["state"] == "wait_prompt" and message.text:
        is_safe, rule = await check_censorship(message.text)
        if not is_safe: return await message.answer("❌ Промпт не прошел цензуру.")
        u["custom_prompt"], u["state"] = message.text[:400], None
        return await message.answer("✅ Промпт сохранен!")

    # Тема школы
    if u["state"] == "school_topic" and message.text:
        u["school"]["topic"] = message.text[:50]; u["state"] = None
        return await message.answer(f"✅ Тема Школы: {u['school']['topic']}", reply_markup=get_school_main_kb(u))

    # Анти-спам
    now = datetime.now()
    if uid in anti_spam and (now - anti_spam[uid]).total_seconds() < 2: return
    anti_spam[uid] = now

    limit = SUB_LIMIT if u["is_sub"] else FREE_LIMIT
    if u["tokens"] >= limit and uid != ADMIN_ID: return await message.answer("❌ Лимит токенов исчерпан.")

    # Обработка документов
    raw_text = message.text or message.caption or ""
    if message.document:
        doc_content = await extract_document_text(message)
        raw_text += f"\n\n[Документ {message.document.file_name}]:\n{doc_content[:4000]}"
    
    # Обучение (Школа)
    s = u["school"]
    if s["active"] and s["hw_type"] == "Письменное" and s["training_mode"]:
        if message.photo:
            b64_img = await photo_to_base64(message.photo[-1].file_id)
            vis_mod = u["current_model"] if models_db[u["current_model"]]["vision"] else "gpt-6-luna"
            try:
                res = await ai_client.chat.completions.create(
                    model=vis_mod,
                    messages=[{"role": "user", "content": [{"type": "text", "text": "Разбери алгоритм и оформление решения с этого фото:"}, {"type": "image_url", "image_url": {"url": b64_img}}]}]
                )
                s["training_samples"].append(f"[Пример]: {res.choices[0].message.content}")
                u["tokens"] += res.usage.total_tokens if res.usage else 200
            except Exception as e: return await message.answer(f"❌ Ошибка распознавания: {e}")
        else:
            s["training_samples"].append(f"[Пример текста]: {raw_text}")
        return await message.answer(f"🧠 Пример #{len(s['training_samples'])} сохранен! Отключи Режим Обучения для решения ДЗ.", reply_markup=get_school_main_kb(u))

    # Цензура
    is_safe, safety_rule = await check_censorship(raw_text)
    if not is_safe:
        u["strikes"] += 1
        if u["strikes"] >= 3:
            try: await bot.send_message(ADMIN_ID, f"⚠️ Бан-лог: {uid} | Запрос: {raw_text[:200]}")
            except: pass

    # Кубик Школы
    dice_style = ""
    if s["active"] and s["hw_type"] in ["Проект", "Творческое", "Пересказ"]:
        dm = await message.answer_dice("🎲"); await asyncio.sleep(2.2)
        styles = {1: "Строго научно", 2: "Креативно с примерами", 3: "С тезисами и планом", 4: "Сторителлинг", 5: "Аналитически за и против", 6: "Подробно на 5+"}
        dice_style = styles.get(dm.dice.value, "")
        await message.answer(f"🎲 Выпало {dm.dice.value}! Стиль: {dice_style}")

    # Сборка промпта
    sys_prompt = BASE_SYSTEM_PROMPT
    if u["custom_prompt"]: sys_prompt += f"\nСвой промпт юзера: {u['custom_prompt']}"
    if s["active"]:
        sys_prompt += f"\n[ШКОЛА]: Ученик {s['grade']}, {s['quarter']}, уровень: {s['level']}. Тема: {s['topic']}. Тип: {s['hw_type']}."
        if dice_style: sys_prompt += f" Стиль выполнения: {dice_style}."
        if s["topic"] != "Общая":
            edu_ctx = await search_internet(f"школьная программа {s['grade']} тема {s['topic']} что должны знать")
            if edu_ctx: sys_prompt += f"\n[ЗНАНИЯ КЛАССА]: {edu_ctx}"
        if s["hw_type"] == "Письменное" and s["training_samples"]:
            sys_prompt += "\nИспользуй строго этот метод решения: " + "\n".join(s["training_samples"][-5:])
            
    if u["web_search"] and raw_text:
        net = await search_internet(raw_text)
        if net: sys_prompt += f"\n[ДАННЫЕ ИЗ ИНТЕРНЕТА]:\n{net}\nТЫ ИМЕЕШЬ доступ к интернету. Обязательно используй эти данные в ответе!"
        
    messages_payload = [{"role": "system", "content": sys_prompt}]
    
    # История
    for msg in u["history"]: messages_payload.append(msg)
    
    # Содержимое
    if message.photo:
        mod = u["current_model"] if models_db[u["current_model"]]["vision"] else "gpt-6-luna"
        b64 = await photo_to_base64(message.photo[-1].file_id)
        current_msg = {"role": "user", "content": [{"type": "text", "text": raw_text or "Опиши фото"}, {"type": "image_url", "image_url": {"url": b64}}]}
        messages_payload.append(current_msg)
    else:
        current_msg = {"role": "user", "content": raw_text}
        messages_payload.append(current_msg)

    # Отправка и стриминг
    temp = await message.answer("⏳ Думаю...")
    resp_text = ""; last_ed = time.time()
    
    try:
        stream = await ai_client.chat.completions.create(model=u["current_model"], messages=messages_payload, stream=True)
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                resp_text += chunk.choices[0].delta.content
                if time.time() - last_ed > 1.2 and resp_text:
                    try: await temp.edit_text(resp_text[:4000] + " ✍️"); last_ed = time.time()
                    except: pass
        
        try: await temp.delete()
        except: pass
        
        for i in range(0, len(resp_text), 4000): await message.answer(resp_text[i:i+4000])
        
        u["history"].append({"role": "user", "content": raw_text if not message.photo else "[Фото/Документ]"})
        u["history"].append({"role": "assistant", "content": resp_text})
        if len(u["history"]) > 10: u["history"] = u["history"][-10:] # Ограничение памяти до 5 последних пар
        u["tokens"] += max(50, len(resp_text) // 3)

    except Exception as e:
        await temp.edit_text(f"❌ Ошибка API: {str(e)}")

async def main():
    asyncio.create_task(model_checker_loop()) # Запуск пингера моделей в фоне
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
