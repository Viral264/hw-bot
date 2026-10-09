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
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============ НАСТРОЙКИ ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ВАШ_ТОКЕН")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "homework.db"))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ============ БАЗА ДАННЫХ ============
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            description TEXT NOT NULL,
            deadline TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            reminded INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def add_homework(user_id: int, subject: str, description: str, deadline: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO homework (user_id, subject, description, deadline, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, subject, description, deadline, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_homework(user_id: int, only_pending=True, days_ahead: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = "SELECT id, subject, description, deadline, done FROM homework WHERE user_id = ?"
    params = [user_id]
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


def mark_done(user_id: int, hw_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE homework SET done = 1 WHERE id = ? AND user_id = ?", (hw_id, user_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def delete_homework(user_id: int, hw_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM homework WHERE id = ? AND user_id = ?", (hw_id, user_id))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def get_due_tomorrow_unreminded():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    cur.execute(
        "SELECT id, user_id, subject, description FROM homework "
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


# ============ FSM ДЛЯ ДОБАВЛЕНИЯ ДЗ ============
class AddHomework(StatesGroup):
    subject = State()
    description = State()
    deadline = State()


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


def format_hw_line(hw_id, subject, description, deadline, done=0) -> str:
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
    return f"{mark} #{hw_id} [{subject}] {description}\n   📅 {d.strftime('%d.%m.%Y')} ({status})"


# ============ БАЗОВЫЕ КОМАНДЫ ============
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот для отслеживания домашних заданий.\n\n"
        "Доступные команды:\n"
        "/add — добавить новое дз\n"
        "/list — список невыполненных дз\n"
        "/today — что сдавать сегодня\n"
        "/week — что сдавать на неделе\n"
        "/done <id> — отметить как выполненное\n"
        "/delete <id> — удалить задание\n"
    )


# ============ ДОБАВЛЕНИЕ ДЗ ============
@router.message(Command("add"))
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
    add_homework(message.from_user.id, data["subject"], data["description"], parsed)
    await state.clear()
    d = datetime.strptime(parsed, "%Y-%m-%d").date()
    await message.answer(
        f"✅ Добавлено!\n📚 {data['subject']}\n📝 {data['description']}\n"
        f"📅 {d.strftime('%d.%m.%Y')}"
    )


# ============ ПРОСМОТР СПИСКОВ ============
@router.message(Command("list"))
async def cmd_list(message: Message):
    rows = get_homework(message.from_user.id)
    if not rows:
        await message.answer("🎉 Нет невыполненных заданий!")
        return
    text = "📋 Твои домашние задания:\n\n" + "\n\n".join(
        format_hw_line(*row) for row in rows
    )
    await message.answer(text)


@router.message(Command("today"))
async def cmd_today(message: Message):
    rows = get_homework(message.from_user.id, days_ahead=0)
    if not rows:
        await message.answer("Сегодня сдавать ничего не нужно 👍")
        return
    text = "🔥 На сегодня:\n\n" + "\n\n".join(format_hw_line(*row) for row in rows)
    await message.answer(text)


@router.message(Command("week"))
async def cmd_week(message: Message):
    rows = get_homework(message.from_user.id, days_ahead=7)
    if not rows:
        await message.answer("На этой неделе всё сдано или заданий нет 👍")
        return
    text = "📆 На неделю:\n\n" + "\n\n".join(format_hw_line(*row) for row in rows)
    await message.answer(text)


# ============ ГОТОВО / УДАЛИТЬ ============
@router.message(Command("done"))
async def cmd_done(message: Message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /done <id>\nНапример: /done 3")
        return
    hw_id = int(parts[1])
    if mark_done(message.from_user.id, hw_id):
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
    if delete_homework(message.from_user.id, hw_id):
        await message.answer(f"🗑️ Задание #{hw_id} удалено.")
    else:
        await message.answer("❌ Задание с таким id не найдено.")


# ============ НАПОМИНАНИЯ ============
async def send_reminders():
    rows = get_due_tomorrow_unreminded()
    for hw_id, user_id, subject, description in rows:
        try:
            await bot.send_message(
                user_id,
                f"⏰ Напоминание! Завтра дедлайн:\n📚 {subject}\n📝 {description}",
            )
            mark_reminded(hw_id)
        except Exception as e:
            logging.warning(f"Не удалось отправить напоминание {hw_id}: {e}")


async def main():
    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_reminders, "cron", hour=20, minute=0)
    scheduler.start()

    logging.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
