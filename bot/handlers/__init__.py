from aiogram import Router

from . import admin, common, driver


def setup_routers() -> Router:
    root = Router()
    # common первым: /start и «Отмена» работают из любого состояния
    root.include_routers(common.router, admin.router, driver.router)
    return root
