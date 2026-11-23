"""
Админ-консоль для бота «Напоминалка» (отдельный модуль).

Как это работает
- Вы задаёте секретный код в переменной ADMIN_CODE (от 10 символов).
- Любой человек, который отправит боту в личку этот код, становится админом
  и получает консоль. Само сообщение с кодом бот сразу удаляет из чата.
- Владелец (ADMIN_ID) всегда админ. Он получает уведомление о каждом новом
  админе и может убрать любого одним нажатием.
- Повторно открыть консоль (без кода): команда /panel.

Подключение — две строки в bot.py:
    import admin_console                                   # вверху файла
    admin_console.setup(app, db=db, owner_id=ADMIN_ID,     # в main(), до app.run_polling()
                        tz=TZ, main_menu=main_menu)

Запуск:  set ADMIN_CODE=ваш_длинный_секретный_код
Если ADMIN_CODE не задан или короче 10 символов, консоль просто отключена.

Требование к db(): функция возвращает sqlite3-соединение с row_factory = sqlite3.Row
(в bot.py так и есть) и таблицами masters(chat_id, name, paid_until), appointments.
"""
import hmac
import logging
import os
import re
import time
from datetime import datetime

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.ext import (
    ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

_cfg = {}


# ---------- Вспомогательное ----------
def _db():
    return _cfg["db"]()


def _fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, _cfg["tz"]).strftime("%d.%m.%Y")


def _fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, _cfg["tz"]).strftime("%d.%m %H:%M")


def _btn(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=f"adm:{action}")


def is_owner(uid: int) -> bool:
    return uid == _cfg["owner_id"]


def is_admin(uid: int) -> bool:
    if is_owner(uid):
        return True
    with _db() as c:
        return c.execute("SELECT 1 FROM admins WHERE chat_id=?", (uid,)).fetchone() is not None


def _log(admin_id: int, action: str):
    with _db() as c:
        c.execute(
            "INSERT INTO admin_log(ts, admin_id, action) VALUES(?,?,?)",
            (int(time.time()), admin_id, action),
        )
    logging.info("ADMIN %s: %s", admin_id, action)


def _init_tables():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS admins(
            chat_id INTEGER PRIMARY KEY,
            name TEXT,
            added INTEGER
        );
        CREATE TABLE IF NOT EXISTS admin_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            admin_id INTEGER,
            action TEXT
        );
        """)


def _panel_kb(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [_btn("📊 Статистика", "stats"), _btn("👥 Подписчики", "subs")],
        [_btn("➕ Выдать доступ", "grant")],
    ]
    if is_owner(uid):
        rows.append([_btn("🛡 Админы", "admins"), _btn("📜 Журнал", "log")])
    else:
        rows.append([_btn("🚪 Выйти из консоли", "logout")])
    return InlineKeyboardMarkup(rows)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("⬅️ Назад", "panel")]])


async def _edit(q, text: str, kb=None):
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except Exception as e:  # например, «сообщение не изменилось»
        logging.debug("edit: %s", e)


# ---------- Действия ----------
async def _grant(context, chat_id: int, days: int) -> int:
    with _db() as c:
        c.execute("INSERT OR IGNORE INTO masters(chat_id, name) VALUES(?, ?)", (chat_id, ""))
        row = c.execute("SELECT paid_until FROM masters WHERE chat_id=?", (chat_id,)).fetchone()
        base = max(row["paid_until"] or 0, time.time())
        new_until = int(base + days * 86400)
        c.execute("UPDATE masters SET paid_until=? WHERE chat_id=?", (new_until, chat_id))
    menu = _cfg.get("main_menu")
    try:
        await context.bot.send_message(
            chat_id,
            f"✅ Оплата подтверждена! Подписка активна до {_fmt_date(new_until)}.\n"
            "Меню открыто — кнопки внизу экрана 👇",
            reply_markup=menu(chat_id) if menu else None,
        )
    except Exception as e:
        logging.warning("Не удалось уведомить мастера: %s", e)
    return new_until


async def _revoke(context, chat_id: int):
    with _db() as c:
        c.execute("UPDATE masters SET paid_until=0 WHERE chat_id=?", (chat_id,))
    try:
        await context.bot.send_message(
            chat_id,
            "Ваша подписка отключена администратором. Если это ошибка — свяжитесь с владельцем.",
            reply_markup=ReplyKeyboardRemove(),
        )
    except Exception as e:
        logging.warning("Не удалось уведомить мастера: %s", e)


def _stats_text() -> str:
    now = int(time.time())
    owner = _cfg["owner_id"]
    with _db() as c:
        total = c.execute("SELECT COUNT(*) FROM masters WHERE chat_id!=?", (owner,)).fetchone()[0]
        active = c.execute(
            "SELECT COUNT(*) FROM masters WHERE paid_until>? AND chat_id!=?", (now, owner)
        ).fetchone()[0]
        soon = c.execute(
            "SELECT COUNT(*) FROM masters WHERE paid_until>? AND paid_until<=? AND chat_id!=?",
            (now, now + 7 * 86400, owner),
        ).fetchone()[0]
        upcoming = c.execute(
            "SELECT COUNT(*) FROM appointments WHERE ts>? AND status!='cancelled'", (now,)
        ).fetchone()[0]
        total_appts = c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        clients = c.execute(
            "SELECT COUNT(DISTINCT client_chat) FROM appointments WHERE client_chat IS NOT NULL"
        ).fetchone()[0]
        admins = c.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
    return (
        "📊 Статистика\n\n"
        f"Мастеров заходило в бота: {total}\n"
        f"С активной подпиской: {active}\n"
        f"Подписка кончается в ближайшие 7 дней: {soon}\n\n"
        f"Предстоящих записей: {upcoming}\n"
        f"Всего записей создано: {total_appts}\n"
        f"Клиентов подключено: {clients}\n\n"
        f"Админов (кроме владельца): {admins}"
    )


# ---------- Вход по коду и текстовые ответы ----------
async def _login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        await update.message.delete()  # убираем код из переписки
    except Exception:
        pass
    new_admin = not is_admin(user.id)
    if new_admin:
        with _db() as c:
            c.execute(
                "INSERT OR IGNORE INTO admins(chat_id, name, added) VALUES(?,?,?)",
                (user.id, user.full_name, int(time.time())),
            )
        _log(user.id, f"вошёл по коду: {user.full_name}")
        kb = InlineKeyboardMarkup([[_btn("🗑 Убрать доступ", f"rmy:{user.id}")]])
        try:
            await context.bot.send_message(
                _cfg["owner_id"],
                f"🔐 Новый админ вошёл по коду: {user.full_name} (ID {user.id}).",
                reply_markup=kb,
            )
        except Exception as e:
            logging.warning("Не удалось уведомить владельца: %s", e)
    await context.bot.send_message(user.id, "🛠 Админ-консоль", reply_markup=_panel_kb(user.id))


async def _handle_wait(update: Update, context: ContextTypes.DEFAULT_TYPE, wait: str, text: str):
    uid = update.effective_user.id
    if wait == "grant":
        m = re.fullmatch(r"\s*(\d+)\s+(\d{1,3})\s*", text)
        if not m or not (1 <= int(m.group(2)) <= 365):
            await update.message.reply_text(
                "Формат: ID мастера и число дней (1–365) через пробел, например:\n123456789 30"
            )
            return
        chat_id, days = int(m.group(1)), int(m.group(2))
        new_until = await _grant(context, chat_id, days)
        context.user_data.pop("adm_wait", None)
        _log(uid, f"выдал доступ {chat_id} на {days} дн. (до {_fmt_date(new_until)})")
        await update.message.reply_text(
            f"✅ Доступ для {chat_id} открыт до {_fmt_date(new_until)}.",
            reply_markup=_panel_kb(uid),
        )


async def _text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Смотрит все личные тексты раньше остальных обработчиков бота."""
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if hmac.compare_digest(text.encode("utf-8"), _cfg["code"].encode("utf-8")):
        await _login(update, context)
        raise ApplicationHandlerStop  # код не должен попасть в остальную логику бота
    uid = update.effective_user.id
    wait = context.user_data.get("adm_wait")
    if wait and is_admin(uid):
        await _handle_wait(update, context, wait, text)
        raise ApplicationHandlerStop


async def _panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return  # посторонним не показываем, что консоль существует
    await update.message.reply_text("🛠 Админ-консоль", reply_markup=_panel_kb(uid))


# ---------- Кнопки консоли ----------
async def _cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    if not is_admin(uid):
        return
    parts = q.data.split(":")
    action = parts[1]
    arg = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    if action == "panel":
        context.user_data.pop("adm_wait", None)
        await _edit(q, "🛠 Админ-консоль", _panel_kb(uid))

    elif action == "cancel":
        context.user_data.pop("adm_wait", None)
        await _edit(q, "🛠 Админ-консоль", _panel_kb(uid))

    elif action == "stats":
        await _edit(q, _stats_text(), _back_kb())

    elif action == "subs":
        now = int(time.time())
        with _db() as c:
            rows = c.execute(
                "SELECT m.chat_id, m.name, m.paid_until, "
                "(SELECT COUNT(*) FROM appointments a WHERE a.master_id=m.chat_id "
                " AND a.ts>? AND a.status!='cancelled') AS upcoming "
                "FROM masters m WHERE m.paid_until>? AND m.chat_id!=? "
                "ORDER BY m.paid_until LIMIT 30",
                (now, now, _cfg["owner_id"]),
            ).fetchall()
        if not rows:
            await _edit(q, "Активных подписчиков нет.", _back_kb())
            return
        lines = [f"👥 Активных подписчиков: {len(rows)}\n"]
        buttons = []
        for i, r in enumerate(rows, 1):
            name = r["name"] or "без имени"
            lines.append(
                f"{i}. {name} (ID {r['chat_id']}) — до {_fmt_date(r['paid_until'])}, "
                f"записей впереди: {r['upcoming']}"
            )
            buttons.append([_btn(f"🚫 Отключить: {name}", f"rev:{r['chat_id']}")])
        buttons.append([_btn("⬅️ Назад", "panel")])
        await _edit(q, "\n".join(lines), InlineKeyboardMarkup(buttons))

    elif action == "rev" and arg:
        kb = InlineKeyboardMarkup([[
            _btn("Да, отключить", f"revy:{arg}"), _btn("Нет", "subs"),
        ]])
        await _edit(q, f"Отключить подписку мастера ID {arg}? Доступ закроется сразу.", kb)

    elif action == "revy" and arg:
        await _revoke(context, arg)
        _log(uid, f"отключил подписку {arg}")
        await _edit(q, f"✅ Подписка мастера ID {arg} отключена.", _back_kb())

    elif action == "grant":
        context.user_data["adm_wait"] = "grant"
        kb = InlineKeyboardMarkup([[_btn("✖️ Отмена", "cancel")]])
        await _edit(
            q,
            "Отправьте ID мастера и число дней через пробел, например:\n123456789 30",
            kb,
        )

    elif action == "logout" and not is_owner(uid):
        with _db() as c:
            c.execute("DELETE FROM admins WHERE chat_id=?", (uid,))
        _log(uid, "вышел из консоли")
        await _edit(q, "Вы вышли из консоли. Чтобы войти снова, отправьте секретный код.")

    # ---- только для владельца ----
    elif action == "admins" and is_owner(uid):
        with _db() as c:
            rows = c.execute("SELECT * FROM admins ORDER BY added").fetchall()
        if not rows:
            await _edit(
                q,
                "Других админов нет. Чтобы добавить, дайте человеку секретный код.",
                _back_kb(),
            )
            return
        lines = ["🛡 Админы:\n"]
        buttons = []
        for r in rows:
            name = r["name"] or "без имени"
            lines.append(f"• {name} (ID {r['chat_id']}) — с {_fmt_date(r['added'])}")
            buttons.append([_btn(f"🗑 Убрать: {name}", f"rm:{r['chat_id']}")])
        buttons.append([_btn("⬅️ Назад", "panel")])
        await _edit(q, "\n".join(lines), InlineKeyboardMarkup(buttons))

    elif action == "rm" and is_owner(uid) and arg:
        kb = InlineKeyboardMarkup([[_btn("Да, убрать", f"rmy:{arg}"), _btn("Нет", "admins")]])
        await _edit(q, f"Убрать админа ID {arg}? Он потеряет доступ к консоли.", kb)

    elif action == "rmy" and is_owner(uid) and arg:
        with _db() as c:
            c.execute("DELETE FROM admins WHERE chat_id=?", (arg,))
        _log(uid, f"убрал админа {arg}")
        await _edit(q, f"✅ Админ ID {arg} убран.", _back_kb())
        try:
            await context.bot.send_message(arg, "Ваш доступ к админ-консоли отключён.")
        except Exception:
            pass

    elif action == "log" and is_owner(uid):
        with _db() as c:
            rows = c.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT 15").fetchall()
        if not rows:
            await _edit(q, "Журнал пуст.", _back_kb())
            return
        lines = ["📜 Последние действия:\n"] + [
            f"{_fmt_dt(r['ts'])} — ID {r['admin_id']}: {r['action']}" for r in rows
        ]
        await _edit(q, "\n".join(lines), _back_kb())


# ---------- Подключение ----------
def setup(app, *, db, owner_id: int, tz, main_menu=None):
    code = os.getenv("ADMIN_CODE", "")
    if len(code) < 10:
        logging.warning("Админ-консоль отключена: задайте ADMIN_CODE (от 10 символов).")
        return
    _cfg.update(db=db, owner_id=owner_id, tz=tz, main_menu=main_menu, code=code)
    _init_tables()
    private = filters.ChatType.PRIVATE
    # group=-1: этот обработчик срабатывает раньше остальных и может их остановить
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, _text_router), group=-1)
    app.add_handler(CommandHandler("panel", _panel_cmd, filters=private))
    app.add_handler(CallbackQueryHandler(_cb, pattern=r"^adm:"))
    logging.info("Админ-консоль включена.")
