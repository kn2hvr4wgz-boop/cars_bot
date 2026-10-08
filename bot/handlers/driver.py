"""Сценарий водителя: смена, пробег, расходы с чеками, отправка на проверку."""
from datetime import datetime, time
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..calc import EXPENSE_KINDS, money
from ..config import Config
from ..db import Database
from ..parse import combine, parse_date, parse_number, parse_time
from ..views import compute, dt, hm, ru_date, shift_card
from .common import (
    BTN_CURRENT, BTN_END, BTN_FUEL, BTN_HISTORY, BTN_MANUAL, BTN_OTHER, BTN_PARKING,
    BTN_SKIP, BTN_START, BTN_TOLL, active_member, cancel_kb, menu_for, notify_admins, review_kb,
)

router = Router(name="driver")

KIND_BY_BUTTON = {BTN_TOLL: "toll", BTN_PARKING: "parking", BTN_FUEL: "fuel", BTN_OTHER: "other"}
NO_RECEIPT = "Без чека"


class Drv(StatesGroup):
    odo_start = State()
    finish_km = State()
    exp_amount = State()
    exp_receipt = State()
    edit = State()
    man_date = State()
    man_start = State()
    man_end = State()
    man_km = State()


def now(config: Config) -> datetime:
    return datetime.now(config.tz).replace(second=0, microsecond=0)


def shift_kb(shift: dict, has_expenses: bool):
    kb = InlineKeyboardBuilder()
    sid = shift["id"]
    kb.button(text="✏️ Начало", callback_data=f"se:{sid}:start")
    if shift["end_at"]:
        kb.button(text="✏️ Конец", callback_data=f"se:{sid}:end")
    kb.button(text="✏️ Пробег", callback_data=f"se:{sid}:km")
    kb.button(text="✏️ Дата", callback_data=f"se:{sid}:date")
    if has_expenses:
        kb.button(text="🗑 Убрать расход", callback_data=f"sx:{sid}")
    kb.button(text="🗑 Удалить смену", callback_data=f"sd:{sid}")
    if shift["status"] == "draft" and shift["end_at"]:
        kb.button(text="✅ Отправить на проверку", callback_data=f"ss:{sid}")
    kb.adjust(2, 2, 1, 1, 1)
    return kb.as_markup()


async def show_shift(target: Message, db: Database, shift_id: int, edit: bool = False):
    shift = await db.get_shift(shift_id)
    member = await db.get_member(shift["member_id"])
    calc, expenses = await compute(db, shift, member)
    text = shift_card(shift, member, calc, expenses)
    markup = shift_kb(shift, bool(expenses)) if shift["status"] in ("open", "draft") else None
    if edit:
        await target.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def own_shift(db: Database, shift_id: int, user_id: int) -> dict | None:
    shift = await db.get_shift(shift_id)
    if not shift:
        return None
    member = await db.get_member(shift["member_id"])
    if member["user_id"] != user_id or shift["status"] not in ("open", "draft"):
        return None
    return shift


async def need_member(message: Message, db: Database) -> dict | None:
    member = await active_member(db, message.from_user.id)
    if not member:
        await message.answer("Вы не подключены ни к одному проекту. Попросите ссылку у администратора.")
    return member


# ---------------- Старт / финиш ----------------

@router.message(StateFilter("*"), F.text == BTN_START)
async def start_shift(message: Message, state: FSMContext, db: Database, config: Config):
    await state.clear()
    member = await need_member(message, db)
    if not member:
        return
    current = await db.current_shift(member["id"])
    if current:
        hint = ("Смена уже идёт." if current["status"] == "open"
                else "Есть неотправленная смена — отправьте её или удалите.")
        await message.answer(hint)
        await show_shift(message, db, current["id"])
        return
    t = now(config)
    sid = await db.create_shift(member["id"], t.date().isoformat(), start_at=t.isoformat())
    await state.set_state(Drv.odo_start)
    await state.update_data(shift_id=sid)
    await message.answer(
        f"▶️ Смена началась в <b>{t:%H:%M}</b> · {escape(member['project_name'])}\n"
        "Время можно поправить в карточке смены.\n\n"
        "Показания одометра на начало? (число, км)",
        reply_markup=cancel_kb(BTN_SKIP),
    )


@router.message(Drv.odo_start, F.text)
async def odo_start(message: Message, state: FSMContext, db: Database, config: Config):
    sid = (await state.get_data())["shift_id"]
    if message.text != BTN_SKIP:
        value = parse_number(message.text)
        if value is None:
            await message.answer("Нужно число, например 125430")
            return
        await db.update_shift(sid, odo_start=value)
    await state.clear()
    await message.answer("Хорошей смены! 🚗",
                         reply_markup=await menu_for(db, config, message.from_user.id))


@router.message(StateFilter("*"), F.text == BTN_END)
async def end_shift(message: Message, state: FSMContext, db: Database, config: Config):
    await state.clear()
    member = await need_member(message, db)
    if not member:
        return
    current = await db.current_shift(member["id"])
    if not current:
        await message.answer(f"Нет начатой смены. Нажмите «{BTN_START}» или «{BTN_MANUAL}».")
        return
    if current["status"] != "open":
        await message.answer("Смена уже закрыта:")
        await show_shift(message, db, current["id"])
        return
    t = now(config)
    end = combine(t.date(), t.time(), config.tz, after=dt(current["start_at"]))
    await db.update_shift(current["id"], end_at=end.isoformat(), status="draft")
    await state.set_state(Drv.finish_km)
    await state.update_data(shift_id=current["id"])
    if current["odo_start"] is not None:
        q = f"Показания одометра на конец? (на начало было {current['odo_start']:g})"
    else:
        q = "Сколько км проехали за смену?"
    await message.answer(f"⏹ Смена закончена в <b>{t:%H:%M}</b>.\n\n{q}",
                         reply_markup=cancel_kb(BTN_SKIP))


@router.message(Drv.finish_km, F.text)
async def finish_km(message: Message, state: FSMContext, db: Database, config: Config):
    sid = (await state.get_data())["shift_id"]
    shift = await db.get_shift(sid)
    if message.text != BTN_SKIP:
        value = parse_number(message.text)
        if value is None:
            await message.answer("Нужно число")
            return
        if shift["odo_start"] is not None:
            if value < shift["odo_start"]:
                await message.answer(f"Меньше, чем на начале ({shift['odo_start']:g}). Проверьте число.")
                return
            await db.update_shift(sid, odo_end=value)
        else:
            await db.update_shift(sid, km=value)
    await state.clear()
    await message.answer("Проверьте и отправьте 👇",
                         reply_markup=await menu_for(db, config, message.from_user.id))
    await show_shift(message, db, sid)


# ---------------- Расходы ----------------

@router.message(StateFilter("*"), F.text.in_(KIND_BY_BUTTON.keys()))
async def expense_start(message: Message, state: FSMContext, db: Database):
    await state.clear()
    member = await need_member(message, db)
    if not member:
        return
    current = await db.current_shift(member["id"])
    if not current:
        await message.answer(
            "Расход крепится к смене. Сначала начните смену "
            f"или внесите её через «{BTN_MANUAL}»."
        )
        return
    kind = KIND_BY_BUTTON[message.text]
    await state.set_state(Drv.exp_amount)
    await state.update_data(shift_id=current["id"], kind=kind)
    await message.answer(f"{EXPENSE_KINDS[kind]} — сумма, ₽?", reply_markup=cancel_kb())


@router.message(Drv.exp_amount, F.text)
async def expense_amount(message: Message, state: FSMContext):
    value = parse_number(message.text)
    if not value:
        await message.answer("Нужна сумма числом, например 450")
        return
    await state.update_data(amount=value)
    await state.set_state(Drv.exp_receipt)
    await message.answer("Пришлите фото чека (или файл).", reply_markup=cancel_kb(NO_RECEIPT))


@router.message(Drv.exp_receipt, F.photo | F.document | (F.text == NO_RECEIPT))
async def expense_receipt(message: Message, state: FSMContext, db: Database, config: Config):
    data = await state.get_data()
    file_id = file_kind = None
    if message.photo:
        file_id, file_kind = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, file_kind = message.document.file_id, "document"
    await db.add_expense(data["shift_id"], data["kind"], data["amount"], file_id, file_kind)
    await state.clear()
    expenses = await db.shift_expenses(data["shift_id"])
    total = sum(e["amount"] for e in expenses)
    await message.answer(
        f"✅ {EXPENSE_KINDS[data['kind']]}: {money(data['amount'])}"
        + ("" if file_id else " (без чека)")
        + f"\nРасходов по смене: {money(total)}",
        reply_markup=await menu_for(db, config, message.from_user.id),
    )


@router.message(Drv.exp_receipt)
async def expense_receipt_wrong(message: Message):
    await message.answer(f"Пришлите фото чека или нажмите «{NO_RECEIPT}».")


# ---------------- Текущая смена / история ----------------

@router.message(StateFilter("*"), F.text == BTN_CURRENT)
async def current(message: Message, state: FSMContext, db: Database):
    await state.clear()
    member = await need_member(message, db)
    if not member:
        return
    shift = await db.current_shift(member["id"])
    if not shift:
        await message.answer("Сейчас нет открытой смены.")
        return
    await show_shift(message, db, shift["id"])


@router.message(StateFilter("*"), F.text == BTN_HISTORY)
async def history(message: Message, state: FSMContext, db: Database):
    await state.clear()
    member = await need_member(message, db)
    if not member:
        return
    shifts = await db.member_shifts(member["id"], limit=15)
    if not shifts:
        await message.answer("Смен пока нет.")
        return
    lines = [f"<b>Последние смены · {escape(member['project_name'])}</b>\n"]
    for s in shifts:
        calc, _ = await compute(db, s, member)
        status = {"open": "🟢", "draft": "📝", "submitted": "⏳", "approved": "✅"}[s["status"]]
        lines.append(f"{status} {ru_date(s['work_date'])} {hm(s['start_at'])}–{hm(s['end_at'])}"
                     f" · {money(calc.total)}")
    lines.append("\n🟢 идёт · 📝 черновик · ⏳ на проверке · ✅ принята")
    await message.answer("\n".join(lines))


# ---------------- Отправка / удаление ----------------

@router.callback_query(F.data.startswith("ss:"))
async def submit(call: CallbackQuery, db: Database, config: Config, bot: Bot):
    shift = await own_shift(db, int(call.data[3:]), call.from_user.id)
    if not shift:
        await call.answer("Смена уже отправлена или недоступна", show_alert=True)
        return
    if not shift["end_at"]:
        await call.answer("Сначала закончите смену", show_alert=True)
        return
    await db.submit_shift(shift["id"])
    await show_shift(call.message, db, shift["id"], edit=True)
    await call.answer("Отправлено на проверку")

    shift = await db.get_shift(shift["id"])
    member = await db.get_member(shift["member_id"])
    calc, expenses = await compute(db, shift, member)
    await notify_admins(bot, config, "📥 Новый отчёт\n\n" + shift_card(
        shift, member, calc, expenses, with_driver=True), review_kb(shift["id"], bool(expenses)))


@router.callback_query(F.data.startswith("sd:"))
async def delete_ask(call: CallbackQuery, db: Database):
    shift = await own_shift(db, int(call.data[3:]), call.from_user.id)
    if not shift:
        await call.answer("Недоступно", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, удалить", callback_data=f"sD:{shift['id']}")
    kb.button(text="Нет", callback_data=f"sv:{shift['id']}")
    await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    await call.answer("Удалить смену вместе с расходами?")


@router.callback_query(F.data.startswith("sD:"))
async def delete_ok(call: CallbackQuery, db: Database):
    shift = await own_shift(db, int(call.data[3:]), call.from_user.id)
    if not shift:
        await call.answer("Недоступно", show_alert=True)
        return
    await db.delete_shift(shift["id"])
    await call.message.edit_text("🗑 Смена удалена.")
    await call.answer()


@router.callback_query(F.data.startswith("sv:"))
async def view(call: CallbackQuery, db: Database):
    shift = await own_shift(db, int(call.data[3:]), call.from_user.id)
    if not shift:
        await call.answer("Недоступно", show_alert=True)
        return
    await show_shift(call.message, db, shift["id"], edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("sx:"))
async def expenses_list(call: CallbackQuery, db: Database):
    shift = await own_shift(db, int(call.data[3:]), call.from_user.id)
    if not shift:
        await call.answer("Недоступно", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for e in await db.shift_expenses(shift["id"]):
        kb.button(text=f"🗑 {EXPENSE_KINDS[e['kind']]} {money(e['amount'])}",
                  callback_data=f"xd:{e['id']}")
    kb.button(text="← Назад", callback_data=f"sv:{shift['id']}")
    kb.adjust(1)
    await call.message.edit_reply_markup(reply_markup=kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("xd:"))
async def expense_delete(call: CallbackQuery, db: Database):
    expense = await db.get_expense(int(call.data[3:]))
    shift = expense and await own_shift(db, expense["shift_id"], call.from_user.id)
    if not shift:
        await call.answer("Недоступно", show_alert=True)
        return
    await db.delete_expense(expense["id"])
    await show_shift(call.message, db, shift["id"], edit=True)
    await call.answer("Расход удалён")


# ---------------- Правка полей ----------------

EDIT_PROMPTS = {
    "start": "Время начала? (например 7:30)",
    "end": "Время окончания? (например 21:15; если после полуночи — просто 01:30)",
    "km": "Пробег за смену, км? (одометр при этом сбросится)",
    "date": "Дата смены? (сегодня / вчера / 05.10)",
}


@router.callback_query(F.data.startswith("se:"))
async def edit_ask(call: CallbackQuery, state: FSMContext, db: Database):
    _, sid, field = call.data.split(":")
    shift = await own_shift(db, int(sid), call.from_user.id)
    if not shift:
        await call.answer("Недоступно", show_alert=True)
        return
    await state.set_state(Drv.edit)
    await state.update_data(shift_id=shift["id"], field=field)
    await call.message.answer(EDIT_PROMPTS[field], reply_markup=cancel_kb())
    await call.answer()


@router.message(Drv.edit, F.text)
async def edit_value(message: Message, state: FSMContext, db: Database, config: Config):
    data = await state.get_data()
    shift = await own_shift(db, data["shift_id"], message.from_user.id)
    if not shift:
        await state.clear()
        await message.answer("Смена недоступна для правки.")
        return
    error = await apply_edit(db, config, shift, data["field"], message.text)
    if error:
        await message.answer(error)
        return
    await state.clear()
    await message.answer("Исправлено.", reply_markup=await menu_for(db, config, message.from_user.id))
    await show_shift(message, db, shift["id"])


async def apply_edit(db: Database, config: Config, shift: dict, field: str, text: str) -> str | None:
    """Возвращает текст ошибки или None."""
    work_date = datetime.fromisoformat(shift["work_date"]).date()
    start, end = dt(shift["start_at"]), dt(shift["end_at"])
    if field in ("start", "end"):
        t = parse_time(text)
        if not t:
            return "Не понял время. Пример: 8:00"
        if field == "start":
            start = combine(work_date, t, config.tz)
            values = {"start_at": start.isoformat()}
            if end:
                values["end_at"] = combine(work_date, end.time(), config.tz, after=start).isoformat()
        else:
            values = {"end_at": combine(work_date, t, config.tz, after=start).isoformat()}
        await db.update_shift(shift["id"], **values)
    elif field == "km":
        value = parse_number(text)
        if value is None:
            return "Нужно число"
        await db.update_shift(shift["id"], km=value, odo_start=None, odo_end=None)
    elif field == "date":
        d = parse_date(text, now(config).date())
        if not d:
            return "Не понял дату. Пример: 05.10"
        values = {"work_date": d.isoformat()}
        if start:
            new_start = combine(d, start.time(), config.tz)
            values["start_at"] = new_start.isoformat()
            if end:
                values["end_at"] = combine(d, end.time(), config.tz, after=new_start).isoformat()
        await db.update_shift(shift["id"], **values)
    return None


# ---------------- Смена задним числом ----------------

@router.message(StateFilter("*"), F.text == BTN_MANUAL)
async def manual_start(message: Message, state: FSMContext, db: Database):
    await state.clear()
    member = await need_member(message, db)
    if not member:
        return
    current = await db.current_shift(member["id"])
    if current:
        await message.answer("Сначала завершите и отправьте текущую смену:")
        await show_shift(message, db, current["id"])
        return
    await state.set_state(Drv.man_date)
    await state.update_data(member_id=member["id"])
    await message.answer("Дата смены?", reply_markup=cancel_kb("Сегодня", "Вчера"))


@router.message(Drv.man_date, F.text)
async def manual_date(message: Message, state: FSMContext, config: Config):
    d = parse_date(message.text, now(config).date())
    if not d:
        await message.answer("Не понял дату. Пример: 05.10")
        return
    await state.update_data(work_date=d.isoformat())
    await state.set_state(Drv.man_start)
    await message.answer("Время начала? (например 7:00)", reply_markup=cancel_kb())


@router.message(Drv.man_start, F.text)
async def manual_start_time(message: Message, state: FSMContext):
    t = parse_time(message.text)
    if not t:
        await message.answer("Не понял время. Пример: 7:00")
        return
    await state.update_data(start=t.isoformat())
    await state.set_state(Drv.man_end)
    await message.answer("Время окончания? (если после полуночи — просто 01:30)")


@router.message(Drv.man_end, F.text)
async def manual_end_time(message: Message, state: FSMContext):
    t = parse_time(message.text)
    if not t:
        await message.answer("Не понял время. Пример: 20:00")
        return
    await state.update_data(end=t.isoformat())
    await state.set_state(Drv.man_km)
    await message.answer("Пробег за смену, км?", reply_markup=cancel_kb(BTN_SKIP))


@router.message(Drv.man_km, F.text)
async def manual_km(message: Message, state: FSMContext, db: Database, config: Config):
    km = None
    if message.text != BTN_SKIP:
        km = parse_number(message.text)
        if km is None:
            await message.answer("Нужно число")
            return
    data = await state.get_data()
    d = datetime.fromisoformat(data["work_date"]).date()
    start = combine(d, time.fromisoformat(data["start"]), config.tz)
    end = combine(d, time.fromisoformat(data["end"]), config.tz, after=start)
    sid = await db.create_shift(
        data["member_id"], data["work_date"], start_at=start.isoformat(),
        end_at=end.isoformat(), km=km, status="draft",
    )
    await state.clear()
    await message.answer("Смена внесена. Добавьте чеки кнопками и отправьте на проверку.",
                         reply_markup=await menu_for(db, config, message.from_user.id))
    await show_shift(message, db, sid)
