"""Админка: проекты и условия, водители, проверка отчётов, выгрузка в Excel."""
import calendar
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaDocument,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..calc import EXPENSE_KINDS, RATE_FIELDS, money, num
from ..config import Config
from ..db import Database
from ..export import build_export, period_label
from ..parse import parse_date, parse_number
from ..views import compute, rates_text, ru_date, shift_card
from .common import BTN_ADMIN, IsAdmin, cancel_kb, menu_for, review_kb
from .driver import show_shift

router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

PROJECT_STEPS = [
    ("shift_rate", "Ставка за смену, ₽?"),
    ("shift_hours", "Сколько часов в смене? (обычно 12 или 10)"),
    ("overtime_rate", "Переработка, ₽ за час? (0 — не оплачивается)"),
    ("km_included", "Сколько км включено в смену? (0 — если км не считаем)"),
    ("km_rate", "Оплата за км сверх лимита, ₽/км? (0 — не оплачиваем)"),
]
MEMBER_TEXT_FIELDS = {"driver_name": "Водитель", "car": "Авто", "contractor": "Контрагент"}


class Adm(StatesGroup):
    new_name = State()
    new_rate = State()
    edit = State()
    reject = State()
    period = State()


async def bot_link(bot: Bot, code: str) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start=j_{code}"


def admin_menu(pending: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="📁 Проекты", callback_data="ap")
    kb.button(text="➕ Новый проект", callback_data="an")
    kb.button(text=f"📥 На проверке ({pending})", callback_data="aq")
    kb.adjust(2, 1)
    return kb.as_markup()


@router.message(StateFilter("*"), Command("admin"))
@router.message(StateFilter("*"), F.text == BTN_ADMIN)
async def admin_home(message: Message, state: FSMContext, db: Database):
    await state.clear()
    pending = len(await db.pending_shifts())
    await message.answer("<b>Админка</b>", reply_markup=admin_menu(pending))


@router.callback_query(F.data == "ah")
async def admin_home_cb(call: CallbackQuery, db: Database):
    pending = len(await db.pending_shifts())
    await call.message.edit_text("<b>Админка</b>", reply_markup=admin_menu(pending))
    await call.answer()


# ---------------- Проекты ----------------

@router.callback_query(F.data == "ap")
async def projects(call: CallbackQuery, db: Database):
    await show_projects(call, db)
    await call.answer()


async def show_projects(call: CallbackQuery, db: Database):
    kb = InlineKeyboardBuilder()
    for p in await db.list_projects():
        kb.button(text=p["name"], callback_data=f"pv:{p['id']}")
    kb.button(text="➕ Новый проект", callback_data="an")
    kb.button(text="← Назад", callback_data="ah")
    kb.adjust(1)
    await call.message.edit_text("<b>Проекты</b>", reply_markup=kb.as_markup())


async def project_view(db: Database, bot: Bot, project_id: int):
    p = await db.get_project(project_id)
    members = await db.project_members(project_id)
    text = (
        f"<b>📁 {escape(p['name'])}</b>\n\n{rates_text(p)}\n\n"
        f"Водителей: {len(members)}\n"
        f"Ссылка для водителей:\n{await bot_link(bot, p['invite_code'])}"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Условия", callback_data=f"pr:{project_id}")
    kb.button(text="👥 Водители", callback_data=f"pm:{project_id}")
    kb.button(text="📤 Выгрузка в Excel", callback_data=f"px:{project_id}")
    kb.button(text="✏️ Название", callback_data=f"ed:p:{project_id}:name")
    kb.button(text="📦 В архив", callback_data=f"pA:{project_id}")
    kb.button(text="← Проекты", callback_data="ap")
    kb.adjust(2, 1, 2, 1)
    return text, kb.as_markup()


@router.callback_query(F.data.startswith("pv:"))
async def project_cb(call: CallbackQuery, db: Database, bot: Bot):
    text, markup = await project_view(db, bot, int(call.data[3:]))
    await call.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)
    await call.answer()


@router.callback_query(F.data.startswith("pA:"))
async def project_archive(call: CallbackQuery, db: Database):
    pid = int(call.data[3:])
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, в архив", callback_data=f"pZ:{pid}")
    kb.button(text="Нет", callback_data=f"pv:{pid}")
    await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    await call.answer("Ссылка перестанет работать, водители не смогут отчитываться")


@router.callback_query(F.data.startswith("pZ:"))
async def project_archive_ok(call: CallbackQuery, db: Database):
    await db.update_project(int(call.data[3:]), "archived", 1)
    await call.answer("Проект в архиве")
    await show_projects(call, db)


@router.callback_query(F.data.startswith("pr:"))
async def project_rates(call: CallbackQuery, db: Database):
    pid = int(call.data[3:])
    kb = InlineKeyboardBuilder()
    for key, label in RATE_FIELDS.items():
        kb.button(text=label, callback_data=f"ed:p:{pid}:{key}")
    kb.button(text="← Назад", callback_data=f"pv:{pid}")
    kb.adjust(1)
    await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "an")
async def new_project(call: CallbackQuery, state: FSMContext):
    await state.set_state(Adm.new_name)
    await call.message.answer("Название проекта?", reply_markup=cancel_kb())
    await call.answer()


@router.message(Adm.new_name, F.text)
async def new_project_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:100], rates={}, step=0)
    await state.set_state(Adm.new_rate)
    await message.answer(PROJECT_STEPS[0][1])


@router.message(Adm.new_rate, F.text)
async def new_project_rate(message: Message, state: FSMContext, db: Database,
                           bot: Bot, config: Config):
    value = parse_number(message.text)
    if value is None:
        await message.answer("Нужно число")
        return
    data = await state.get_data()
    step, rates = data["step"], data["rates"]
    rates[PROJECT_STEPS[step][0]] = value
    step += 1
    if step < len(PROJECT_STEPS):
        await state.update_data(rates=rates, step=step)
        await message.answer(PROJECT_STEPS[step][1])
        return
    await state.clear()
    pid = await db.create_project(message.from_user.id, data["name"], **rates)
    await message.answer(
        "✅ Проект создан. Перешлите ссылку водителям — они зарегистрируются сами.\n"
        "Персональные условия водителя можно задать в «👥 Водители».",
        reply_markup=await menu_for(db, config, message.from_user.id),
    )
    text, markup = await project_view(db, bot, pid)
    await message.answer(text, reply_markup=markup, disable_web_page_preview=True)


# ---------------- Водители ----------------

@router.callback_query(F.data.startswith("pm:"))
async def members(call: CallbackQuery, db: Database):
    await show_members(call, db, int(call.data[3:]))
    await call.answer()


async def show_members(call: CallbackQuery, db: Database, pid: int):
    kb = InlineKeyboardBuilder()
    for m in await db.project_members(pid):
        kb.button(text=f"{m['driver_name']} · {m['car']}", callback_data=f"mv:{m['id']}")
    kb.button(text="← Назад", callback_data=f"pv:{pid}")
    kb.adjust(1)
    await call.message.edit_text("<b>Водители</b> (нажмите, чтобы изменить)",
                                 reply_markup=kb.as_markup())


async def member_view(db: Database, member_id: int):
    m = await db.get_member(member_id)
    p = await db.get_project(m["project_id"])
    text = (
        f"<b>👤 {escape(m['driver_name'])}</b>\n"
        f"Авто: {escape(m['car'] or '—')}\nКонтрагент: {escape(m['contractor'] or '—')}\n"
        f"Проект: {escape(p['name'])}\n\n<b>Условия</b>\n{rates_text(m, p)}\n\n"
        "<i>Персональные условия перекрывают условия проекта. "
        "Чтобы вернуть как в проекте — отправьте «-».</i>"
    )
    kb = InlineKeyboardBuilder()
    for key, label in MEMBER_TEXT_FIELDS.items():
        kb.button(text=f"✏️ {label}", callback_data=f"ed:m:{member_id}:{key}")
    for key, label in RATE_FIELDS.items():
        kb.button(text=label, callback_data=f"ed:m:{member_id}:{key}")
    kb.button(text="🚫 Отключить от проекта", callback_data=f"mO:{member_id}")
    kb.button(text="← Водители", callback_data=f"pm:{m['project_id']}")
    kb.adjust(3, *[1] * (len(RATE_FIELDS) + 2))
    return text, kb.as_markup()


@router.callback_query(F.data.startswith("mv:"))
async def member_cb(call: CallbackQuery, db: Database):
    text, markup = await member_view(db, int(call.data[3:]))
    await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data.startswith("mO:"))
async def member_off(call: CallbackQuery, db: Database):
    mid = int(call.data[3:])
    m = await db.get_member(mid)
    await db.update_member(mid, "active", 0)
    await call.answer("Отключён. Его смены остаются в выгрузках.", show_alert=True)
    await show_members(call, db, m["project_id"])


# ---------------- Правка полей проекта / водителя ----------------

@router.callback_query(F.data.startswith("ed:"))
async def edit_ask(call: CallbackQuery, state: FSMContext):
    _, kind, obj_id, field = call.data.split(":")
    await state.set_state(Adm.edit)
    await state.update_data(kind=kind, obj_id=int(obj_id), field=field)
    label = RATE_FIELDS.get(field) or MEMBER_TEXT_FIELDS.get(field) or "Название"
    hint = " (или «-», чтобы взять из проекта)" if kind == "m" and field in RATE_FIELDS else ""
    await call.message.answer(f"{label}: новое значение?{hint}", reply_markup=cancel_kb())
    await call.answer()


@router.message(Adm.edit, F.text)
async def edit_value(message: Message, state: FSMContext, db: Database,
                     bot: Bot, config: Config):
    data = await state.get_data()
    kind, obj_id, field = data["kind"], data["obj_id"], data["field"]
    text = message.text.strip()
    if field in RATE_FIELDS:
        if kind == "m" and text == "-":
            value = None
        else:
            value = parse_number(text)
            if value is None:
                await message.answer("Нужно число")
                return
            if field == "overtime_step_min":
                value = int(value)
    else:
        value = text[:120]
    if kind == "p":
        await db.update_project(obj_id, field, value)
        view = await project_view(db, bot, obj_id)
    else:
        await db.update_member(obj_id, field, value)
        view = await member_view(db, obj_id)
    await state.clear()
    await message.answer("Сохранено.", reply_markup=await menu_for(db, config, message.from_user.id))
    await message.answer(view[0], reply_markup=view[1], disable_web_page_preview=True)


# ---------------- Проверка отчётов ----------------

@router.callback_query(F.data == "aq")
async def queue(call: CallbackQuery, db: Database):
    pending = await db.pending_shifts()
    await call.answer()
    if not pending:
        await call.message.answer("Нет отчётов на проверке 👌")
        return
    for s in pending[:20]:
        member = await db.get_member(s["member_id"])
        calc, expenses = await compute(db, s, member)
        await call.message.answer(shift_card(s, member, calc, expenses, with_driver=True),
                                  reply_markup=review_kb(s["id"], bool(expenses)))
    if len(pending) > 20:
        await call.message.answer(f"…и ещё {len(pending) - 20}. Обработайте эти и откройте снова.")


@router.callback_query(F.data.startswith("ra:"))
async def approve(call: CallbackQuery, db: Database, bot: Bot):
    shift = await db.get_shift(int(call.data[3:]))
    if not shift or shift["status"] != "submitted":
        await call.answer("Отчёт уже обработан", show_alert=True)
        return
    await db.review_shift(shift["id"], call.from_user.id, approve=True)
    member = await db.get_member(shift["member_id"])
    shift = await db.get_shift(shift["id"])
    calc, expenses = await compute(db, shift, member)
    await call.message.edit_text(shift_card(shift, member, calc, expenses, with_driver=True)
                                 + f"\n\n✅ Принял: {escape(call.from_user.full_name)}")
    await call.answer("Принято")
    try:
        await bot.send_message(member["user_id"],
                               f"✅ Смена {ru_date(shift['work_date'])} принята · {money(calc.total)}")
    except Exception:
        pass


@router.callback_query(F.data.startswith("rb:"))
async def reject_ask(call: CallbackQuery, state: FSMContext, db: Database):
    shift = await db.get_shift(int(call.data[3:]))
    if not shift or shift["status"] != "submitted":
        await call.answer("Отчёт уже обработан", show_alert=True)
        return
    await state.set_state(Adm.reject)
    await state.update_data(shift_id=shift["id"])
    await call.message.answer("Что исправить? Напишите водителю причину.", reply_markup=cancel_kb())
    await call.answer()


@router.message(Adm.reject, F.text)
async def reject(message: Message, state: FSMContext, db: Database, bot: Bot, config: Config):
    sid = (await state.get_data())["shift_id"]
    await state.clear()
    shift = await db.get_shift(sid)
    if not shift or shift["status"] != "submitted":
        await message.answer("Отчёт уже обработан.")
        return
    await db.review_shift(sid, message.from_user.id, approve=False, note=message.text[:500])
    member = await db.get_member(shift["member_id"])
    await message.answer(f"↩️ Возвращено водителю {escape(member['driver_name'])}.",
                         reply_markup=await menu_for(db, config, message.from_user.id))
    try:
        msg = await bot.send_message(member["user_id"], "↩️ Смену вернули на доработку:")
        await show_shift(msg, db, sid)
    except Exception:
        pass


@router.callback_query(F.data.startswith("rc:"))
async def receipts(call: CallbackQuery, db: Database):
    expenses = [e for e in await db.shift_expenses(int(call.data[3:])) if e["file_id"]]
    await call.answer()
    if not expenses:
        await call.message.answer("Чеков нет.")
        return
    photos = [InputMediaPhoto(media=e["file_id"],
                              caption=f"{EXPENSE_KINDS[e['kind']]}: {money(e['amount'])}")
              for e in expenses if e["file_kind"] == "photo"]
    docs = [InputMediaDocument(media=e["file_id"],
                               caption=f"{EXPENSE_KINDS[e['kind']]}: {money(e['amount'])}")
            for e in expenses if e["file_kind"] != "photo"]
    for group in (photos, docs):
        for i in range(0, len(group), 10):
            await call.message.answer_media_group(group[i:i + 10])


# ---------------- Выгрузка ----------------

def month_bounds(d: date) -> tuple[date, date]:
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def period_options(today: date) -> dict[str, tuple[str | None, str | None, str]]:
    week_start = today - timedelta(days=today.weekday())
    prev_week = week_start - timedelta(days=7)
    m1, m2 = month_bounds(today)
    pm1, pm2 = month_bounds(m1 - timedelta(days=1))
    return {
        "tw": (week_start.isoformat(), today.isoformat(), "Эта неделя"),
        "pw": (prev_week.isoformat(), (week_start - timedelta(days=1)).isoformat(), "Прошлая неделя"),
        "tm": (m1.isoformat(), m2.isoformat(), "Этот месяц"),
        "pm": (pm1.isoformat(), pm2.isoformat(), "Прошлый месяц"),
        "all": (None, None, "Весь период"),
    }


@router.callback_query(F.data.startswith("px:"))
async def export_period(call: CallbackQuery, state: FSMContext, config: Config):
    pid = int(call.data[3:])
    await state.update_data(export_pid=pid)
    kb = InlineKeyboardBuilder()
    for code, (_, _, label) in period_options(datetime.now(config.tz).date()).items():
        kb.button(text=label, callback_data=f"xp:{code}")
    kb.button(text="📅 Свои даты", callback_data="xp:custom")
    kb.button(text="← Назад", callback_data=f"pv:{pid}")
    kb.adjust(2, 2, 1, 1, 1)
    await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    await call.answer("За какой период?")


@router.callback_query(F.data.startswith("xp:"))
async def export_period_chosen(call: CallbackQuery, state: FSMContext, db: Database,
                               config: Config):
    code = call.data[3:]
    if code == "custom":
        await state.set_state(Adm.period)
        await call.message.answer("Период через дефис, например: <code>01.10-15.10</code>",
                                  reply_markup=cancel_kb())
        await call.answer()
        return
    date_from, date_to, _ = period_options(datetime.now(config.tz).date())[code]
    await state.update_data(export_from=date_from, export_to=date_to)
    await call.answer()
    await ask_contractor(call.message, state, db)


@router.message(Adm.period, F.text)
async def export_custom_period(message: Message, state: FSMContext, db: Database,
                               config: Config):
    parts = [p.strip() for p in message.text.replace("—", "-").replace("–", "-").split("-")]
    today = datetime.now(config.tz).date()
    dates = [parse_date(p, today) for p in parts] if len(parts) == 2 else []
    if len(dates) != 2 or not all(dates) or dates[0] > dates[1]:
        await message.answer("Не понял. Пример: <code>01.10-15.10</code>")
        return
    await state.set_state(None)
    await state.update_data(export_from=dates[0].isoformat(), export_to=dates[1].isoformat())
    await message.answer("Ок.", reply_markup=await menu_for(db, config, message.from_user.id))
    await ask_contractor(message, state, db)


async def ask_contractor(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    contractors = await db.project_contractors(data["export_pid"])
    await state.update_data(export_contractors=contractors)
    kb = InlineKeyboardBuilder()
    kb.button(text="📊 Все контрагенты", callback_data="xc:-1")
    for i, c in enumerate(contractors):
        kb.button(text=c or "Без контрагента", callback_data=f"xc:{i}")
    kb.adjust(1)
    await message.answer(
        f"Период: {period_label(data.get('export_from'), data.get('export_to'))}\n"
        "Чью выгрузку собрать?", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("xc:"))
async def export_run(call: CallbackQuery, state: FSMContext, db: Database, bot: Bot):
    data = await state.get_data()
    if "export_pid" not in data:
        await call.answer("Начните выгрузку заново", show_alert=True)
        return
    idx = int(call.data[3:])
    contractor = None if idx < 0 else data["export_contractors"][idx]
    await call.answer("Собираю…")
    progress = await call.message.answer("⏳ Собираю отчёт и скачиваю чеки…")

    async def fetch(file_id: str, file_kind: str) -> tuple[bytes, str]:
        f = await bot.get_file(file_id)
        buf = await bot.download_file(f.file_path)
        return buf.read(), Path(f.file_path).suffix or ".jpg"

    result = await build_export(db, data["export_pid"], data.get("export_from"),
                                data.get("export_to"), contractor, fetch)
    await progress.delete()
    if not result.shifts:
        await call.message.answer("За этот период отправленных смен нет.")
        return
    caption = (f"Смен: {result.shifts} · Итого: <b>{money(result.total)}</b>\n"
               "В выгрузке — принятые и ожидающие проверки смены (см. колонку «Статус»).")
    if result.missing_receipts:
        caption += f"\n⚠️ Расходов без чека: {num(result.missing_receipts)}"
    if result.filename.endswith(".zip"):
        caption += "\nРаспакуйте архив — ссылки на чеки в Excel откроются из папки «Чеки»."
    await call.message.answer_document(BufferedInputFile(result.data, result.filename),
                                       caption=caption)
