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
# Список заданий общий для всех (не привязан к конкретному чату) — его видно
# и в личке с ботом, и в любой группе, куда бот добавлен. Добавлять новые
# задания можно только из личного чата с ботом (см. ниже).
# Файлы вынесены в отдельную таблицу — к одному заданию можно прикрепить
# сколько угодно файлов.
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
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS homework_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hw_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            file_type TEXT NOT NULL,
            FOREIGN KEY (hw_id) REFERENCES homework (id)
        )
    """)
    # Миграция со старых версий базы
    cur.execute("PRAGMA table_info(homework)")
    cols = [row[1] for row in cur.fetchall()]
    if "chat_id" not in cols and "user_id" in cols:
        cur.execute("ALTER TABLE homework RENAME COLUMN user_id TO chat_id")
    for col, coltype in (("added_by", "TEXT"), ("done_by", "TEXT")):
        try:
            cur.execute(f"ALTER TABLE homework ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass
    # Если в старой версии файл хранился прямо в homework (file_id/file_type) —
    # перенесём его в новую таблицу homework_files.
    cur.execute("PRAGMA table_info(homework)")
    cols = [row[1] for row in cur.fetchall()]
    if "file_id" in cols:
        cur.execute("SELECT id, file_id, file_type FROM homework WHERE file_id IS NOT NULL")
        for hw_id, file_id, file_type in cur.fetchall():
            cur.execute(
                "INSERT INTO homework_files (hw_id, file_id, file_type) VALUES (?, ?, ?)",
                (hw_id, file_id, file_type or "document"),
            )
    conn.commit()
    conn.close()
 
 
def add_homework(chat_id: int, added_by: str, subject: str, description: str, deadline: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO homework (chat_id, added_by, subject, description, deadline, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, added_by, subject, description, deadline, datetime.now().isoformat()),
    )
    hw_id = cur.lastrowid
    conn.commit()
    conn.close()
    return hw_id
 
 
def add_file(hw_id: int, file_id: str, file_type: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO homework_files (hw_id, file_id, file_type) VALUES (?, ?, ?)",
        (hw_id, file_id, file_type),
    )
    conn.commit()
    conn.close()
 
 
def get_files(hw_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT file_id, file_type FROM homework_files WHERE hw_id = ?", (hw_id,))
    rows = cur.fetchall()
    conn.close()
    return rows
 
 
def get_homework(only_pending=True, days_ahead: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = ("SELECT id, subject, description, deadline, done, added_by, done_by "
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
        "SELECT id, subject, description, deadline, done, added_by, done_by FROM homework WHERE id = ?",
        (hw_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row
 
 
def mark_done(hw_id: int, done_by: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE homework SET done = 1, done_by = ? WHERE id = ?", (done_by, hw_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed
 
 
def delete_homework(hw_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM homework WHERE id = ?", (hw_id,))
    cur.execute("DELETE FROM homework_files WHERE hw_id = ?", (hw_id,))
    changed = cur.rowcount >= 0
    conn.commit()
    conn.close()
    return changed
 
 
def update_field(hw_id: int, field: str, value: str) -> bool:
    assert field in ("subject", "description", "deadline")  # защита от произвольных имён колонок
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(f"UPDATE homework SET {field} = ? WHERE id = ?", (value, hw_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed
 
 
def get_due_tomorrow_unreminded():
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
 
 
def hw_actions_kb(hw_id: int, files_count: int) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{hw_id}"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"edit:{hw_id}"),
        ],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{hw_id}")],
    ]
    if files_count:
        word = "файл" if files_count == 1 else ("файла" if 2 <= files_count <= 4 else "файлов")
        buttons.insert(1, [InlineKeyboardButton(
            text=f"📎 Показать {files_count} {word}", callback_data=f"files:{hw_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
 
 
def files_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Готово, больше файлов нет", callback_data="finish_files")]]
    )
 
 
def edit_choice_kb(hw_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Предмет", callback_data=f"editfield:{hw_id}:subject")],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"editfield:{hw_id}:description")],
        [InlineKeyboardButton(text="📅 Дедлайн", callback_data=f"editfield:{hw_id}:deadline")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="edit_cancel")],
    ])
 
 
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
 
 
# ============ FSM: ДОБАВЛЕНИЕ ДЗ ============
class AddHomework(StatesGroup):
    subject = State()
    description = State()
    deadline = State()
    attachment = State()  # можно прислать несколько файлов подряд
 
 
# ============ FSM: РЕДАКТИРОВАНИЕ ДЗ ============
class EditHomework(StatesGroup):
    waiting_value = State()
 
 
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
 
 
def format_hw_line(hw_id, subject, description, deadline, done, added_by=None, done_by=None) -> str:
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
    line = (
        f"{mark} #{hw_id} [{subject}] {description}\n"
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
            "✏️ А добавлять новые задания можно только в личном чате со мной."
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
        "К заданию можно прикрепить сразу несколько файлов, а потом отредактировать "
        "предмет, описание или дедлайн в любой момент.\n\n"
        "📢 Добавь меня в общий чат класса — там все смогут смотреть список "
        "командой /list, а добавлять новые задания сможешь только ты, в этой личке.",
        reply_markup=main_menu_kb(),
    )
 
 
# ============ ДОБАВЛЕНИЕ ДЗ (только в личном чате с ботом) ============
@router.message(Command("add"), F.chat.type.in_({"group", "supergroup"}))
@router.message(F.text == "➕ Добавить", F.chat.type.in_({"group", "supergroup"}))
async def cmd_add_blocked_in_group(message: Message):
    me = await bot.me()
    await message.answer(
        "✋ Добавлять задания можно только в личном чате с ботом.\n"
        f"Напишите мне в личку: @{me.username}, и там используйте /add.\n"
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
    data = await state.get_data()
    # Создаём задание сразу, файлы будем прикреплять к уже существующей записи
    hw_id = add_homework(message.chat.id, display_name(message.from_user), data["subject"], data["description"], parsed)
    await state.update_data(hw_id=hw_id, deadline=parsed)
    await state.set_state(AddHomework.attachment)
    await message.answer(
        "📎 Можно прикрепить файлы к заданию — присылайте фото или документы одно за другим, "
        "сколько нужно.\nКогда закончите — нажмите «Готово».",
        reply_markup=files_done_kb(),
    )
 
 
@router.message(AddHomework.attachment, F.photo)
async def process_attachment_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    add_file(data["hw_id"], message.photo[-1].file_id, "photo")
    await message.answer("✅ Фото добавлено. Присылайте ещё, или нажмите «Готово».", reply_markup=files_done_kb())
 
 
@router.message(AddHomework.attachment, F.document)
async def process_attachment_document(message: Message, state: FSMContext):
    data = await state.get_data()
    add_file(data["hw_id"], message.document.file_id, "document")
    await message.answer("✅ Документ добавлен. Присылайте ещё, или нажмите «Готово».", reply_markup=files_done_kb())
 
 
@router.callback_query(AddHomework.attachment, F.data == "finish_files")
async def process_finish_files(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    files_count = len(get_files(data["hw_id"]))
    await state.clear()
    d = datetime.strptime(data["deadline"], "%Y-%m-%d").date()
    text = (
        f"✅ Добавлено!\n📚 {data['subject']}\n📝 {data['description']}\n"
        f"📅 {d.strftime('%d.%m.%Y')}"
    )
    if files_count:
        text += f"\n📎 Прикреплено файлов: {files_count}"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(text, reply_markup=main_menu_kb())
    await callback.answer()
 
 
@router.message(AddHomework.attachment)
async def process_attachment_invalid(message: Message):
    await message.answer(
        "Пришлите фото или документ, либо нажмите «✅ Готово, больше файлов нет» кнопкой выше."
    )
 
 
# ============ ПРОСМОТР СПИСКОВ ============
async def send_hw_list(message: Message, rows, empty_text: str, header: str):
    if not rows:
        await message.answer(empty_text)
        return
    await message.answer(header)
    for hw_id, subject, description, deadline, done, added_by, done_by in rows:
        text = format_hw_line(hw_id, subject, description, deadline, done, added_by, done_by)
        files_count = len(get_files(hw_id))
        await message.answer(text, reply_markup=hw_actions_kb(hw_id, files_count))
 
 
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
 
 
# ============ ТРИГЕРНАЯ ФРАЗА ============
@router.message(F.text.func(
    lambda t: t is not None and "инокент" in t.lower() and "дз" in t.lower()
))
async def trigger_phrase(message: Message):
    rows = get_homework()
    await send_hw_list(message, rows, "🎉 Дз нет, можно выдыхать!", "📋 Вот что по дз:")
 
 
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
 
 
# ============ ГОТОВО / УДАЛИТЬ / ФАЙЛЫ — через кнопки ============
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
 
 
@router.callback_query(F.data.startswith("files:"))
async def cb_files(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    files = get_files(hw_id)
    if not files:
        await callback.answer("Файлов нет", show_alert=True)
        return
    for file_id, file_type in files:
        if file_type == "photo":
            await callback.message.answer_photo(file_id, caption=f"📎 К заданию #{hw_id}")
        else:
            await callback.message.answer_document(file_id, caption=f"📎 К заданию #{hw_id}")
    await callback.answer()
 
 
# ============ РЕДАКТИРОВАНИЕ ЗАДАНИЯ ============
@router.callback_query(F.data.startswith("edit:"))
async def cb_edit_start(callback: CallbackQuery):
    hw_id = int(callback.data.split(":")[1])
    if not get_one(hw_id):
        await callback.answer("Задание не найдено", show_alert=True)
        return
    await callback.message.answer("Что хотите изменить?", reply_markup=edit_choice_kb(hw_id))
    await callback.answer()
 
 
@router.callback_query(F.data == "edit_cancel")
async def cb_edit_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()
 
 
@router.callback_query(F.data.startswith("editfield:"))
async def cb_edit_field(callback: CallbackQuery, state: FSMContext):
    _, hw_id, field = callback.data.split(":")
    hw_id = int(hw_id)
    prompts = {
        "subject": "📚 Введите новый предмет:",
        "description": "📝 Введите новое описание задания:",
        "deadline": "📅 Введите новый дедлайн (ДД.ММ.ГГГГ):",
    }
    await state.set_state(EditHomework.waiting_value)
    await state.update_data(hw_id=hw_id, field=field)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(prompts[field])
    await callback.answer()
 
 
@router.message(EditHomework.waiting_value)
async def process_edit_value(message: Message, state: FSMContext):
    data = await state.get_data()
    hw_id, field = data["hw_id"], data["field"]
    value = message.text.strip()
 
    if field == "deadline":
        parsed = parse_date(value)
        if not parsed:
            await message.answer("❌ Не понял дату. Введите в формате ДД.ММ.ГГГГ или ДД.ММ.")
            return
        value = parsed
 
    if update_field(hw_id, field, value):
        field_names = {"subject": "Предмет", "description": "Описание", "deadline": "Дедлайн"}
        shown_value = value
        if field == "deadline":
            shown_value = datetime.strptime(value, "%Y-%m-%d").date().strftime("%d.%m.%Y")
        await message.answer(f"✅ {field_names[field]} обновлён: {shown_value}")
    else:
        await message.answer("❌ Не удалось найти задание для изменения.")
    await state.clear()
 
 
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
