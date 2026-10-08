"""Общее: /start, подключение к проекту по ссылке, регистрация водителя, меню."""
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..config import Config
from ..db import Database

router = Router(name="common")

BTN_START = "▶️ Начать смену"
BTN_END = "⏹ Закончить смену"
BTN_TOLL = "🛣 Платная дорога"
BTN_PARKING = "🅿️ Парковка"
BTN_FUEL = "⛽ Топливо"
BTN_OTHER = "🧾 Другой расход"
BTN_CURRENT = "📋 Текущая смена"
BTN_MANUAL = "📝 Смена задним числом"
BTN_HISTORY = "🗂 Мои смены"
BTN_PROJECT = "🔄 Проект"
BTN_ADMIN = "⚙️ Админка"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP = "Пропустить"


MENU_BUTTONS = {
    BTN_START, BTN_END, BTN_TOLL, BTN_PARKING, BTN_FUEL, BTN_OTHER,
    BTN_CURRENT, BTN_MANUAL, BTN_HISTORY, BTN_PROJECT, BTN_ADMIN, BTN_CANCEL,
}


async def menu_resets_state(handler, event: Message, data: dict):
    """Нажатие кнопки меню посреди любого ввода — выход из ввода, а не «неверное число»."""
    if event.text in MENU_BUTTONS and data.get("raw_state") is not None:
        await data["state"].clear()
        data["raw_state"] = None
    return await handler(event, data)


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, config: Config) -> bool:
        return event.from_user is not None and event.from_user.id in config.admin_ids


def main_menu(is_admin: bool, is_driver: bool) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    rows: list[list[KeyboardButton]] = []
    if is_driver:
        rows += [
            [KeyboardButton(text=BTN_START), KeyboardButton(text=BTN_END)],
            [KeyboardButton(text=BTN_TOLL), KeyboardButton(text=BTN_PARKING)],
            [KeyboardButton(text=BTN_FUEL), KeyboardButton(text=BTN_OTHER)],
            [KeyboardButton(text=BTN_CURRENT), KeyboardButton(text=BTN_MANUAL)],
            [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_PROJECT)],
        ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    if not rows:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def cancel_kb(*extra: str) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=t)] for t in extra]
    rows.append([KeyboardButton(text=BTN_CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, one_time_keyboard=True)


async def active_member(db: Database, user_id: int) -> dict | None:
    memberships = await db.user_memberships(user_id)
    if not memberships:
        return None
    user = await db.get_user(user_id)
    for m in memberships:
        if user and m["id"] == user["active_member_id"]:
            return m
    await db.set_active_member(user_id, memberships[0]["id"])
    return memberships[0]


async def menu_for(db: Database, config: Config, user_id: int):
    is_driver = bool(await db.user_memberships(user_id))
    return main_menu(user_id in config.admin_ids, is_driver)


async def notify_admins(bot: Bot, config: Config, text: str,
                        markup: InlineKeyboardMarkup | None = None) -> None:
    for admin_id in config.admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except Exception:  # админ мог не запускать бота — не валим сценарий водителя
            pass


def review_kb(shift_id: int, has_expenses: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Принять", callback_data=f"ra:{shift_id}")
    kb.button(text="↩️ Вернуть", callback_data=f"rb:{shift_id}")
    if has_expenses:
        kb.button(text="🧾 Чеки", callback_data=f"rc:{shift_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


class Reg(StatesGroup):
    name = State()
    car = State()
    contractor = State()


@router.message(StateFilter("*"), Command("cancel"))
@router.message(StateFilter("*"), F.text == BTN_CANCEL)
async def cancel(message: Message, state: FSMContext, db: Database, config: Config):
    await state.clear()
    await message.answer("Отменено.", reply_markup=await menu_for(db, config, message.from_user.id))


@router.message(StateFilter("*"), CommandStart())
async def start(message: Message, command: CommandObject, state: FSMContext,
                db: Database, config: Config):
    await state.clear()
    u = message.from_user
    await db.upsert_user(u.id, u.full_name, u.username)
    arg = (command.args or "").strip()
    if arg.startswith("j_"):
        await begin_join(message, state, db, arg[2:])
        return

    is_admin = u.id in config.admin_ids
    member = await active_member(db, u.id)
    lines = [f"Привет, {escape(u.first_name or '')}! Это бот для отчётов по транспорту."]
    if member:
        lines.append(f"\nТекущий проект: <b>{escape(member['project_name'])}</b>")
        lines.append(
            "\nКак пользоваться:\n"
            f"• утром — «{BTN_START}»\n"
            f"• платки, парковки, топливо — кнопками, сумма + фото чека\n"
            f"• вечером — «{BTN_END}», указать пробег и отправить на проверку\n"
            f"• забыли отметиться — «{BTN_MANUAL}»"
        )
    elif not is_admin:
        lines.append("\nЧтобы начать, попросите у администратора ссылку-приглашение в проект.")
    if is_admin:
        lines.append(f"\nВы администратор. Управление — кнопка «{BTN_ADMIN}» или /admin.")
    await message.answer("\n".join(lines), reply_markup=main_menu(is_admin, bool(member)))


@router.message(Command("join"))
async def join_cmd(message: Message, command: CommandObject, state: FSMContext, db: Database):
    u = message.from_user
    await db.upsert_user(u.id, u.full_name, u.username)
    if not command.args:
        await message.answer("Пришлите код так: <code>/join КОД</code>")
        return
    await begin_join(message, state, db, command.args.strip())


async def begin_join(message: Message, state: FSMContext, db: Database, code: str):
    project = await db.get_project_by_code(code)
    if not project:
        await message.answer("Ссылка недействительна или проект закрыт. Уточните у администратора.")
        return
    await state.set_state(Reg.name)
    await state.update_data(project_id=project["id"])
    await message.answer(
        f"Проект <b>{escape(project['name'])}</b>.\n\nКак вас записать в отчётах? (Фамилия Имя)",
        reply_markup=cancel_kb(message.from_user.full_name),
    )


@router.message(Reg.name, F.text)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(driver_name=message.text.strip()[:80])
    await state.set_state(Reg.car)
    await message.answer("Машина: марка и госномер. Например: <i>Toyota Camry А123ВС777</i>",
                         reply_markup=cancel_kb())


@router.message(Reg.car, F.text)
async def reg_car(message: Message, state: FSMContext):
    await state.update_data(car=message.text.strip()[:80])
    await state.set_state(Reg.contractor)
    await message.answer(
        "Контрагент — на кого оформляются оплаты (ИП, ООО, самозанятый).\n"
        "Если работаете сами на себя — нажмите «Я сам».",
        reply_markup=cancel_kb("Я сам"),
    )


@router.message(Reg.contractor, F.text)
async def reg_contractor(message: Message, state: FSMContext, db: Database,
                         config: Config, bot: Bot):
    data = await state.get_data()
    contractor = data["driver_name"] if message.text == "Я сам" else message.text.strip()[:120]
    member_id = await db.add_member(
        data["project_id"], message.from_user.id, data["driver_name"], data["car"], contractor
    )
    await db.set_active_member(message.from_user.id, member_id)
    await state.clear()
    member = await db.get_member(member_id)
    await message.answer(
        f"Готово! Вы в проекте <b>{escape(member['project_name'])}</b>.\n\n"
        f"Утром жмите «{BTN_START}», вечером — «{BTN_END}». Чеки — кнопками ниже.",
        reply_markup=main_menu(message.from_user.id in config.admin_ids, True),
    )
    await notify_admins(
        bot, config,
        f"🆕 Новый водитель в «{escape(member['project_name'])}»:\n"
        f"{escape(member['driver_name'])} · {escape(member['car'])} · {escape(contractor)}",
    )


@router.message(StateFilter("*"), F.text == BTN_PROJECT)
async def switch_project(message: Message, state: FSMContext, db: Database):
    await state.clear()
    memberships = await db.user_memberships(message.from_user.id)
    if len(memberships) < 2:
        current = memberships[0]["project_name"] if memberships else "—"
        await message.answer(f"Вы подключены к одному проекту: <b>{escape(current)}</b>.")
        return
    kb = InlineKeyboardBuilder()
    for m in memberships:
        kb.button(text=m["project_name"], callback_data=f"am:{m['id']}")
    kb.adjust(1)
    await message.answer("Выберите проект:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("am:"))
async def set_project(call: CallbackQuery, db: Database):
    member = await db.get_member(int(call.data[3:]))
    if not member or member["user_id"] != call.from_user.id:
        await call.answer("Недоступно", show_alert=True)
        return
    await db.set_active_member(call.from_user.id, member["id"])
    await call.message.edit_text(f"Текущий проект: <b>{escape(member['project_name'])}</b>")
    await call.answer()
