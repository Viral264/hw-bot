import asyncio
import logging
import os
import sqlite3
from datetime import datetime, date, timedelta
 
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
 
# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ВАШ_ТОКЕН")
DB_PATH = os.path.join(os.path.dirname(__file__), "homework.db")
 
logging.basicConfig(level=logging.INFO)
 
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)
 
 
# ============ БАЗА ДАННЫХ ============
# ВАЖНО: задания привязаны к chat_id (а не к user_id), поэтому все участники
# одного группового чата видят один общий список. В личке с ботом chat_id
# совпадает с вашим личным id, так что там всё работает как и раньше.
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            added_by TEXT,
            subject TEXT NOT NULL,
            description TEXT NOT NULL,
            deadline TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            done_by TEXT,
            reminded INTEGER DEFAULT 0,
            file_id TEXT,
            file_type TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # На случай, если база уже существовала в старом формате (по user_id)
    cur.execute("PRAGMA table_info(homework)")
    cols = [row[1] for row in cur.fetchall()]
    if "chat_id" not in cols and "user_id" in cols:
        cur.execute("ALTER TABLE homework RENAME COLUMN user_id TO chat_id")
    for col, coltype in (
        ("file_id", "TEXT"), ("file_type", "TEXT"),
        ("added_by", "TEXT"), ("done_by", "TEXT"),
    ):
        try:
            cur.execute(f"ALTER TABLE homework ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # колонка уже есть
    conn.commit()
    conn.close()
 
 
def add_homework(chat_id: int, added_by: str, subject: str, description: str, deadline: str,
                  file_id: str | None = None, file_type: str | None = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO homework (chat_id, added_by, subject, description, deadline, file_id, file_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, added_by, subject, description, deadline, file_id, file_type, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
 
 
def get_homework(only_pending=True, days_ahead: int | None = None):
    """Список общий для всех — не привязан к конкретному чату, поэтому
    его видно и в личке с ботом, и в любой группе, куда бот добавлен."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = ("SELECT id, subject, description, deadline, done, file_id, file_type, added_by, done_by "
              "FROM homework WHERE 1=1")
    params = []
    if only_pending:
        query += " AND done = 0"
    if days_ahead is not None:
        today = date.today()
        limit = today + timedelta(days=days_ahead)
        query += " AND date(deadline) BETWEEN date(?) AND date(?)"
        params += [today.isoformat(), limit.isoformat()]
    query += " ORDER BY date(deadline) ASC"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows
 
 
def get_one(hw_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, subject, description, deadline, done, file_id, file_type, added_by, done_by "
        "FROM homework WHERE id = ?",
        (hw_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row
 
 
def mark_done(hw_id: int, done_by: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "UPDATE homework SET done = 1, done_by = ? WHERE id = ?",
        (done_by, hw_id),
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed
 
 
def delete_homework(hw_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM homework WHERE id = ?", (hw_id,))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed
 
 
def get_due_tomorrow_unreminded():
    """chat_id тут — это чат, где добавили задание (обычно личка с ботом);
    именно туда и уйдёт персональное напоминание добавившему."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    cur.execute(
        "SELECT id, chat_id, subject, description FROM homework "
        "WHERE date(deadline) = ? AND done = 0 AND reminded = 0",
        (tomorrow,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows
 
 
def mark_reminded(hw_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE homework SET reminded = 1 WHERE id = ?", (hw_id,))
    conn.commit()
    conn.close()
 
 
def display_name(user) -> str:
    """Имя пользователя для подписи 'добавил(а) ...'."""
    if user.username:
        return f"@{user.username}"
    return user.full_name
 
 
# ============ КЛАВИАТУРЫ ============
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="📋 Список")],
            [KeyboardButton(text="🔥 Сегодня"), KeyboardButton(text="📆 Неделя")],
        ],
        resize_keyboard=True,
    )
 
 
def hw_actions_kb(hw_id: int, has_file: bool) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{hw_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{hw_id}"),
        ]
    ]
    if has_file:
        buttons.append([InlineKeyboardButton(text="📎 Показать файл", callback_data=f"file:{hw_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
 
 
def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_file")]]
    )
 
 
# ============ КОМАНДЫ БОТА (меню "/" в Telegram) ============
async def set_bot_commands():
    commands = [
        BotCommand(command="start", description="Начать / показать меню"),
        BotCommand(command="add", description="Добавить домашнее задание"),
        BotCommand(command="list", description="Список невыполненных дз"),
        BotCommand(command="today", description="Задания на сегодня"),
        BotCommand(command="week", description="Задания на неделю"),
        BotCommand(command="done", description="Отметить выполненным: /done <id>"),
        BotCommand(command="delete", description="Удалить задание: /delete <id>"),
    ]
    await bot.set_my_commands(commands)
 
 
# ============ FSM ДЛЯ ДОБАВЛЕНИЯ ДЗ ============
class AddHomework(StatesGroup):
    subject = State()
    description = State()
    deadline = State()
    attachment = State()
 
 
def parse_date(text: str) -> str | None:
    text = text.strip()
    formats = ["%d.%m.%Y", "%d.%m.%y", "%d.%m"]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%d.%m":
                dt = dt.replace(year=date.today().year)
            return dt.date().isoformat()
        except ValueError:
            continue
    return None
 
 
def format_hw_line(hw_id, subject, description, deadline, done, file_id=None, file_type=None,
                    added_by=None, done_by=None) -> str:
    d = datetime.strptime(deadline, "%Y-%m-%d").date()
    days_left = (d - date.today()).days
    if days_left < 0:
        status = "⚠️ просрочено"
    elif days_left == 0:
        status = "🔥 сегодня"
    elif days_left == 1:
        status = "⏰ завтра"
    else:
        status = f"через {days_left} дн."
    mark = "✅" if done else "▫️"
    file_note = " 📎" if file_id else ""
    line = (
        f"{mark} #{hw_id} [{subject}] {description}{file_note}\n"
        f"   📅 {d.strftime('%d.%m.%Y')} ({status})"
    )
    if added_by:
        line += f"\n   👤 добавил(а): {added_by}"
    if done and done_by:
        line += f"\n   ✅ выполнил(а): {done_by}"
    return line
 
 
# ============ БАЗОВЫЕ КОМАНДЫ ============
@router.message(CommandStart())
async def cmd_start(message: Message):
    is_group = message.chat.type in ("group", "supergroup")
    if is_group:
        await message.answer(
            "👋 Привет! Список домашних заданий общий для всех.\n\n"
            "В этом чате можно:\n"
            "/list — посмотреть список\n"
            "/today — что сдавать сегодня\n"
            "/week — что сдавать на неделе\n"
            "/done <id> — отметить выполненным\n\n"
            "✏️ А вот добавлять новые задания можно только в личном чате со мной."
        )
        return
    await message.answer(
        "👋 Привет! Я бот для отслеживания домашних заданий.\n\n"
        "Используй кнопки внизу или команды из меню («/»):\n"
        "/add — добавить дз\n"
        "/list — список невыполненных дз\n"
        "/today — что сдавать сегодня\n"
        "/week — что сдавать на этой неделе\n"
        "/done <id> — отметить как выполненное\n"
        "/delete <id> — удалить задание\n\n"
        "К заданию можно прикрепить файл (фото или документ).\n\n"
        "📢 Добавь меня в общий чат класса — там все смогут смотреть список "
        "командой /list, а добавлять новые задания сможешь только ты, в этой личке.",
        reply_markup=main_menu_kb(),
    )
 
 
# ============ ДОБАВЛЕНИЕ ДЗ (только в личном чате с ботом) ============
@router.message(Command("add"), F.chat.type.in_({"group", "supergroup"}))
@router.message(F.text == "➕ Добавить", F.chat.type.in_({"group", "supergroup"}))
async def cmd_add_blocked_in_group(message: Message):
    await message.answer(
        "✋ Добавлять задания можно только в личном чате с ботом.\n"
        f"Напишите мне в личку: @{(await bot.me()).username}, и там используйте /add.\n"
        "А смотреть список — можно прямо здесь, командой /list."
    )
 
 
@router.message(Command("add"))
@router.message(F.text == "➕ Добавить")
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddHomework.subject)
    await message.answer("📚 По какому предмету задание?")
 
 
@router.message(AddHomework.subject)
async def process_subject(message: Message, state: FSMContext):
    await state.update_data(subject=message.text.strip())
    await state.set_state(AddHomework.description)
    await message.answer("📝 Что нужно сделать? (опишите задание)")
 
 
@router.message(AddHomework.description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(AddHomework.deadline)
    await message.answer("📅 Когда дедлайн? (в формате ДД.ММ.ГГГГ, например 15.09.2026)")
 
 
@router.message(AddHomework.deadline)
async def process_deadline(message: Message, state: FSMContext):
    parsed = parse_date(message.text)
    if not parsed:
        await message.answer(
            "❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ (например, 15.09.2026) или ДД.ММ."
        )
        return
    await state.update_data(deadline=parsed)
    await state.set_state(AddHomework.attachment)
    await message.answer(
        "📎 Хотите прикрепить файл к заданию? Пришлите фото или документ.\n"
        "Либо нажмите «Пропустить».",
        reply_markup=skip_kb(),
    )
 
 
async def finish_adding(state: FSMContext, chat_id: int, added_by: str,
                         file_id: str | None = None, file_type: str | None = None) -> str:
    """chat_id тут — id личного чата того, кто добавляет (нужен только для
    напоминаний ему лично); сам список заданий общий для всех."""
    data = await state.get_data()
    add_homework(chat_id, added_by, data["subject"], data["description"], data["deadline"], file_id, file_type)
    await state.clear()
    d = datetime.strptime(data["deadline"], "%Y-%m-%d").date()
    text = (
        f"✅ Добавлено ({added_by})!\n📚 {data['subject']}\n📝 {data['description']}\n"
        f"📅 {d.strftime('%d.%m.%Y')}"
    )
    if file_id:
        text += "\n📎 Файл прикреплён"
    return text
 
 
@router.message(AddHomework.attachment, F.photo)
async def process_attachment_photo(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    text = await finish_adding(state, message.chat.id, display_name(message.from_user), file_id, "photo")
    await message.answer(text, reply_markup=main_menu_kb())
 
 
@router.message(AddHomework.attachment, F.document)
async def process_attachment_document(message: Message, state: FSMContext):
    file_id = message.document.file_id
    text = await finish_adding(state, message.chat.id, display_name(message.from_user), file_id, "document")
    await message.answer(text, reply_markup=main_menu_kb())
 
 
@router.callback_query(AddHomework.attachment, F.data == "skip_file")
async def process_attachment_skip(callback: CallbackQuery, state: FSMContext):
    text = await finish_adding(state, callback.message.chat.id, display_name(callback.from_user))
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text, reply_markup=main_menu_kb())
    await callback.answer()
 
 
@router.message(AddHomework.attachment)
async def process_attachment_invalid(message: Message):
    await message.answer(
        "Пришлите фото или документ, либо нажмите «⏭ Пропустить» кнопкой выше."
    )
 
 
# ============ ПРОСМОТР СПИСКОВ ============
async def send_hw_list(message: Message, rows, empty_text: str, header: str):
    if not rows:
        await message.answer(empty_text)
        return
    await message.answer(header)
    for hw_id, subject, description, deadline, done, file_id, file_type, added_by, done_by in rows:
        text = format_hw_line(hw_id, subject, description, deadline, done, file_id, file_type, added_by, done_by)
        await message.answer(text, reply_markup=hw_actions_kb(hw_id, bool(file_id)))
 
 
@router.message(Command("list"))
@router.message(F.text == "📋 Список")
async def cmd_list(message: Message):
    rows = get_homework()
    await send_hw_list(message, rows, "🎉 Нет невыполненных заданий!", "📋 Общий список домашних заданий:")
 
 
@router.message(Command("today"))
@router.message(F.text == "🔥 Сегодня")
async def cmd_today(message: Message):
    rows = get_homework(days_ahead=0)
    await send_hw_list(message, rows, "Сегодня сдавать ничего не нужно 👍", "🔥 На сегодня:")
 
 
@router.message(Command("week"))
@router.message(F.text == "📆 Неделя")
async def cmd_week(message: Message):
    rows = get_homework(days_ahead=7)
    await send_hw_list(message, rows, "На этой неделе всё сдано или заданий нет 👍", "📆 На неделю:")
 
 
# ============ ГОТОВО / УДАЛИТЬ — через команды ============
@router.message(Command("done"))
async def cmd_done(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /done <id>\nНапример: /done 3")
        return
    hw_id = int(parts[1])
    if mark_done(hw_id, display_name(message.from_user)):
        await message.answer(f"✅ Задание #{hw_id} отмечено как выполненное!")
    else:
        await message.answer("❌ Задание с таким id не найдено.")
 
 
@router.message(Command("delete"))
async def cmd_delete(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /delete <id>\nНапример: /delete 3")
        return
    hw_id = int(parts[1])
    if delete_homework(hw_id):
        await message.answer(f"🗑️ Задание #{hw_id} удалено.")
    else:
        await message.answer("❌ Задание с таким id не найдено.")
 
 
# ============ ГОТОВО / УДАЛИТЬ / ФАЙЛ — через кнопки ============
@router.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    who = display_name(callback.from_user)
    if mark_done(hw_id, who):
        await callback.message.edit_text(f"✅ Задание #{hw_id} отмечено как выполненное ({who})!")
    else:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await callback.answer("Готово!")
 
 
@router.callback_query(F.data.startswith("delete:"))
async def cb_delete(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    who = display_name(callback.from_user)
    if delete_homework(hw_id):
        await callback.message.edit_text(f"🗑️ Задание #{hw_id} удалено ({who}).")
    else:
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await callback.answer("Удалено")
 
 
@router.callback_query(F.data.startswith("file:"))
async def cb_file(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    row = get_one(hw_id)
    if not row or not row[5]:
        await callback.answer("Файл не найден", show_alert=True)
        return
    file_id, file_type = row[5], row[6]
    if file_type == "photo":
        await callback.message.answer_photo(file_id, caption=f"📎 Файл к заданию #{hw_id}")
    else:
        await callback.message.answer_document(file_id, caption=f"📎 Файл к заданию #{hw_id}")
    await callback.answer()
 
 
# ============ НАПОМИНАНИЯ ============
async def send_reminders():
    rows = get_due_tomorrow_unreminded()
    for hw_id, chat_id, subject, description in rows:
        try:
            await bot.send_message(
                chat_id,
                f"⏰ Напоминание! Завтра дедлайн:\n📚 {subject}\n📝 {description}",
            )
            mark_reminded(hw_id)
        except Exception as e:
            logging.warning(f"Не удалось отправить напоминание {hw_id}: {e}")
 
 
async def main():
    init_db()
    await set_bot_commands()
 
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "cron", hour=20, minute=0)
    scheduler.start()
 
    logging.info("Бот запущен")
    await dp.start_polling(bot)
 
 
if __name__ == "__main__":
    asyncio.run(main())
