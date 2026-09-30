from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_SEVERITY_ALIASES = {
    "низкий": Severity.LOW,
    "низкая": Severity.LOW,
    "средний": Severity.MEDIUM,
    "средняя": Severity.MEDIUM,
    "высокий": Severity.HIGH,
    "высокая": Severity.HIGH,
    "критический": Severity.HIGH,
    "critical": Severity.HIGH,
}


def _parse_severity(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip().lower()
        return _SEVERITY_ALIASES.get(v, v)
    return value


def _parse_number(value: Any) -> Any:
    """LLM иногда возвращает «12,5» или «45 м3» вместо числа."""
    if isinstance(value, str):
        v = value.strip().replace(",", ".").replace(" ", "").replace(" ", "")
        if not v:
            return None
        num = ""
        for ch in v:
            if ch.isdigit() or (ch in ".-" and ch not in num):
                num += ch
            else:
                break
        try:
            return float(num)
        except ValueError:
            return None
    return value


class _Item(BaseModel):
    entry_ids: list[int] = Field(
        default_factory=list, description="Номера сообщений-источников (#N из журнала)"
    )


class WorkItem(_Item):
    description: str = Field(description="Что сделано — кратко, языком исполнительной документации")
    quantity: float | None = Field(None, description="Объём числом, только если он назван")
    unit: str | None = Field(None, description="Единица: м³, м², п.м., т, шт и т.п.")
    location: str | None = Field(None, description="Где: секция, этаж, оси, помещение")

    parse_quantity = field_validator("quantity", mode="before")(_parse_number)


class Issue(_Item):
    description: str = Field(description="Проблема, возникшая на объекте")
    severity: Severity = Severity.MEDIUM

    parse_severity = field_validator("severity", mode="before")(_parse_severity)


class MaterialRequest(_Item):
    name: str = Field(description="Материал с маркой/размером, если указаны")
    quantity: float | None = None
    unit: str | None = None
    needed_by: str | None = Field(None, description="Когда нужен, как сказал прораб")
    comment: str | None = None

    parse_quantity = field_validator("quantity", mode="before")(_parse_number)


class ScheduleRisk(_Item):
    description: str = Field(description="Что может сорвать сроки и почему")
    severity: Severity = Severity.MEDIUM
    mitigation: str | None = Field(None, description="Что нужно сделать, чтобы снять риск")

    parse_severity = field_validator("severity", mode="before")(_parse_severity)


class SiteDailyReport(BaseModel):
    summary: str = Field(description="2–3 предложения: главное за день")
    work_done: list[WorkItem] = Field(default_factory=list)
    workforce: int | None = Field(None, description="Сколько человек работало, если названо")
    equipment: list[str] = Field(default_factory=list, description="Техника на объекте")
    issues: list[Issue] = Field(default_factory=list)
    materials_needed: list[MaterialRequest] = Field(default_factory=list)
    schedule_risks: list[ScheduleRisk] = Field(default_factory=list)
    plans_for_tomorrow: list[str] = Field(default_factory=list)

    @field_validator("workforce", mode="before")
    @classmethod
    def _workforce(cls, value: Any) -> Any:
        num = _parse_number(value)
        return int(num) if isinstance(num, float) else num

    def drop_unknown_entry_ids(self, known: set[int]) -> None:
        """Модель может «придумать» номер сообщения — оставляем только реальные."""
        for items in (self.work_done, self.issues, self.materials_needed, self.schedule_risks):
            for item in items:
                item.entry_ids = [i for i in item.entry_ids if i in known]
