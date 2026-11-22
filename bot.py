import asyncio
import logging
import os
import secrets
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from aiogram.utils.deep_linking import create_start_link

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "santa.db")
DEFAULT_BUDGET = int(os.getenv("DEFAULT_BUDGET", "1500"))
TZ = ZoneInfo("Europe/Moscow")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dp = Dispatcher()


class CreateGame(StatesGroup):
    title = State()
    deadline = State()
    budget = State()


class JoinGame(StatesGroup):
    name = State()
    wish = State()
    wishlist = State()


def db():
    return aiosqlite.connect(DB_PATH)


async def init_db():
    async with db() as conn:
        await conn.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            organizer_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            deadline TEXT NOT NULL,
            budget INTEGER NOT NULL,
            is_drawn INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            wish TEXT DEFAULT '',
            wishlist_url TEXT DEFAULT '',
            joined_at TEXT NOT NULL,
            UNIQUE(game_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS assignments (
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            giver_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            PRIMARY KEY(game_id, giver_id),
            UNIQUE(game_id, receiver_id)
        );

        CREATE TABLE IF NOT EXISTS sent_reminders (
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            days_before INTEGER NOT NULL,
            PRIMARY KEY(game_id, days_before)
        );
        """)
        await conn.commit()


async def get_game_by_token(token: str):
    async with db() as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM games WHERE token = ?", (token,))
        return await cur.fetchone()


async def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("join_"):
        token = parts[1][5:]
        game = await get_game_by_token(token)
        if not game:
            await message.answer("Не нашла такую игру. Проверь ссылку-приглашение.")
            return
        if game["is_drawn"]:
            await message.answer("Жеребьёвка в этой игре уже проведена, регистрация закрыта.")
            return
        async with db() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM participants WHERE game_id = ? AND user_id = ?",
                (game["id"], message.from_user.id),
            )
            exists = await cur.fetchone()
        if exists:
            await message.answer(f"Ты уже участвуешь в игре «{game['title']}» 🎅")
            return
        await state.update_data(game_id=game["id"], game_title=game["title"])
        await state.set_state(JoinGame.name)
        await message.answer(
            f"🎅 Добро пожаловать в игру «{game['title']}»!\n\n"
            f"Бюджет подарка: {game['budget']} ₽\n"
            "Как тебя подписать в игре? Напиши имя и, если нужно, фамилию."
        )
        return

    await message.answer(
        "Привет! Я бот «Тайный Санта» 🎅\n\n"
        "Чтобы участвовать в игре, перейди по ссылке-приглашению от организатора.\n"
        "Организатор может создать игру командой /newgame.\n"
        "Список своих игр: /games"
    )


@dp.message(Command("newgame"))
async def new_game(message: Message, state: FSMContext):
    if not await is_admin(message.from_user.id):
        await message.answer("Создавать игры может только организатор.")
        return
    await state.clear()
    await state.set_state(CreateGame.title)
    await message.answer("Как назовём игру? Например: «Тайный Санта команды».")


@dp.message(CreateGame.title)
async def game_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if len(title) < 3 or len(title) > 80:
        await message.answer("Название должно быть от 3 до 80 символов. Попробуй ещё раз.")
        return
    await state.update_data(title=title)
    await state.set_state(CreateGame.deadline)
    await message.answer(
        "До какой даты нужно подготовить подарки?\n"
        "Напиши дату в формате ДД.ММ.ГГГГ, например 25.12.2026."
    )


@dp.message(CreateGame.deadline)
async def game_deadline(message: Message, state: FSMContext):
    try:
        deadline = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
        if deadline <= datetime.now(TZ).date():
            raise ValueError
    except ValueError:
        await message.answer("Не получилось распознать дату или она уже прошла. Формат: ДД.ММ.ГГГГ.")
        return
    await state.update_data(deadline=deadline.isoformat())
    await state.set_state(CreateGame.budget)
    await message.answer(
        f"Укажи бюджет подарка в рублях.\n"
        f"По умолчанию — {DEFAULT_BUDGET} ₽. Напиши число или «по умолчанию»."
    )


@dp.message(CreateGame.budget)
async def game_budget(message: Message, state: FSMContext):
    raw = (message.text or "").strip().lower()
    if raw in {"по умолчанию", "по умолчанию.", "default"}:
        budget = DEFAULT_BUDGET
    else:
        try:
            budget = int(raw.replace(" ", "").replace("₽", ""))
            if not 100 <= budget <= 1_000_000:
                raise ValueError
        except ValueError:
            await message.answer("Введи сумму от 100 до 1 000 000 ₽ или напиши «по умолчанию».")
            return

    data = await state.get_data()
    token = secrets.token_urlsafe(9)
    async with db() as conn:
        cur = await conn.execute(
            """INSERT INTO games(token, organizer_id, title, deadline, budget, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (token, message.from_user.id, data["title"], data["deadline"], budget,
             datetime.now(TZ).isoformat()),
        )
        game_id = cur.lastrowid
        await conn.commit()

    await state.clear()
    invite = await create_start_link(message.bot, f"join_{token}", encode=False)
    deadline_text = date.fromisoformat(data["deadline"]).strftime("%d.%m.%Y")
    await message.answer(
        f"🎄 Игра «{data['title']}» создана!\n\n"
        f"Бюджет: {budget} ₽\n"
        f"Дедлайн: {deadline_text}\n\n"
        f"Ссылка для участников:\n{invite}\n\n"
        f"Когда все зарегистрируются, проведи жеребьёвку командой:\n"
        f"/draw {token}\n\n"
        "Важно: участники должны сначала нажать Start у бота, иначе Telegram не позволит "
        "отправить им личное сообщение с результатом."
    )


@dp.message(JoinGame.name)
async def participant_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if len(name) < 2 or len(name) > 60:
        await message.answer("Имя должно быть от 2 до 60 символов. Попробуй ещё раз.")
        return
    await state.update_data(name=name)
    await state.set_state(JoinGame.wish)
    await message.answer(
        "Что тебе хотелось бы получить? 🎁\n"
        "Напиши несколько идей или отправь «пока не знаю»."
    )


@dp.message(JoinGame.wish)
async def participant_wish(message: Message, state: FSMContext):
    wish = (message.text or "").strip()
    if not wish or len(wish) > 500:
        await message.answer("Напиши пожелания (до 500 символов) или отправь «пока не знаю».")
        return
    await state.update_data(wish=wish)
    await state.set_state(JoinGame.wishlist)
    await message.answer(
        "Пришли ссылку на вишлист, если он есть.\n"
        "Если ссылки нет, напиши «нет»."
    )


@dp.message(JoinGame.wishlist)
async def participant_wishlist(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    if url.lower() in {"нет", "нету", "не знаю", "-"}:
        url = ""
    elif not (url.startswith("https://") or url.startswith("http://")):
        await message.answer("Нужна ссылка, начинающаяся с https:// или http://, либо напиши «нет».")
        return
    if len(url) > 300:
        await message.answer("Ссылка слишком длинная. Пришли более короткую или напиши «нет».")
        return

    data = await state.get_data()
    try:
        async with db() as conn:
            await conn.execute(
                """INSERT INTO participants(game_id, user_id, name, wish, wishlist_url, joined_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (data["game_id"], message.from_user.id, data["name"], data["wish"], url,
                 datetime.now(TZ).isoformat()),
            )
            await conn.commit()
    except aiosqlite.IntegrityError:
        await message.answer("Ты уже зарегистрирован(а) в этой игре.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"Ты зарегистрирован(а) в игре «{data['game_title']}»! 🎄\n\n"
        "Когда организатор проведёт жеребьёвку, я пришлю тебе имя твоего получателя."
    )


@dp.message(Command("games"))
async def list_games(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("Эта команда доступна организатору.")
        return
    async with db() as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT g.*, COUNT(p.id) AS participant_count
               FROM games g LEFT JOIN participants p ON p.game_id = g.id
               WHERE g.organizer_id = ?
               GROUP BY g.id ORDER BY g.id DESC""",
            (message.from_user.id,),
        )
        games = await cur.fetchall()
    if not games:
        await message.answer("У тебя пока нет игр. Создай первую командой /newgame.")
        return
    lines = ["🎅 Твои игры:"]
    for game in games:
        status = "жеребьёвка проведена" if game["is_drawn"] else "регистрация открыта"
        lines.append(
            f"\n• {game['title']}\n"
            f"  Участников: {game['participant_count']}\n"
            f"  Дедлайн: {date.fromisoformat(game['deadline']).strftime('%d.%m.%Y')}\n"
            f"  Статус: {status}\n"
            f"  Токен: {game['token']}"
        )
    await message.answer("\n".join(lines))


@dp.message(Command("draw"))
async def draw(message: Message):
    if not await is_admin(message.from_user.id):
        await message.answer("Провести жеребьёвку может только организатор.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Укажи токен игры: /draw TOKEN\nТокен можно посмотреть в /games.")
        return
    game = await get_game_by_token(parts[1].strip())
    if not game or game["organizer_id"] != message.from_user.id:
        await message.answer("Игра не найдена или она создана другим организатором.")
        return
    if game["is_drawn"]:
        await message.answer("Жеребьёвка для этой игры уже проводилась.")
        return

    async with db() as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT user_id, name, wish, wishlist_url FROM participants WHERE game_id = ?",
            (game["id"],),
        )
        people = await cur.fetchall()

    if len(people) < 2:
        await message.answer("Для жеребьёвки нужно минимум 2 участника.")
        return

    # Перемешиваем получателей до тех пор, пока никто не выпадет сам себе.
    giver_ids = [p["user_id"] for p in people]
    receivers = giver_ids[:]
    for _ in range(10000):
        import random
        random.shuffle(receivers)
        if all(giver != receiver for giver, receiver in zip(giver_ids, receivers)):
            break
    else:
        await message.answer("Не удалось построить распределение. Попробуй ещё раз.")
        return

    by_id = {p["user_id"]: p for p in people}
    assignments = list(zip(giver_ids, receivers))

    async with db() as conn:
        await conn.executemany(
            "INSERT INTO assignments(game_id, giver_id, receiver_id) VALUES (?, ?, ?)",
            [(game["id"], giver, receiver) for giver, receiver in assignments],
        )
        await conn.execute("UPDATE games SET is_drawn = 1 WHERE id = ?", (game["id"],))
        await conn.commit()

    sent = 0
    failed = 0
    for giver_id, receiver_id in assignments:
        receiver = by_id[receiver_id]
        text = (
            f"🎅 Жеребьёвка состоялась!\n\n"
            f"Ты — Тайный Санта для: {receiver['name']}\n"
            f"Бюджет подарка: {game['budget']} ₽\n"
            f"Дедлайн: {date.fromisoformat(game['deadline']).strftime('%d.%m.%Y')}\n\n"
            f"Пожелания: {receiver['wish'] or 'не указаны'}\n"
        )
        if receiver["wishlist_url"]:
            text += f"\nВишлист: {receiver['wishlist_url']}"
        text += "\n\nСохраняй секрет и не раскрывайся раньше времени! 🤫"
        try:
            await message.bot.send_message(giver_id, text)
            sent += 1
        except Exception:
            logger.exception("Could not send assignment to user %s", giver_id)
            failed += 1

    await message.answer(
        f"🎄 Жеребьёвка игры «{game['title']}» завершена.\n"
        f"Участников: {len(people)}\n"
        f"Сообщений отправлено: {sent}\n"
        f"Не удалось отправить: {failed}\n\n"
        "Распределение никому не показываю — даже организатору."
    )


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Текущее действие отменено.")


async def reminder_loop(bot: Bot):
    """Проверяет дедлайны и отправляет каждому участнику напоминания за 3 и 1 день."""
    while True:
        try:
            today = datetime.now(TZ).date()
            async with db() as conn:
                conn.row_factory = aiosqlite.Row
                cur = await conn.execute(
                    """SELECT * FROM games
                       WHERE is_drawn = 1 AND deadline >= ?""",
                    (today.isoformat(),),
                )
                games = await cur.fetchall()

            for game in games:
                deadline = date.fromisoformat(game["deadline"])
                days_left = (deadline - today).days
                for threshold in (3, 1):
                    if days_left == threshold:
                        async with db() as conn:
                            cur = await conn.execute(
                                "SELECT 1 FROM sent_reminders WHERE game_id = ? AND days_before = ?",
                                (game["id"], threshold),
                            )
                            already_sent = await cur.fetchone()
                        if already_sent:
                            continue

                        async with db() as conn:
                            cur = await conn.execute(
                                "SELECT user_id FROM participants WHERE game_id = ?",
                                (game["id"],),
                            )
                            users = await cur.fetchall()

                        for user in users:
                            try:
                                await bot.send_message(
                                    user["user_id"],
                                    f"⏰ Напоминание от Тайного Санты!\n"
                                    f"До дедлайна игры «{game['title']}» осталось {days_left} дн.\n"
                                    f"Не забудь подготовить подарок 🎁"
                                )
                            except Exception:
                                logger.exception("Reminder failed for user %s", user["user_id"])

                        async with db() as conn:
                            await conn.execute(
                                "INSERT OR IGNORE INTO sent_reminders(game_id, days_before) VALUES (?, ?)",
                                (game["id"], threshold),
                            )
                            await conn.commit()
        except Exception:
            logger.exception("Reminder loop error")
        await asyncio.sleep(6 * 60 * 60)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")
    if not ADMIN_IDS:
        raise RuntimeError("Не заданы ADMIN_IDS. Укажи Telegram user ID организатора.")
    await init_db()
    bot = Bot(BOT_TOKEN)
    reminder_task = asyncio.create_task(reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
