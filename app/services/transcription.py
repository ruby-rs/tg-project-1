import asyncio
import logging
from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI

from app.config import Settings, TranscriberBackend

log = logging.getLogger(__name__)

# Подсказка Whisper: повышает точность строительной лексики и единиц измерения
CONSTRUCTION_PROMPT = (
    "Отчёт прораба со стройки. Бетон, арматура, опалубка, монолит, перекрытие, "
    "фундамент, кладка, газоблок, кирпич, штукатурка, стяжка, гидроизоляция, утеплитель, "
    "кровля, фасад, отмостка, котлован, сваи, ростверк, автокран, бетононасос, миксер. "
    "Кубы, квадраты, погонные метры, тонны. КС-2, КС-3, технадзор, субподрядчик."
)


class Transcriber(Protocol):
    async def transcribe(self, path: Path) -> str: ...


class ApiWhisperTranscriber:
    """OpenAI-совместимый /v1/audio/transcriptions.

    Подходит и для self-hosted серверов (speaches, faster-whisper-server и т.п.).
    """

    def __init__(self, client: AsyncOpenAI, model: str, language: str) -> None:
        self._client = client
        self._model = model
        self._language = language

    async def transcribe(self, path: Path) -> str:
        data = await asyncio.to_thread(path.read_bytes)
        result = await self._client.audio.transcriptions.create(
            model=self._model,
            file=(path.name, data),
            language=self._language,
            prompt=CONSTRUCTION_PROMPT,
        )
        return result.text.strip()


class LocalWhisperTranscriber:
    """faster-whisper прямо в процессе воркера: данные не покидают сервер."""

    def __init__(self, model_size: str, device: str, compute_type: str, language: str) -> None:
        from faster_whisper import WhisperModel  # опциональная зависимость

        log.info("Загружаю модель Whisper %s (%s, %s)", model_size, device, compute_type)
        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._language = language
        # Модель сама использует все ядра: параллельные прогоны только мешают друг другу
        self._lock = asyncio.Lock()

    def _run(self, path: Path) -> str:
        segments, _info = self._model.transcribe(
            str(path),
            language=self._language,
            initial_prompt=CONSTRUCTION_PROMPT,
            vad_filter=True,
            beam_size=5,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, path: Path) -> str:
        async with self._lock:
            return await asyncio.to_thread(self._run, path)


def build_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber == TranscriberBackend.LOCAL:
        return LocalWhisperTranscriber(
            settings.whisper_local_model,
            settings.whisper_device,
            settings.whisper_compute_type,
            settings.whisper_language,
        )
    client = AsyncOpenAI(
        api_key=settings.effective_whisper_api_key or "not-needed",
        base_url=settings.whisper_api_base_url or settings.llm_base_url,
        timeout=300,
        max_retries=2,
    )
    return ApiWhisperTranscriber(client, settings.whisper_api_model, settings.whisper_language)
