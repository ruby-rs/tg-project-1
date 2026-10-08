from aiogram import Router

from app.bot.handlers import (
    admin,
    archive,
    consent,
    fallback,
    feedback,
    intake,
    menu,
    reports,
    settings,
    sites,
    start,
)


def get_routers() -> list[Router]:
    # Порядок важен: согласие на обработку данных — первым (перехватывает всё, пока его нет),
    # кнопки главного меню — раньше диалогов, ждущих ввод текста,
    # команды и FSM-диалоги раньше приёма сообщений, fallback — последним
    return [
        consent.router,
        menu.router,
        start.router,
        admin.router,
        settings.router,
        sites.router,
        reports.router,
        archive.router,
        feedback.router,
        intake.router,
        fallback.router,
    ]
