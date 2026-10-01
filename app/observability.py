import logging

from app.config import Settings

log = logging.getLogger(__name__)


def setup_sentry(settings: Settings, component: str) -> None:
    """Отправка ошибок в Sentry/GlitchTip. Ошибки из log.exception попадают туда же."""
    if not settings.sentry_dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        server_name=component,
        # Тексты сообщений прорабов — персональные данные, в Sentry их не отправляем
        send_default_pii=False,
        include_local_variables=False,
        traces_sample_rate=0,
    )
    sentry_sdk.set_tag("component", component)
    log.info("Sentry включён (%s)", component)
