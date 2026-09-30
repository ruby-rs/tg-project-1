from aiogram import Router

from app.bot.handlers import fallback, intake, reports, sites, start


def get_routers() -> list[Router]:
    # Порядок важен: команды и FSM-диалоги раньше приёма сообщений, fallback — последним
    return [start.router, sites.router, reports.router, intake.router, fallback.router]
