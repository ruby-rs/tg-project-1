FROM python:3.12-slim

# 1 — поставить faster-whisper для локальной расшифровки (образ станет больше)
ARG LOCAL_WHISPER=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEDIA_ROOT=/data/media \
    HF_HOME=/data/models

WORKDIR /srv

# Сначала только зависимости — слой кешируется, пока не меняется pyproject.toml
COPY pyproject.toml README.md ./
RUN mkdir app && touch app/__init__.py \
    && if [ "$LOCAL_WHISPER" = "1" ]; then pip install ".[local-whisper]"; else pip install .; fi \
    && pip uninstall -y prorab-bot

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --system --uid 1000 --home /srv app \
    && mkdir -p /data/media /data/models \
    && chown -R app /data

USER app

CMD ["python", "-m", "app", "bot"]
