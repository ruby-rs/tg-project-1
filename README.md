# Прораб-бот

Telegram-бот для строительных компаний. Прорабы в течение дня присылают фото, текст и
голосовые с объекта, бот расшифровывает голосовые, привязывает всё к объекту и дате,
хранит архив фото и по запросу собирает структурированный дневной отчёт:
что сделано и в каком объёме, проблемы, нужные материалы, риски срыва сроков.

Архитектура и дорожная карта — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Установка на сервер (Debian 13) — в [docs/DEPLOY.md](docs/DEPLOY.md).
Персональные данные и 152-ФЗ — в [docs/PERSONAL_DATA.md](docs/PERSONAL_DATA.md).

**Стек:** Python 3.12, aiogram 3, PostgreSQL 16, SQLAlchemy 2 + Alembic,
Whisper (faster-whisper или OpenAI-совместимый API), любая LLM с OpenAI-совместимым API, Docker.

## Как пользоваться

| Кто | Действие |
|-----|----------|
| Руководитель | `/start` → название компании → `/new_object` → `/invite` и отправить ссылку прорабам |
| Прораб | переходит по ссылке → выбирает объект → в течение дня шлёт фото, голосовые, текст |
| Любой | `/report` — отчёт за сегодня, `/report вчера` — за вчера, `/object` — сменить объект |

Голосовые бот расшифровывает и присылает текст ответом — прораб сразу видит, что
распознано. Фото для доказательной базы лучше отправлять «файлом»: так сохраняется
оригинал с EXIF-датой съёмки.

## Запуск в Docker

```bash
cp .env.example .env      # заполнить BOT_TOKEN, LLM_*, пароль БД
docker compose up -d --build
docker compose logs -f bot worker
```

Сервисы: `db` (PostgreSQL), `migrate` (применяет миграции и завершается), `bot`, `worker`.
Медиа-архив хранится в томе `media`, модели Whisper — в томе `models`.

## Локальная разработка

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # + ".[local-whisper]" для локальной расшифровки
cp .env.example .env
alembic upgrade head
python -m app bot                  # в одном терминале
python -m app worker               # в другом
```

## Тесты

```bash
pytest                                              # юнит-тесты
TEST_DATABASE_URL=postgresql+asyncpg://foreman:foreman@localhost:5432/foreman_test pytest
```

Сквозные тесты гоняют настоящие апдейты Telegram через диспетчер aiogram
(Telegram API, Whisper и LLM подменены) на реальной PostgreSQL: регистрация,
создание объекта, приём голосового и фото, обработка воркером, отчёт.
База `TEST_DATABASE_URL` очищается тестами — не указывайте рабочую.

## Новая миграция

```bash
alembic revision --autogenerate -m "описание"
alembic upgrade head
```
