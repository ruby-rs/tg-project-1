import base64
import logging
from typing import Any, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> str:
    """Достаёт JSON-объект из ответа, даже если модель обернула его в ```json."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"В ответе модели нет JSON: {text[:200]!r}")
    return text[start : end + 1]


class LLMClient:
    """Клиент к любому OpenAI-совместимому API: vLLM, Ollama, YandexGPT, прокси и т.д."""

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        vision_model: str | None = None,
        temperature: float = 0.2,
        json_mode: bool = True,
    ) -> None:
        self._client = client
        self.model = model
        self.vision_model = vision_model
        self._temperature = temperature
        self._json_mode = json_mode

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMClient":
        client = AsyncOpenAI(
            api_key=settings.llm_api_key.get_secret_value() or "not-needed",
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout,
            max_retries=2,
        )
        return cls(
            client,
            model=settings.llm_model,
            vision_model=settings.llm_vision_model,
            temperature=settings.llm_temperature,
            json_mode=settings.llm_json_mode,
        )

    async def complete_json(
        self, system: str, user: str, schema: type[T], *, repair_attempts: int = 1
    ) -> T:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        extra: dict[str, Any] = {}
        if self._json_mode:
            extra["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for _ in range(repair_attempts + 1):
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self._temperature,
                **extra,
            )
            content = response.choices[0].message.content or ""
            try:
                return schema.model_validate_json(extract_json(content))
            except (ValidationError, LLMError) as exc:
                last_error = exc
                log.warning("LLM вернула невалидный JSON, просим исправить: %s", exc)
                messages += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "Ответ не соответствует схеме. Ошибки:\n"
                        f"{exc}\nВерни только исправленный JSON-объект.",
                    },
                ]
        raise LLMError(f"Не удалось получить валидный JSON: {last_error}")

    async def describe_image(
        self, image: bytes, mime_type: str, system: str, hint: str | None = None
    ) -> str:
        if not self.vision_model:
            raise LLMError("LLM_VISION_MODEL не задана")
        data_url = f"data:{mime_type};base64,{base64.b64encode(image).decode()}"
        text = "Опиши фото." + (f" Подпись прораба: «{hint}»" if hint else "")
        response = await self._client.chat.completions.create(
            model=self.vision_model,
            temperature=self._temperature,
            messages=[
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        return (response.choices[0].message.content or "").strip()
