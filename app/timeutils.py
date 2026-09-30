from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def to_local(dt: datetime, tz_name: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz_name))


def work_date_for(dt: datetime, tz_name: str, day_start_hour: int = 0) -> date:
    """Рабочая дата сообщения в часовом поясе компании.

    Если прораб досылает отчёт ночью (до day_start_hour), сообщение
    относится к предыдущему дню.
    """
    local = to_local(dt, tz_name)
    return (local - timedelta(hours=day_start_hour)).date()


def today_for(tz_name: str, day_start_hour: int = 0) -> date:
    return work_date_for(datetime.now(ZoneInfo("UTC")), tz_name, day_start_hour)
