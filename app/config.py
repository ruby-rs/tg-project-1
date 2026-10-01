from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TranscriberBackend(StrEnum):
    API = "api"  # OpenAI-совместимый /audio/transcriptions
    LOCAL = "local"  # faster-whisper в контейнере воркера


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    database_url: str = "postgresql+asyncpg://foreman:foreman@localhost:5432/foreman"
    media_root: Path = Path("/data/media")
    default_timezone: str = "Europe/Moscow"
    # Сообщения, отправленные до этого часа, относятся к предыдущему рабочему дню
    work_day_start_hour: int = Field(default=0, ge=0, le=23)
    log_level: str = "INFO"
    # Хранилище состояний диалогов (FSM); пусто — в памяти, сбрасывается при рестарте
    redis_url: str | None = None
    # Ошибки в Sentry или совместимый сервис (GlitchTip); пусто — выключено
    sentry_dsn: str | None = None
    sentry_environment: str = "production"

    # --- Персональные данные (152-ФЗ) ---
    # Оператор ПДн для текста согласия, например «ООО Ромашка, ИНН 7700000000, г. Москва, ...»
    pd_operator: str | None = None
    privacy_policy_url: str | None = None
    # Куда писать для отзыва согласия и удаления данных: e-mail или @username
    support_contact: str | None = None

    # --- Расшифровка голосовых ---
    transcriber: TranscriberBackend = TranscriberBackend.API
    whisper_api_base_url: str | None = None
    whisper_api_key: SecretStr | None = None
    whisper_api_model: str = "whisper-1"
    whisper_local_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_language: str = "ru"

    # --- LLM (любой OpenAI-совместимый API) ---
    llm_base_url: str | None = None
    # Заголовок OpenAI-Project; для YandexGPT — ID каталога Yandex Cloud
    llm_project: str | None = None
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str
    # Мультимодальная модель для описания фото; пусто — фото идут в отчёт только с подписью
    llm_vision_model: str | None = None
    llm_temperature: float = 0.2
    llm_timeout: float = 120.0
    # response_format={"type": "json_object"}; выключить, если провайдер его не поддерживает
    llm_json_mode: bool = True

    # --- Воркер ---
    worker_concurrency: int = Field(default=4, ge=1)
    report_concurrency: int = Field(default=1, ge=1)  # одновременных запросов к LLM
    worker_poll_interval: float = 2.0
    worker_max_attempts: int = 5
    worker_stale_after: int = 600  # сек: задача «зависла», можно брать повторно

    @model_validator(mode="after")
    def _empty_to_none(self) -> "Settings":
        for name in (
            "whisper_api_base_url",
            "llm_base_url",
            "llm_project",
            "llm_vision_model",
            "pd_operator",
            "privacy_policy_url",
            "support_contact",
            "redis_url",
            "sentry_dsn",
        ):
            if getattr(self, name) == "":
                setattr(self, name, None)
        return self

    @property
    def effective_whisper_api_key(self) -> str:
        key = self.whisper_api_key or self.llm_api_key
        return key.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
