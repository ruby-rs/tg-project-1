import asyncio
import gc
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from openai import AsyncOpenAI

from app.config import Settings, TranscriberBackend

log = logging.getLogger(__name__)

# Подсказка Whisper: строительная лексика и единицы измерения. Связный текст, а не список
# слов: на тишине и коротких записях Whisper склонен «дописывать» список из подсказки
CONSTRUCTION_PROMPT = (
    "Отчёт прораба со стройки за день. Залили бетон в опалубку перекрытия, связали "
    "арматуру, сделали стяжку, монолит, кладку из газоблока и кирпича, штукатурку, "
    "гидроизоляцию фундамента, утеплитель, кровлю и фасад. Работали автокран, бетононасос "
    "и миксер, копали котлован, забивали сваи под ростверк. Объёмы: 12 кубов, 40 квадратов, "
    "15 погонных метров, 3 тонны. Субподрядчик, технадзор, КС-2, КС-3."
)


class Transcriber(Protocol):
    async def transcribe(self, path: Path, *, accurate: bool = False) -> str:
        """accurate=True — повторный разбор по просьбе прораба: медленнее, но точнее."""
        ...


class ApiWhisperTranscriber:
    """OpenAI-совместимый /v1/audio/transcriptions.

    Подходит и для self-hosted серверов (speaches, faster-whisper-server и т.п.).
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        language: str,
        *,
        accurate_model: str | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._language = language
        self._accurate_model = accurate_model or model

    async def transcribe(self, path: Path, *, accurate: bool = False) -> str:
        data = await asyncio.to_thread(path.read_bytes)
        result = await self._client.audio.transcriptions.create(
            model=self._accurate_model if accurate else self._model,
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

    def _options(self, accurate: bool) -> dict[str, Any]:
        options: dict[str, Any] = {
            "language": self._language,
            "initial_prompt": CONSTRUCTION_PROMPT,
            "beam_size": self._beam_size,
            # Не тянуть текст предыдущего куска: иначе одна ошибка размножается и Whisper
            # начинает повторять фразы по кругу
            "condition_on_previous_text": False,
            "vad_filter": True,
            # Паузы на стройке длинные, а тихие слова на краях фраз не должны обрезаться
            "vad_parameters": {"min_silence_duration_ms": 1000, "speech_pad_ms": 400},
        }
        if accurate:
            # Повторный разбор: шире перебор вариантов и чувствительнее к тихой речи
            options["beam_size"] = max(self._beam_size, 8)
            options["patience"] = 2.0
            options["vad_parameters"] = {
                "threshold": 0.3,
                "min_silence_duration_ms": 1500,
                "speech_pad_ms": 800,
            }
        return options

    def _run(self, path: Path, accurate: bool) -> str:
        segments, _info = self._model.transcribe(str(path), **self._options(accurate))
        return " ".join(s.text.strip() for s in segments).strip()

    async def transcribe(self, path: Path, *, accurate: bool = False) -> str:
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._factory)
            try:
                return await asyncio.to_thread(self._run, path, accurate)
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


class TwoModelTranscriber:
    """Обычные голосовые — основной моделью, повторный разбор — более сильной."""

    def __init__(self, main: Transcriber, accurate: Transcriber) -> None:
        self._main = main
        self._accurate = accurate

    async def transcribe(self, path: Path, *, accurate: bool = False) -> str:
        backend = self._accurate if accurate else self._main
        return await backend.transcribe(path, accurate=accurate)


# Сильная модель для повторов нужна редко, а памяти занимает много — выгружаем быстро
RETRY_MODEL_UNLOAD_AFTER = 120


def build_transcriber(settings: Settings) -> Transcriber:
    if settings.transcriber == TranscriberBackend.LOCAL:

        def local(model: str, unload_after: int) -> LocalWhisperTranscriber:
            return LocalWhisperTranscriber(
                model,
                settings.whisper_device,
                settings.whisper_compute_type,
                settings.whisper_language,
                beam_size=settings.whisper_beam_size,
                unload_after=unload_after,
            )

        main = local(settings.whisper_local_model, settings.whisper_unload_after)
        retry_model = settings.whisper_retry_model
        if retry_model is None or retry_model == settings.whisper_local_model:
            return main
        unload = settings.whisper_unload_after
        retry_unload = min(unload, RETRY_MODEL_UNLOAD_AFTER) if unload else RETRY_MODEL_UNLOAD_AFTER
        return TwoModelTranscriber(main, local(retry_model, retry_unload))
    client = AsyncOpenAI(
        api_key=settings.effective_whisper_api_key or "not-needed",
        base_url=settings.whisper_api_base_url or settings.llm_base_url,
        timeout=300,
        max_retries=2,
    )
    return ApiWhisperTranscriber(
        client,
        settings.whisper_api_model,
        settings.whisper_language,
        accurate_model=settings.whisper_retry_model,
    )
