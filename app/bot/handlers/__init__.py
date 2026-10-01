from aiogram import Router

from app.bot.handlers import consent, fallback, intake, reports, sites, start


def get_routers() -> list[Router]:
    # Порядок важен: согласие на обработку данных — первым (перехватывает всё, пока его нет),
    # команды и FSM-диалоги раньше приёма сообщений, fallback — последним
    return [
        consent.router,
        start.router,
        sites.router,
        reports.router,
        intake.router,
        fallback.router,
    ]
