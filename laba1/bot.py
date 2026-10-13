"""
XRU4EVALNYA — Telegram-бот для оценки хрючевальности еды.

Лабораторная 1:
  Пользователи присылают фото еды, другие оценивают его по шкале 1–5 🐷.
  Бот считает рейтинги, ведёт статистику, рассылает «Хрючево дня»
  и еженедельный отчёт админам.

Лабораторная 2 — работа с данными из БД:
  • /find    — поиск блюд по названию;
  • /history — история своих оценок и блюд за период;
  • /export  — выгрузка своей истории в CSV.

Стек: python-telegram-bot (v21, async), SQLite (стандартный модуль sqlite3).
"""

import csv
import html
import io
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, time, timezone
from warnings import filterwarnings
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

# =====================================================================
# НАСТРОЙКИ
# =====================================================================

# Загружаем переменные окружения из файла .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "hryuchevo.db")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))

# Минимальное число голосов, после которого блюдо получает вердикт
# и попадает в топы
MIN_VOTES = 3

# Ограничения для новых команд
FIND_LIMIT = 10          # максимум результатов поиска
HISTORY_DEFAULT_DAYS = 7  # период /history по умолчанию
HISTORY_MAX_DAYS = 365    # максимальный период /history
HISTORY_SHOW = 15         # сколько строк каждого раздела показывать в чате


def parse_admin_ids(raw: str) -> set[int]:
    """Разбирает строку вида '123,456' в множество ID админов.
    Некорректные значения пропускаются с предупреждением."""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logging.warning("Некорректный ADMIN_ID пропущен: %r", part)
    return ids


ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))

# Логирование: пишем в консоль время, уровень и сообщение
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# httpx слишком болтлив на уровне INFO, приглушаем
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("hryuchevalnya")

# ConversationHandler предупреждает о CallbackQueryHandler внутри диалога.
# Для нашего сценария это нормально, поэтому скрываем предупреждение.
filterwarnings(action="ignore", message=r".*CallbackQueryHandler", category=PTBUserWarning)

# =====================================================================
# СПРАВОЧНИКИ И ТЕКСТЫ
# =====================================================================

# Места, откуда может быть еда: ключ хранится в БД, значение показывается юзеру
PLACES = {
    "canteen": "🏫 Столовая",
    "dorm": "🏠 Общага",
    "delivery": "🛵 Доставка",
    "shop": "🏪 Магазин",
    "homemade": "👩‍🍳 Сам приготовил",
}

# Вердикт по округлённой средней оценке
VERDICTS = {
    1: "Default",
    2: "🙂 Съедобно",
    3: "🎓 Классическое",
    4: "🐽 Хрючево",
    5: "🏆 МЕГА хрючево",
}

# Звания по активности (посты + оценки), от старшего к младшему
RANKS = [
    (50, "Повелитель"),
    (20, "Кабан."),
    (5, "Boss"),
    (0, "Crook"),
]

HELP_TEXT = (
    "🐷 <b>XRU4EVALNYA</b> — оцениваем.\n\n"
    "Шкала: 🐷 — ничего необычного, 🐷🐷🐷🐷🐷 — МЕГА хрючево.\n\n"
    "<b>Команды:</b>\n"
    "/new — добавить фото (или просто пришли фото)\n"
    "/rate — оценить чужое творение\n"
    "/top — топ-10 самого крутого \n"
    "/antitop — топ-10 не самого крутого!! \n"
    "/places —  рейтинг по местам\n"
    "/find &lt;слово&gt; — найти блюдо по названию\n"
    "/my — мои посты\n"
    "/stats — моя статистика и звание\n"
    "/history [дни] — моя история за период (по умолчанию 7 дней)\n"
    "/export — выгрузить мою историю в CSV\n"
    "/report — отчёт (только для админов)\n"
    "/cancel — отменить добавление\n"
    "/help — эта справка"
)

# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ФОРМАТИРОВАНИЯ
# =====================================================================


def round_score(avg: float) -> int:
    """Округляет среднюю оценку до целого 1..5 (обычное округление, не банковское)."""
    return max(1, min(5, int(avg + 0.5)))


def pigs(avg: float) -> str:
    """Превращает оценку в строку из свинок: 3.6 -> 🐷🐷🐷🐷."""
    return "🐷" * round_score(avg)


def verdict(avg: float) -> str:
    """Возвращает текстовый вердикт по средней оценке."""
    return VERDICTS[round_score(avg)]


def get_rank(actions: int) -> tuple[str, int | None]:
    """Возвращает (звание, сколько действий осталось до следующего звания).
    Для максимального звания второй элемент — None."""
    for i, (threshold, title) in enumerate(RANKS):
        if actions >= threshold:
            next_left = RANKS[i - 1][0] - actions if i > 0 else None
            return title, next_left
    return RANKS[-1][1], RANKS[-2][0] - actions


def display_name(row) -> str:
    """Имя автора для вывода: @username, иначе имя, иначе 'аноним'."""
    if row["username"]:
        return "@" + html.escape(row["username"])
    if row["first_name"]:
        return html.escape(row["first_name"])
    return "аноним"


def fmt_dt(db_value: str | None) -> str:
    """Переводит время из БД (UTC, 'YYYY-MM-DD HH:MM:SS') в местное 'ДД.ММ.ГГГГ ЧЧ:ММ'.
    SQLite пишет CURRENT_TIMESTAMP в UTC, а пользователю нужно его время."""
    try:
        dt = datetime.strptime(db_value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return db_value or "—"


def post_caption(post) -> str:
    """Подпись к фото блюда: название, место, цена."""
    price = f"{post['price']} ₽" if post["price"] is not None else "не указана"
    return (
        f"🍽 <b>{html.escape(post['title'])}</b>\n"
        f"{PLACES.get(post['place'], post['place'])}\n"
        f"💰 Цена: {price}"
    )


def rating_line(avg: float | None, votes: int) -> str:
    """Строка с текущим рейтингом блюда и вердиктом (если голосов достаточно)."""
    if not votes:
        return "Пока никто не оценил."
    if votes < MIN_VOTES:
        return f"Голосов: {votes}. Вердикт появится после {MIN_VOTES}."
    return (
        f"Средняя: {pigs(avg)} {avg:.1f} ({votes} гол.)\n"
        f"Вердикт: {verdict(avg)}"
    )


def short_rating(avg: float | None, votes: int) -> str:
    """Короткая запись рейтинга для списков: '🐷🐷🐷 3.2 (4 гол.)' или 'не оценено'."""
    if not votes:
        return "не оценено"
    return f"{pigs(avg)} {avg:.1f} ({votes} гол.)"


# =====================================================================
# БАЗА ДАННЫХ (SQLite)
# =====================================================================
# Используем синхронный sqlite3: для учебного бота с небольшой нагрузкой
# запросы выполняются за миллисекунды и не блокируют бота заметно.


def py_lower(value):
    """Python-версия lower() для SQLite.
    Встроенная LOWER() в SQLite понижает регистр только у латиницы,
    поэтому «Макароны» и «макароны» для неё разные строки.
    Регистрируем свою функцию, которая умеет в кириллицу."""
    return value.lower() if isinstance(value, str) else value


def get_conn() -> sqlite3.Connection:
    """Открывает соединение с БД. Строки возвращаются как словари (sqlite3.Row)."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("PY_LOWER", 1, py_lower, deterministic=True)
    return conn


def init_db() -> None:
    """Создаёт таблицы, если их ещё нет."""
    with closing(get_conn()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY,          -- Telegram user_id
                username    TEXT,
                first_name  TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                file_id     TEXT NOT NULL,                -- file_id фото в Telegram
                title       TEXT NOT NULL,
                place       TEXT NOT NULL,                -- ключ из PLACES
                price       INTEGER,                      -- может быть NULL
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS ratings (
                post_id     INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                score       INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (post_id, user_id)            -- один голос на пост
            );
            """
        )
    logger.info("База данных готова: %s", DB_PATH)


def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Добавляет пользователя или обновляет его username/имя."""
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """
            INSERT INTO users (id, username, first_name) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name),
        )


def add_post(user_id: int, file_id: str, title: str, place: str, price: int | None) -> int:
    """Сохраняет новое блюдо и возвращает его ID."""
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO posts (user_id, file_id, title, place, price) VALUES (?, ?, ?, ?, ?)",
            (user_id, file_id, title, place, price),
        )
        return cur.lastrowid


def get_post(post_id: int):
    """Возвращает блюдо по ID или None."""
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def has_rated(post_id: int, user_id: int) -> bool:
    """Оценивал ли пользователь это блюдо."""
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM ratings WHERE post_id = ? AND user_id = ?", (post_id, user_id)
        ).fetchone()
        return row is not None


def get_random_unrated_post(user_id: int):
    """Случайное чужое блюдо, которое пользователь ещё не оценивал."""
    with closing(get_conn()) as conn:
        return conn.execute(
            """
            SELECT * FROM posts
            WHERE user_id != ?
              AND id NOT IN (SELECT post_id FROM ratings WHERE user_id = ?)
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (user_id, user_id),
        ).fetchone()


def add_rating(post_id: int, user_id: int, score: int) -> bool:
    """Сохраняет оценку. Возвращает False, если пользователь уже голосовал."""
    try:
        with closing(get_conn()) as conn, conn:
            conn.execute(
                "INSERT INTO ratings (post_id, user_id, score) VALUES (?, ?, ?)",
                (post_id, user_id, score),
            )
        return True
    except sqlite3.IntegrityError:
        # Сработал PRIMARY KEY (post_id, user_id) — повторный голос
        return False


def get_post_rating(post_id: int) -> tuple[float | None, int]:
    """Средняя оценка и число голосов для блюда."""
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT AVG(score) AS avg_score, COUNT(*) AS votes FROM ratings WHERE post_id = ?",
            (post_id,),
        ).fetchone()
        return row["avg_score"], row["votes"]


def get_ranked_posts(order: str = "DESC", limit: int = 10,
                     min_votes: int = MIN_VOTES, days: int | None = None):
    """Рейтинг блюд по средней оценке.

    order: 'DESC' — самое хрючевное сверху, 'ASC' — самое приличное сверху.
    days:  если задано, берутся только блюда за последние N дней.
    """
    # order подставляется в SQL напрямую, поэтому проверяем его строго
    if order not in ("ASC", "DESC"):
        raise ValueError("order должен быть 'ASC' или 'DESC'")

    where, params = "", []
    if days is not None:
        where = "WHERE p.created_at >= datetime('now', ?)"
        params.append(f"-{days} days")
    params += [min_votes, limit]

    with closing(get_conn()) as conn:
        return conn.execute(
            f"""
            SELECT p.id, p.user_id, p.file_id, p.title, p.place, p.price,
                   u.username, u.first_name,
                   AVG(r.score) AS avg_score, COUNT(r.score) AS votes
            FROM posts p
            JOIN ratings r ON r.post_id = p.id
            JOIN users u   ON u.id = p.user_id
            {where}
            GROUP BY p.id
            HAVING votes >= ?
            ORDER BY avg_score {order}, votes DESC
            LIMIT ?
            """,
            params,
        ).fetchall()


def get_place_stats():
    """Средняя хрючевальность по каждому месту."""
    with closing(get_conn()) as conn:
        return conn.execute(
            """
            SELECT p.place,
                   AVG(r.score)         AS avg_score,
                   COUNT(DISTINCT p.id) AS posts,
                   COUNT(r.score)       AS votes
            FROM posts p
            JOIN ratings r ON r.post_id = p.id
            GROUP BY p.place
            ORDER BY avg_score DESC
            """
        ).fetchall()


def get_user_posts(user_id: int, limit: int = 10):
    """Последние посты пользователя с их рейтингом (в т.ч. неоценённые)."""
    with closing(get_conn()) as conn:
        return conn.execute(
            """
            SELECT p.id, p.title, p.place,
                   AVG(r.score) AS avg_score, COUNT(r.score) AS votes
            FROM posts p
            LEFT JOIN ratings r ON r.post_id = p.id
            WHERE p.user_id = ?
            GROUP BY p.id
            ORDER BY p.created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()


def get_user_stats(user_id: int) -> dict:
    """Личная статистика: сколько выложил, сколько оценил, средние оценки."""
    with closing(get_conn()) as conn:
        posts = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        given = conn.execute(
            "SELECT COUNT(*) AS cnt, AVG(score) AS avg_score FROM ratings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        received = conn.execute(
            """
            SELECT COUNT(*) AS cnt, AVG(r.score) AS avg_score
            FROM ratings r JOIN posts p ON p.id = r.post_id
            WHERE p.user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return {
        "posts": posts,
        "given_cnt": given["cnt"],
        "given_avg": given["avg_score"],
        "received_cnt": received["cnt"],
        "received_avg": received["avg_score"],
    }


def get_all_user_ids() -> list[int]:
    """ID всех пользователей — для рассылки «Хрючева дня»."""
    with closing(get_conn()) as conn:
        return [row["id"] for row in conn.execute("SELECT id FROM users")]


def get_week_summary() -> dict:
    """Данные для еженедельного отчёта админу (за последние 7 дней)."""
    period = "-7 days"
    with closing(get_conn()) as conn:
        posts = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE created_at >= datetime('now', ?)", (period,)
        ).fetchone()[0]
        votes = conn.execute(
            "SELECT COUNT(*) FROM ratings WHERE created_at >= datetime('now', ?)", (period,)
        ).fetchone()[0]
        active = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) FROM (
                SELECT user_id FROM posts   WHERE created_at >= datetime('now', ?)
                UNION
                SELECT user_id FROM ratings WHERE created_at >= datetime('now', ?)
            )
            """,
            (period, period),
        ).fetchone()[0]
        top_user = conn.execute(
            """
            SELECT u.username, u.first_name, COUNT(*) AS actions
            FROM (
                SELECT user_id FROM posts   WHERE created_at >= datetime('now', ?)
                UNION ALL
                SELECT user_id FROM ratings WHERE created_at >= datetime('now', ?)
            ) a
            JOIN users u ON u.id = a.user_id
            GROUP BY a.user_id
            ORDER BY actions DESC
            LIMIT 1
            """,
            (period, period),
        ).fetchone()
    return {"posts": posts, "votes": votes, "active": active, "top_user": top_user}


# ---------- Лаба 2: поиск, история, экспорт ----------


def escape_like(text: str) -> str:
    """Экранирует спецсимволы LIKE (% и _), чтобы пользователь
    искал их как обычные символы, а не как шаблоны."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_posts(query: str, limit: int = FIND_LIMIT):
    """Ищет блюда, в названии которых встречается query (без учёта регистра).
    Свежие блюда идут первыми."""
    pattern = f"%{escape_like(query.lower())}%"
    with closing(get_conn()) as conn:
        return conn.execute(
            """
            SELECT p.id, p.title, p.place, p.created_at,
                   u.username, u.first_name,
                   AVG(r.score) AS avg_score, COUNT(r.score) AS votes
            FROM posts p
            JOIN users u ON u.id = p.user_id
            LEFT JOIN ratings r ON r.post_id = p.id
            WHERE PY_LOWER(p.title) LIKE ? ESCAPE '\\'
            GROUP BY p.id
            ORDER BY p.created_at DESC
            LIMIT ?
            """,
            (pattern, limit),
        ).fetchall()


def get_user_history(user_id: int, days: int):
    """История пользователя за последние days дней.
    Возвращает (оценки, которые он поставил; блюда, которые он выложил)."""
    period = f"-{days} days"
    with closing(get_conn()) as conn:
        given = conn.execute(
            """
            SELECT r.score, r.created_at, p.id AS post_id, p.title, p.place
            FROM ratings r
            JOIN posts p ON p.id = r.post_id
            WHERE r.user_id = ? AND r.created_at >= datetime('now', ?)
            ORDER BY r.created_at DESC
            """,
            (user_id, period),
        ).fetchall()
        posted = conn.execute(
            """
            SELECT p.id, p.title, p.created_at,
                   AVG(r.score) AS avg_score, COUNT(r.score) AS votes
            FROM posts p
            LEFT JOIN ratings r ON r.post_id = p.id
            WHERE p.user_id = ? AND p.created_at >= datetime('now', ?)
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """,
            (user_id, period),
        ).fetchall()
    return given, posted


def get_export_rows(user_id: int):
    """Вся история пользователя для CSV: его оценки и его блюда в одной таблице."""
    with closing(get_conn()) as conn:
        return conn.execute(
            """
            SELECT 'оценка'      AS kind, r.created_at, p.id AS post_id, p.title, p.place, p.price,
                   r.score       AS my_score,
                   (SELECT AVG(score) FROM ratings WHERE post_id = p.id) AS avg_score,
                   (SELECT COUNT(*)   FROM ratings WHERE post_id = p.id) AS votes
            FROM ratings r
            JOIN posts p ON p.id = r.post_id
            WHERE r.user_id = ?
            UNION ALL
            SELECT 'моё блюдо', p.created_at, p.id, p.title, p.place, p.price,
                   NULL,
                   (SELECT AVG(score) FROM ratings WHERE post_id = p.id),
                   (SELECT COUNT(*)   FROM ratings WHERE post_id = p.id)
            FROM posts p
            WHERE p.user_id = ?
            ORDER BY 2 DESC
            """,
            (user_id, user_id),
        ).fetchall()


def build_csv(rows) -> bytes:
    """Собирает CSV в памяти.
    Разделитель ';' и кодировка UTF-8 с BOM — чтобы русский Excel
    открыл файл двойным кликом без кракозябр и с правильными колонками."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "Тип", "Дата", "ID блюда", "Название", "Место",
        "Цена, ₽", "Моя оценка", "Средняя оценка", "Голосов",
    ])
    for row in rows:
        avg = row["avg_score"]
        writer.writerow([
            row["kind"],
            fmt_dt(row["created_at"]),
            row["post_id"],
            row["title"],
            PLACES.get(row["place"], row["place"]),
            row["price"] if row["price"] is not None else "",
            row["my_score"] if row["my_score"] is not None else "",
            f"{avg:.2f}".replace(".", ",") if avg is not None else "",  # запятая для Excel
            row["votes"],
        ])
    return buf.getvalue().encode("utf-8-sig")


# =====================================================================
# ОБЩИЕ ОБРАБОТЧИКИ
# =====================================================================


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Срабатывает на КАЖДОЕ обновление (группа -1) и регистрирует пользователя.
    Благодаря этому внешние ключи на users никогда не ломаются."""
    user = update.effective_user
    if user:
        upsert_user(user.id, user.username, user.first_name)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start и /help — приветствие и список команд."""
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ответ на любой текст вне диалога."""
    await update.message.reply_text(
        "Я понимаю только хрючево 🐷\nПришли фото еды или загляни в /help"
    )


# =====================================================================
# ДОБАВЛЕНИЕ БЛЮДА (диалог: фото → название → место → цена)
# =====================================================================

# Состояния диалога
PHOTO, TITLE, PLACE, PRICE = range(4)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/new — начало диалога, просим фото."""
    context.user_data["new_post"] = {}
    await update.message.reply_text("📸 Присылай фото хрючева. Отмена: /cancel")
    return PHOTO


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили фото (через /new или просто так) — просим название."""
    # Telegram присылает несколько размеров фото, берём самый большой
    file_id = update.message.photo[-1].file_id
    context.user_data["new_post"] = {"file_id": file_id}
    await update.message.reply_text(
        "Как называется это творение? (например: «Макароны с чем-то»)"
    )
    return TITLE


async def photo_expected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """В состоянии PHOTO прислали не фото."""
    await update.message.reply_text("Нужно именно фото 📸 Или /cancel для отмены.")
    return PHOTO


async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили название — проверяем и предлагаем выбрать место."""
    title = update.message.text.strip()
    if not 1 <= len(title) <= 100:
        await update.message.reply_text("Название должно быть от 1 до 100 символов.")
        return TITLE

    context.user_data["new_post"]["title"] = title

    # Кнопки мест, по две в ряд
    buttons = [
        InlineKeyboardButton(label, callback_data=f"place:{key}")
        for key, label in PLACES.items()
    ]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    await update.message.reply_text(
        "Откуда это хрючево?", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PLACE


async def receive_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажали кнопку места — просим цену."""
    query = update.callback_query
    await query.answer()

    place = query.data.split(":", 1)[1]
    if place not in PLACES:
        await query.edit_message_text("Неизвестное место, выбери из списка.")
        return PLACE

    context.user_data["new_post"]["place"] = place
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_price")]]
    )
    await query.edit_message_text(
        f"Место: {PLACES[place]}\n\nСколько стоило? Напиши число в рублях.",
        reply_markup=keyboard,
    )
    return PRICE


async def place_expected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """В состоянии PLACE прислали текст вместо нажатия кнопки."""
    await update.message.reply_text("Жми кнопку с местом 👆 Или /cancel для отмены.")
    return PLACE


async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили цену текстом — проверяем и сохраняем пост."""
    text = update.message.text.strip().replace("₽", "").replace(" ", "")
    try:
        price = int(text)
        if not 0 <= price <= 100_000:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Нужно целое число от 0 до 100000. Или жми «Пропустить».")
        return PRICE

    context.user_data["new_post"]["price"] = price
    return await save_new_post(update, context)


async def skip_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Нажали «Пропустить» — сохраняем пост без цены."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # убираем кнопку
    context.user_data["new_post"]["price"] = None
    return await save_new_post(update, context)


async def save_new_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Записывает собранные данные в БД и завершает диалог."""
    data = context.user_data.pop("new_post", {})
    chat_id = update.effective_chat.id

    # На всякий случай проверяем, что всё собрано
    if not all(k in data for k in ("file_id", "title", "place")):
        await context.bot.send_message(chat_id, "Что-то потерялось 🤔 Начни заново: /new")
        return ConversationHandler.END

    post_id = add_post(
        update.effective_user.id, data["file_id"], data["title"], data["place"], data.get("price")
    )
    logger.info("Новый пост #%s от %s", post_id, update.effective_user.id)

    await context.bot.send_message(
        chat_id,
        f"✅ Хрючево №{post_id} добавлено в ленту!\n"
        f"Когда наберётся {MIN_VOTES} голоса, пришлю вердикт.\n\n"
        "А пока оцени чужое: /rate",
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel — прерываем добавление."""
    context.user_data.pop("new_post", None)
    await update.message.reply_text("Отменено. Хрючево не пострадало 🐷")
    return ConversationHandler.END


# =====================================================================
# ОЦЕНКА БЛЮД
# =====================================================================


def rating_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Кнопки оценки 🐷1 … 🐷5."""
    row = [
        InlineKeyboardButton(f"🐷{score}", callback_data=f"rate:{post_id}:{score}")
        for score in range(1, 6)
    ]
    return InlineKeyboardMarkup([row])


def next_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «следующее блюдо»."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🍽 Следующее хрючево", callback_data="next")]]
    )


async def send_random_post(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    """Присылает случайное неоценённое блюдо с кнопками оценки."""
    post = get_random_unrated_post(user_id)
    if post is None:
        await context.bot.send_message(
            chat_id, "Ты оценил всё хрючево 🎉 Неси новое: /new"
        )
        return

    await context.bot.send_photo(
        chat_id=chat_id,
        photo=post["file_id"],
        caption=post_caption(post) + "\n\n<b>Насколько это хрючево?</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=rating_keyboard(post["id"]),
    )


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rate — показать блюдо для оценки."""
    await send_random_post(context, update.effective_chat.id, update.effective_user.id)


async def next_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «Следующее хрючево»."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)  # убираем кнопку со старого поста
    await send_random_post(context, update.effective_chat.id, update.effective_user.id)


async def rate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Нажатие на 🐷1…🐷5: сохраняем голос, показываем рейтинг, уведомляем автора."""
    query = update.callback_query
    _, post_id, score = query.data.split(":")
    post_id, score = int(post_id), int(score)
    user_id = update.effective_user.id

    post = get_post(post_id)
    if post is None:
        await query.answer("Это блюдо уже исчезло 🫥", show_alert=True)
        return
    if post["user_id"] == user_id:
        await query.answer("Своё хрючево оценивать нельзя 🙅", show_alert=True)
        return
    if not add_rating(post_id, user_id, score):
        await query.answer("Ты уже оценил это блюдо", show_alert=True)
        return

    await query.answer(f"Принято: {'🐷' * score}")
    avg, votes = get_post_rating(post_id)

    # Обновляем подпись под фото: оценка пользователя + текущий рейтинг
    await query.edit_message_caption(
        caption=(
            post_caption(post)
            + f"\n\nТвоя оценка: {'🐷' * score}\n"
            + rating_line(avg, votes)
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=next_keyboard(),
    )

    # Уведомляем автора блюда о новой оценке
    text = (
        f"🔔 Твоё «{html.escape(post['title'])}» оценили на {'🐷' * score}\n"
        + rating_line(avg, votes)
    )
    if votes == MIN_VOTES:
        text += "\n\n🎉 Набралось достаточно голосов, вердикт вынесен!"
    try:
        await context.bot.send_message(post["user_id"], text, parse_mode=ParseMode.HTML)
    except Forbidden:
        # Автор заблокировал бота — просто пропускаем
        logger.info("Автор %s заблокировал бота, уведомление не отправлено", post["user_id"])
    except TelegramError as e:
        logger.warning("Не удалось уведомить автора %s: %s", post["user_id"], e)


# =====================================================================
# СТАТИСТИКА И ТОПЫ
# =====================================================================


def format_ranked(rows, header: str) -> str:
    """Формирует текст топа из строк get_ranked_posts."""
    if not rows:
        return f"Пока мало оценок 🤷 Нужно хотя бы {MIN_VOTES} голоса на блюдо.\nПомоги: /rate"
    lines = [header, ""]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. <b>{html.escape(row['title'])}</b> — {pigs(row['avg_score'])} "
            f"{row['avg_score']:.1f} ({row['votes']} гол.)\n"
            f"    {PLACES.get(row['place'], row['place'])} · автор {display_name(row)}"
        )
    return "\n".join(lines)


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/top — самое хрючевное."""
    rows = get_ranked_posts(order="DESC")
    await update.message.reply_text(
        format_ranked(rows, "🏆 <b>Топ хрючева</b>"), parse_mode=ParseMode.HTML
    )


async def antitop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/antitop — самое приличное."""
    rows = get_ranked_posts(order="ASC")
    await update.message.reply_text(
        format_ranked(rows, "✨ <b>Антитоп: самая приличная еда</b>"), parse_mode=ParseMode.HTML
    )


async def places_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/places — средняя хрючевальность по местам."""
    rows = get_place_stats()
    if not rows:
        await update.message.reply_text("Пока нет оценённых блюд. Начни с /rate")
        return

    lines = ["📍 <b>Хрючевальность по местам</b>", ""]
    for row in rows:
        lines.append(
            f"{PLACES.get(row['place'], row['place'])}: {pigs(row['avg_score'])} "
            f"{row['avg_score']:.1f}  ({row['posts']} блюд, {row['votes']} гол.)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/my — последние посты пользователя."""
    rows = get_user_posts(update.effective_user.id)
    if not rows:
        await update.message.reply_text("У тебя пока нет хрючева. Добавь: /new")
        return

    lines = ["🍽 <b>Твоё хрючево</b> (последние 10)", ""]
    for row in rows:
        lines.append(
            f"№{row['id']} <b>{html.escape(row['title'])}</b> — "
            f"{short_rating(row['avg_score'], row['votes'])}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats — личная статистика и звание."""
    s = get_user_stats(update.effective_user.id)
    actions = s["posts"] + s["given_cnt"]
    rank, next_left = get_rank(actions)

    lines = [
        "📊 <b>Твоя статистика</b>",
        "",
        f"Звание: {rank}",
        f"До следующего звания: {next_left} действий" if next_left else "Максимальное звание 👑",
        "",
        f"Выложено блюд: {s['posts']}",
        f"Оценено чужих: {s['given_cnt']}",
    ]
    if s["given_avg"] is not None:
        lines.append(f"Ты в среднем ставишь: {s['given_avg']:.1f} 🐷")
    if s["received_avg"] is not None:
        lines.append(
            f"Твоё хрючево в среднем: {pigs(s['received_avg'])} "
            f"{s['received_avg']:.1f} ({s['received_cnt']} гол.)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def build_report() -> str:
    """Текст сводного отчёта за неделю (для /report и еженедельной рассылки)."""
    week = get_week_summary()
    best = get_ranked_posts(order="DESC", limit=1, min_votes=1, days=7)
    worst = get_ranked_posts(order="ASC", limit=1, min_votes=1, days=7)
    places = get_place_stats()

    lines = [
        "📋 <b>Отчёт Хрючевальни за 7 дней</b>",
        "",
        f"Новых блюд: {week['posts']}",
        f"Голосов: {week['votes']}",
        f"Активных пользователей: {week['active']}",
    ]
    if week["top_user"]:
        lines.append(
            f"Самый активный: {display_name(week['top_user'])} ({week['top_user']['actions']} действий)"
        )
    if best:
        b = best[0]
        lines.append(f"Хрючево недели: «{html.escape(b['title'])}» — {b['avg_score']:.1f} 🐷")
    if worst:
        w = worst[0]
        lines.append(f"Самое приличное: «{html.escape(w['title'])}» — {w['avg_score']:.1f} 🐷")
    if places:
        lines.append("")
        lines.append(f"Самое хрючевное место: {PLACES.get(places[0]['place'])} ({places[0]['avg_score']:.1f})")
        lines.append(f"Самое приличное место: {PLACES.get(places[-1]['place'])} ({places[-1]['avg_score']:.1f})")
    return "\n".join(lines)


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/report — сводный отчёт, только для админов."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("🔒 Отчёт доступен только админам.")
        return
    await update.message.reply_text(build_report(), parse_mode=ParseMode.HTML)


# =====================================================================
# ЛАБА 2: ПОИСК, ИСТОРИЯ, ЭКСПОРТ
# =====================================================================


async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/find <слово> — поиск блюд по названию с кнопками для открытия."""
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Что ищем? Напиши так: /find макароны")
        return
    if len(query) < 2:
        await update.message.reply_text("Нужно хотя бы 2 символа для поиска.")
        return
    if len(query) > 50:
        await update.message.reply_text("Слишком длинный запрос, сократи до 50 символов.")
        return

    rows = find_posts(query)
    if not rows:
        await update.message.reply_text(
            f"По запросу «{query}» хрючева не найдено 🤷\nМожет, добавишь его сам? /new"
        )
        return

    lines = [f"🔎 <b>Найдено по запросу «{html.escape(query)}»:</b>", ""]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. <b>{html.escape(row['title'])}</b> — {short_rating(row['avg_score'], row['votes'])}\n"
            f"    {PLACES.get(row['place'], row['place'])} · {display_name(row)} · {fmt_dt(row['created_at'])}"
        )
    lines.append("\nЖми номер, чтобы открыть блюдо 👇")

    # Кнопки с номерами, по 5 в ряд
    buttons = [
        InlineKeyboardButton(str(i), callback_data=f"show:{row['id']}")
        for i, row in enumerate(rows, 1)
    ]
    keyboard = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_post_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка из результатов /find: показать фото блюда с рейтингом.
    Если блюдо чужое и ещё не оценено — сразу даём кнопки оценки."""
    query = update.callback_query
    await query.answer()

    post_id = int(query.data.split(":", 1)[1])
    post = get_post(post_id)
    if post is None:
        await context.bot.send_message(update.effective_chat.id, "Это блюдо уже исчезло 🫥")
        return

    user_id = update.effective_user.id
    avg, votes = get_post_rating(post_id)
    caption = post_caption(post) + "\n\n" + rating_line(avg, votes)

    can_rate = post["user_id"] != user_id and not has_rated(post_id, user_id)
    if can_rate:
        caption += "\n\n<b>Насколько это хрючево?</b>"

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=post["file_id"],
        caption=caption,
        parse_mode=ParseMode.HTML,
        reply_markup=rating_keyboard(post_id) if can_rate else None,
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/history [дни] — оценки и блюда пользователя за последние N дней."""
    days = HISTORY_DEFAULT_DAYS
    if context.args:
        try:
            days = int(context.args[0])
            if not 1 <= days <= HISTORY_MAX_DAYS:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"Укажи число дней от 1 до {HISTORY_MAX_DAYS}, например: /history 30"
            )
            return

    given, posted = get_user_history(update.effective_user.id, days)
    if not given and not posted:
        await update.message.reply_text(
            f"За последние {days} дн. ты ничего не делал в Хрючевальне 😴\n"
            "Начни с /rate или /new"
        )
        return

    lines = [f"🕓 <b>Твоя история за {days} дн.</b>", ""]

    if given:
        avg_given = sum(r["score"] for r in given) / len(given)
        lines.append(f"<b>Твои оценки</b> ({len(given)} шт., в среднем {avg_given:.1f} 🐷):")
        for r in given[:HISTORY_SHOW]:
            lines.append(f"{fmt_dt(r['created_at'])} — {html.escape(r['title'])}: {'🐷' * r['score']}")
        if len(given) > HISTORY_SHOW:
            lines.append(f"…и ещё {len(given) - HISTORY_SHOW}")
        lines.append("")

    if posted:
        lines.append(f"<b>Твои блюда</b> ({len(posted)} шт.):")
        for p in posted[:HISTORY_SHOW]:
            lines.append(
                f"{fmt_dt(p['created_at'])} — {html.escape(p['title'])}: "
                f"{short_rating(p['avg_score'], p['votes'])}"
            )
        if len(posted) > HISTORY_SHOW:
            lines.append(f"…и ещё {len(posted) - HISTORY_SHOW}")
        lines.append("")

    lines.append("Полная история файлом: /export")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export — присылает CSV со всей историей пользователя."""
    rows = get_export_rows(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Экспортировать пока нечего 🤷 Оцени что-нибудь: /rate")
        return

    data = build_csv(rows)
    filename = f"hryuchevalnya_{datetime.now(TZ):%Y-%m-%d}.csv"
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename=filename,
        caption=f"📁 Твоя история: {len(rows)} записей. Открывается в Excel или Google Таблицах.",
    )


# =====================================================================
# ЗАДАЧИ ПО РАСПИСАНИЮ
# =====================================================================


async def daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Каждый день: рассылаем всем «Хрючево дня» — блюдо с максимальной средней за сутки."""
    rows = get_ranked_posts(order="DESC", limit=1, min_votes=1, days=1)
    if not rows:
        logger.info("Хрючево дня: за сутки нет оценённых блюд, рассылки не будет")
        return

    post = rows[0]
    caption = (
        "🏅 <b>Хрючево дня!</b>\n\n"
        + post_caption(post)
        + f"\n\nСредняя: {pigs(post['avg_score'])} {post['avg_score']:.1f} ({post['votes']} гол.)"
        + f"\nАвтор: {display_name(post)}"
    )

    sent = 0
    for user_id in get_all_user_ids():
        try:
            await context.bot.send_photo(user_id, post["file_id"], caption=caption, parse_mode=ParseMode.HTML)
            sent += 1
        except Forbidden:
            pass  # пользователь заблокировал бота
        except TelegramError as e:
            logger.warning("Не удалось отправить дайджест %s: %s", user_id, e)
    logger.info("Хрючево дня разослано: %s получателей", sent)


async def weekly_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Раз в неделю: отправляем сводный отчёт всем админам."""
    text = build_report()
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.warning("Не удалось отправить отчёт админу %s: %s", admin_id, e)


# =====================================================================
# ОБРАБОТКА ОШИБОК
# =====================================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик: логируем ошибку и вежливо сообщаем пользователю."""
    logger.error("Ошибка при обработке обновления", exc_info=context.error)

    if isinstance(update, Update) and update.effective_chat:
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                "😵 Что-то сломалось в Хрючевальне. Попробуй ещё раз чуть позже.",
            )
        except TelegramError:
            pass  # если даже это не отправилось — ничего не делаем


# =====================================================================
# ЗАПУСК
# =====================================================================


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("❌ Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS пуст — /report и еженедельный отчёт работать не будут")

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Группа -1: регистрация пользователя на любое обновление (до остальных обработчиков)
    app.add_handler(TypeHandler(Update, track_user), group=-1)

    # Диалог добавления блюда
    new_post_conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_command),
            MessageHandler(filters.PHOTO, receive_photo),  # можно просто прислать фото
        ],
        states={
            PHOTO: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.ALL & ~filters.COMMAND, photo_expected),
            ],
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            PLACE: [
                CallbackQueryHandler(receive_place, pattern=r"^place:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, place_expected),
            ],
            PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_price),
                CallbackQueryHandler(skip_price, pattern=r"^skip_price$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(new_post_conv)

    # Команды лабы 1
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("top", top_command))
    app.add_handler(CommandHandler("antitop", antitop_command))
    app.add_handler(CommandHandler("places", places_command))
    app.add_handler(CommandHandler("my", my_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("report", report_command))

    # Команды лабы 2
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("export", export_command))

    # Кнопки
    app.add_handler(CallbackQueryHandler(rate_callback, pattern=r"^rate:\d+:[1-5]$"))
    app.add_handler(CallbackQueryHandler(next_callback, pattern=r"^next$"))
    app.add_handler(CallbackQueryHandler(show_post_callback, pattern=r"^show:\d+$"))

    # Любой другой текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))

    # Ошибки
    app.add_error_handler(error_handler)

    # Расписание (нужен python-telegram-bot[job-queue])
    if app.job_queue:
        app.job_queue.run_daily(daily_digest, time=time(20, 0, tzinfo=TZ), name="daily_digest")
        # В PTB v20+ дни недели: 0 = воскресенье, 1 = понедельник, …
        app.job_queue.run_daily(weekly_report, time=time(10, 0, tzinfo=TZ), days=(1,), name="weekly_report")
        logger.info("Расписание: хрючево дня в 20:00, отчёт по понедельникам в 10:00 (%s)", TZ)
    else:
        logger.warning("JobQueue недоступен — установи python-telegram-bot[job-queue]")

    logger.info("🐷 Хрючевальня запущена")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
