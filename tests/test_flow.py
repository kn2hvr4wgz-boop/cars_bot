"""Сквозной прогон: админ создаёт проект, водитель отрабатывает смену, админ принимает и выгружает."""
from datetime import datetime
from itertools import count
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, File, Message, PhotoSize, Update, User

from bot.config import Config
from bot.handlers import setup_routers
from bot.handlers.common import (
    BTN_CURRENT, BTN_END, BTN_START, BTN_TOLL, menu_resets_state,
)

ADMIN, DRIVER = 1, 2
_ids = count(1000)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return User(id=999, is_bot=True, first_name="Bot", username="cars_test_bot")
        if name == "GetFile":
            return File(file_id=method.file_id, file_unique_id="u", file_path="photos/r.jpg")
        if name.startswith("Send") and name != "SendMediaGroup":
            return Message(message_id=next(_ids), date=datetime.now(),
                           chat=Chat(id=method.chat_id, type="private"),
                           text=getattr(method, "text", None)).as_(bot)
        if name == "SendMediaGroup":
            return []
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True):
        yield b"\xff\xd8fake-jpeg"

    async def close(self):
        pass

    def texts(self, chat_id=None):
        return [getattr(r, "text", None) or getattr(r, "caption", None) or ""
                for r in self.requests
                if chat_id is None or getattr(r, "chat_id", None) == chat_id]


@pytest.fixture
def env(db):
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["config"] = Config("42:TEST", frozenset({ADMIN}), ZoneInfo("Europe/Moscow"), ":memory:")
    dp.message.outer_middleware(menu_resets_state)
    root = setup_routers()
    dp.include_router(root)
    yield bot, dp, session, db
    # роутеры — модульные синглтоны; отцепляем, чтобы следующий тест мог подключить их снова
    for r in root.sub_routers:
        r._parent_router = None


def user(uid):
    return User(id=uid, is_bot=False, first_name=f"U{uid}", last_name="Тест")


async def send(env, uid, text=None, photo=False):
    bot, dp, _, _ = env
    msg = Message(
        message_id=next(_ids), date=datetime.now(), chat=Chat(id=uid, type="private"),
        from_user=user(uid), text=text,
        photo=[PhotoSize(file_id="PHOTO1", file_unique_id="p1", width=10, height=10)]
        if photo else None,
    )
    await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))


async def press(env, uid, data):
    bot, dp, _, _ = env
    msg = Message(message_id=next(_ids), date=datetime.now(),
                  chat=Chat(id=uid, type="private"), text="card")
    cb = CallbackQuery(id=str(next(_ids)), from_user=user(uid), chat_instance="ci",
                       data=data, message=msg)
    await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=cb))


async def test_full_cycle(env):
    _, _, session, db = env

    # Админ заводит проект
    await press(env, ADMIN, "an")
    for answer in ["Сериал К5", "6000", "12", "700", "150", "20"]:
        await send(env, ADMIN, answer)
    [project] = await db.list_projects()
    assert project["shift_rate"] == 6000 and project["km_rate"] == 20
    assert any(f"start=j_{project['invite_code']}" in t for t in session.texts(ADMIN))

    # Водитель регистрируется по ссылке
    await send(env, DRIVER, f"/start j_{project['invite_code']}")
    for answer in ["Тимофей Водитель", "Kia K5 А555АА777", "Я сам"]:
        await send(env, DRIVER, answer)
    [member] = await db.user_memberships(DRIVER)
    assert member["contractor"] == "Тимофей Водитель"
    assert any("Новый водитель" in t for t in session.texts(ADMIN))

    # Смена: старт, одометр, платка с чеком, финиш
    await send(env, DRIVER, BTN_START)
    await send(env, DRIVER, "10 000")
    await send(env, DRIVER, BTN_TOLL)
    await send(env, DRIVER, "450")
    await send(env, DRIVER, photo=True)
    await send(env, DRIVER, BTN_END)
    await send(env, DRIVER, "10180")

    shift = await db.current_shift(member["id"])
    assert shift["status"] == "draft" and shift["odo_end"] == 10180
    [expense] = await db.shift_expenses(shift["id"])
    assert expense["amount"] == 450 and expense["file_id"] == "PHOTO1"

    # Правка времени начала
    await press(env, DRIVER, f"se:{shift['id']}:start")
    await send(env, DRIVER, "7:00")
    shift = await db.get_shift(shift["id"])
    assert shift["start_at"].startswith(f"{shift['work_date']}T07:00")

    # Кнопка меню посреди ввода суммы не воспринимается как «неверное число»
    await send(env, DRIVER, BTN_TOLL)
    await send(env, DRIVER, BTN_CURRENT)
    assert "Нужна сумма" not in session.texts(DRIVER)[-1]
    assert "Смена" in session.texts(DRIVER)[-1]

    # Отправка → админу пришла карточка → принял
    await press(env, DRIVER, f"ss:{shift['id']}")
    assert (await db.get_shift(shift["id"]))["status"] == "submitted"
    assert any("Новый отчёт" in t for t in session.texts(ADMIN))

    await press(env, DRIVER, f"ra:{shift['id']}")  # водитель не может принять сам
    assert (await db.get_shift(shift["id"]))["status"] == "submitted"
    await press(env, ADMIN, f"ra:{shift['id']}")
    assert (await db.get_shift(shift["id"]))["status"] == "approved"
    assert any("принята" in t for t in session.texts(DRIVER))

    # Выгрузка: архив с Excel и чеком
    await press(env, ADMIN, f"px:{project['id']}")
    await press(env, ADMIN, "xp:all")
    await press(env, ADMIN, "xc:-1")
    [doc] = [r for r in session.requests if type(r).__name__ == "SendDocument"]
    assert doc.document.filename.endswith(".zip")
    assert "Смен: 1" in doc.caption


async def test_reject_returns_shift_to_driver(env):
    _, _, session, db = env
    pid = await db.create_project(ADMIN, "Проект", shift_rate=5000)
    await db.upsert_user(DRIVER, "Водитель", None)
    mid = await db.add_member(pid, DRIVER, "Водитель", "Авто", "ИП")
    sid = await db.create_shift(mid, "2026-10-01", start_at="2026-10-01T08:00:00+03:00",
                                end_at="2026-10-01T20:00:00+03:00", status="draft")
    await db.submit_shift(sid)

    await press(env, ADMIN, f"rb:{sid}")
    await send(env, ADMIN, "Нет чека за парковку")
    shift = await db.get_shift(sid)
    assert shift["status"] == "draft" and shift["admin_note"] == "Нет чека за парковку"
    assert any("Нет чека за парковку" in t for t in session.texts(DRIVER))
