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
    superadmin,
)


def get_routers() -> list[Router]:
    # Порядок важен: панель главного администратора — первой (ему не нужны ни компания,
    # ни согласие), затем согласие на обработку данных (перехватывает всё, пока его нет),
    # кнопки главного меню — раньше диалогов, ждущих ввод текста,
    # команды и FSM-диалоги раньше приёма сообщений, fallback — последним
    return [
        superadmin.router,
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
