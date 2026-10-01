import asyncio
import gc
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

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
    """faster-whisper прямо в процессе воркера: данные не покидают сервер.

    Модель занимает ~0,5–3 ГБ памяти (в зависимости от размера), а голосовые приходят
    эпизодически. Поэтому она загружается при первом голосовом и выгружается после
    unload_after секунд простоя (0 — держать в памяти всегда).
    """

    def __init__(
        self,
        model_size: str,
        device: str,
        compute_type: str,
        language: str,
        *,
        beam_size: int = 5,
        unload_after: float = 600,
        model_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._language = language
        self._beam_size = beam_size
        self._unload_after = unload_after
        self._factory = model_factory or self._load
        self._model: Any = None
        self._unload_task: asyncio.Task[None] | None = None
        # Модель сама использует все ядра: параллельные прогоны только мешают друг другу
        self._lock = asyncio.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> Any:
        from faster_whisper import WhisperModel  # опциональная зависимость

        log.info(
            "Загружаю модель Whisper %s (%s, %s)",
            self._model_size,
            self._device,
            self._compute_type,
        )
        return WhisperModel(self._model_size, device=self._device, compute_type=self._compute_type)

    def _run(self, path: Path) -> str:
        segments, _info = self._model.transcribe(
            str(path),
            language=self._language,
            initial_prompt=CONSTRUCTION_PROMPT,
            vad_filter=True,
            beam_size=self._beam_size,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, path: Path) -> str:
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._factory)
            try:
                return await asyncio.to_thread(self._run, path)
            finally:
                self._schedule_unload()

    def _schedule_unload(self) -> None:
        if self._unload_after <= 0:
            return
        if self._unload_task is not None:
            self._unload_task.cancel()
        self._unload_task = asyncio.get_running_loop().create_task(self._unload_later())

    async def _unload_later(self) -> None:
        await asyncio.sleep(self._unload_after)
        async with self._lock:
            if self._model is not None:
                self._model = None
                gc.collect()
                log.info("Модель Whisper выгружена из памяти до следующего голосового")


def build_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber == TranscriberBackend.LOCAL:
        return LocalWhisperTranscriber(
            settings.whisper_local_model,
            settings.whisper_device,
            settings.whisper_compute_type,
            settings.whisper_language,
            beam_size=settings.whisper_beam_size,
            unload_after=settings.whisper_unload_after,
        )
    client = AsyncOpenAI(
        api_key=settings.effective_whisper_api_key or "not-needed",
        base_url=settings.whisper_api_base_url or settings.llm_base_url,
        timeout=300,
        max_retries=2,
    )
    return ApiWhisperTranscriber(client, settings.whisper_api_model, settings.whisper_language)
