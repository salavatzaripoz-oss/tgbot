import os
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from openai import AsyncOpenAI

# Переменные читаются напрямую из среды BotHost (dotenv удален)
bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()
ai_client = AsyncOpenAI(api_key=os.getenv("AI_API_KEY"), base_url=os.getenv("AI_BASE_URL"))

ADMIN_ID = int(os.getenv("ADMIN_ID"))
FREE_LIMIT = int(os.getenv("FREE_LIMIT"))
SUB_LIMIT = int(os.getenv("SUB_LIMIT"))
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT")

# In-memory БД для моделей
models_db = {
    "gpt-6.1-sol": {"name": "GPT-6.1 Sol", "premium": True, "vision": True},
    "gpt-6-astra": {"name": "GPT-6 Astra", "premium": True, "vision": True},
    "gpt-6-so": {"name": "GPT-6 SO", "premium": False, "vision": True},
    "gpt-6-luna": {"name": "GPT-6 Luna", "premium": False, "vision": True},
    "opus-5.5": {"name": "Claude 5.5 Opus", "premium": True, "vision": True},
    "opus-5": {"name": "Claude 5 Opus", "premium": True, "vision": True},
    "sonnet-5.5": {"name": "Claude 5.5 Sonnet", "premium": True, "vision": True},
    "sonnet-5": {"name": "Claude 5 Sonnet", "premium": False, "vision": True},
    "fable-5.1": {"name": "Claude 5.1 Fable", "premium": False, "vision": False},
    "gemini-4-argon": {"name": "Gemini 4 Argon", "premium": True, "vision": True},
    "gemini-3.8-flash": {"name": "Gemini 3.8 Flash", "premium": False, "vision": True},
    "gemini-3.6-flash": {"name": "Gemini 3.6 Flash", "premium": False, "vision": True},
    "gemini-3.5-flash-lite": {"name": "Gemini 3.5 Flash Lite", "premium": False, "vision": False},
    "grok-4.7": {"name": "Grok 4.7", "premium": True, "vision": True},
    "deepseek-flash": {"name": "DeepSeek Flash", "premium": False, "vision": False},
    "deepseek-v4.1-flash-1017": {"name": "DeepSeek v4.1", "premium": False, "vision": False},
    "deepseek-r1": {"name": "DeepSeek R1", "premium": True, "vision": False}
}

users_db = {}
anti_spam = {}

def get_user(user_id: int) -> dict:
    now = datetime.now()
    if user_id not in users_db:
        users_db[user_id] = {
            "is_sub": False, 
            "sub_end": None, 
            "tokens": 0, 
            "reset_date": now + timedelta(days=30),
            "current_model": "gpt-6-luna" # Базовая бесплатная модель
        }
    
    user = users_db[user_id]
    
    # Сброс подписки
    if user["is_sub"] and user["sub_end"] and now > user["sub_end"]:
        user["is_sub"] = False
        user["sub_end"] = None
        
    # Сброс токенов
    if now >= user["reset_date"]:
        user["tokens"] = 0
        user["reset_date"] = now + timedelta(days=30)
        
    # Защита, если текущая модель удалена или стала платной без подписки
    current = user["current_model"]
    if current not in models_db or (models_db[current]["premium"] and not user["is_sub"]):
        user["current_model"] = "gpt-6-luna" 
        
    return user

# --- АДМИН ПАНЕЛЬ ---

@dp.message(Command("give_sub"))
async def admin_give_sub(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        _, target_id, days = message.text.split()
        target_id, days = int(target_id), int(days)
        user = get_user(target_id)
        user["is_sub"] = True
        user["sub_end"] = datetime.now() + timedelta(days=days)
        await message.answer(f"✅ Подписка выдана {target_id} на {days} дней.")
    except Exception:
        await message.answer("Формат: /give_sub 123456789 30")

@dp.message(Command("add_model"))
async def admin_add_model(message: Message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split(maxsplit=4)
    if len(args) < 5:
        return await message.answer("Формат: /add_model <id> <premium:1/0> <vision:1/0> <Название>\n"
                                    "Пример: /add_model gpt-4 1 0 GPT-4 Turbo")
    
    _, model_id, is_prem, is_vis, name = args
    models_db[model_id] = {
        "name": name,
        "premium": bool(int(is_prem)),
        "vision": bool(int(is_vis))
    }
    await message.answer(f"✅ Модель `{model_id}` ('{name}') добавлена/обновлена.")

@dp.message(Command("del_model"))
async def admin_del_model(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        model_id = message.text.split()[1]
        if model_id in models_db:
            del models_db[model_id]
            await message.answer(f"✅ Модель `{model_id}` удалена.")
        else:
            await message.answer("❌ Модель не найдена.")
    except IndexError:
        await message.answer("Формат: /del_model <id>")

# --- ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ---

@dp.message(CommandStart())
async def start_cmd(message: Message):
    await message.answer("Привет! Я ИИ-бот.\nКоманды:\n/stats - профиль\n/model - выбор нейросети")

@dp.message(Command("stats"))
async def user_stats(message: Message):
    user = get_user(message.from_user.id)
    limit = SUB_LIMIT if user["is_sub"] else FREE_LIMIT
    used = user["tokens"]
    percent = (used / limit) * 100 if limit > 0 else 100
    status = "Premium 💎" if user["is_sub"] else "Free 🆓"
    model_name = models_db.get(user['current_model'], {}).get('name', user['current_model'])
    
    await message.answer(
        f"👤 Статус: {status}\n"
        f"🧠 Модель: {model_name}\n"
        f"📊 Использовано: {used} / {limit} ({percent:.1f}%)\n"
        f"🔄 Сброс: {user['reset_date'].strftime('%d.%m.%Y')}"
    )

@dp.message(Command("model"))
async def choose_model_cmd(message: Message):
    user = get_user(message.from_user.id)
    buttons = []
    
    for m_id, data in models_db.items():
        prefix = "✅ " if user["current_model"] == m_id else ""
        lock = " 🔒" if data["premium"] and not user["is_sub"] else ""
        vis = " 🖼" if data["vision"] else ""
        
        buttons.append([
            InlineKeyboardButton(
                text=f"{prefix}{data['name']}{vis}{lock}", 
                callback_data=f"model_{m_id}"
            )
        ])
        
    await message.answer("Выберите нейросеть:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("model_"))
async def process_model_selection(callback: CallbackQuery):
    selected = callback.data.replace("model_", "")
    if selected not in models_db:
        return await callback.answer("❌ Модель не найдена!", show_alert=True)

    user = get_user(callback.from_user.id)
    if models_db[selected]["premium"] and not user["is_sub"]:
        return await callback.answer("💎 Доступно только по подписке Premium", show_alert=True)
    
    user["current_model"] = selected
    await callback.message.edit_text(f"✅ Текущая модель: {models_db[selected]['name']}")
    await callback.answer()

@dp.message(F.text | F.photo)
async def handle_ai_request(message: Message):
    user_id = message.from_user.id
    now = datetime.now()
    
    if user_id in anti_spam and (now - anti_spam[user_id]).total_seconds() < 2:
        return await message.answer("⏳ Не так быстро.")
    anti_spam[user_id] = now

    user = get_user(user_id)
    limit = SUB_LIMIT if user["is_sub"] else FREE_LIMIT
    
    if user["tokens"] >= limit:
        return await message.answer("❌ Лимит токенов исчерпан.")

    model_id = user["current_model"]
    model_info = models_db.get(model_id, {"vision": False})
    
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if message.photo:
        if not model_info["vision"]:
            return await message.answer("❌ Выбранная модель не поддерживает картинки. Выберите другую через /model (отмечены 🖼).")
            
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)
        file_url = f"https://api.telegram.org/file/bot{os.getenv('BOT_TOKEN')}/{file.file_path}"
        prompt_text = message.caption or "Опиши это изображение"
        
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": file_url}}
            ]
        })
    else:
        messages.append({"role": "user", "content": message.text})

    msg = await message.answer("⏳ Обработка...")

    try:
        response = await ai_client.chat.completions.create(
            model=model_id,
            messages=messages
        )
        
        answer = response.choices[0].message.content
        user["tokens"] += response.usage.total_tokens
        
        await msg.edit_text(answer)

    except Exception as e:
        await msg.edit_text(f"❌ Ошибка API: {str(e)}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
