# Установка на сервер с Debian 13

Инструкция разворачивает бота из Git-репозитория в Docker Compose:
PostgreSQL, миграции, бот и воркер с локальным Whisper. Все команды выполняются
на сервере по SSH.

## 0. Что подготовить

| Что | Где взять |
|-----|-----------|
| Сервер с Debian 13, доступ по SSH | Любой хостинг в РФ (Selectel, Timeweb Cloud, Yandex Cloud, VK Cloud и т.п.) |
| Токен бота | [@BotFather](https://t.me/BotFather) → `/newbot` |
| Доступ к LLM | Внешний API (например, YandexGPT) или Ollama на этом же сервере — см. шаг 6 |
| Доступ к репозиторию | Deploy key — см. шаг 4 |

**Ресурсы сервера:**

| Конфигурация | CPU / RAM / диск |
|--------------|------------------|
| LLM по внешнему API, Whisper `small` локально | 2 vCPU / 4 ГБ / 40 ГБ |
| То же с Whisper `medium` | 4 vCPU / 8 ГБ / 40 ГБ |
| Плюс локальная LLM 7–8B в Ollama на CPU | 8 vCPU / 16 ГБ / 80 ГБ (отчёт будет собираться минуты) |
| Локальная LLM с нормальной скоростью | GPU с 16–24 ГБ видеопамяти |

Медиа-архив растёт примерно на 50–200 МБ в день на активный объект — закладывайте
диск с запасом или планируйте переход на S3.

Входящие порты боту не нужны: он сам опрашивает Telegram (long polling).
Наружу открыт только SSH.

## Если на сервере уже работают другие проекты

Инструкция написана для чистого сервера. Если на нём уже крутятся сайты, другие
боты или панели, несколько шагов могут их сломать. Сначала осмотритесь:

```bash
docker --version && docker compose version   # Docker уже стоит? Тогда шаг 2 пропустить
docker ps                                     # какие контейнеры уже работают
ss -tlnp                                      # какие порты слушаются снаружи
cat /etc/docker/daemon.json                   # текущие настройки Docker
ufw status                                    # включён ли уже файрвол
free -h && df -h /                            # хватит ли памяти и диска
```

Что делать иначе:

| Шаг | Риск | Как поступить |
|-----|------|---------------|
| Файрвол (шаг 1) | `ufw enable` с правилом «только SSH» закроет сайты и панели | До включения разрешите все нужные порты из `ss -tlnp` (обычно `80`, `443`, порт панели) или не трогайте файрвол |
| SSH (шаг 1) | `PermitRootLogin no` отключит вход под root, через который обслуживаются другие проекты | Используйте `PermitRootLogin prohibit-password`: root входит только по ключу |
| Docker (шаг 2) | Повторная установка не нужна | Пропустите, только добавьте `deploy` в группу `docker` |
| `daemon.json` (шаги 2–3) | Команды ниже перезаписывают файл целиком, а перезапуск Docker перезапускает все контейнеры | Допишите нужные ключи в существующий файл и перезапускайте Docker в спокойное время |
| Память | Whisper и сборка образа конкурируют с другими проектами | Смотрите `free -h`; при нехватке — swap или модель Whisper `base` |

Конфликтов по портам и именам у этого проекта нет: он ничего не публикует наружу,
а контейнеры и тома получают префикс по имени каталога (`prorab-bot`).

## 1. Базовая настройка сервера

```bash
# под root
apt update && apt full-upgrade -y
apt install -y git curl ca-certificates gnupg ufw unattended-upgrades

timedatectl set-timezone Europe/Moscow

# пользователь для работы (вместо root)
adduser deploy
usermod -aG sudo deploy
```

Скопируйте свой SSH-ключ пользователю `deploy` (с локальной машины:
`ssh-copy-id deploy@IP_СЕРВЕРА`) и убедитесь, что входите по ключу. После этого
отключите вход по паролю и под root. Настройка кладётся отдельным файлом с
префиксом `00-`: в sshd действует первое найденное значение, а образы хостеров
часто содержат `50-cloud-init.conf` с `PasswordAuthentication yes`.

```bash
printf 'PasswordAuthentication no\nPermitRootLogin no\n' | sudo tee /etc/ssh/sshd_config.d/00-hardening.conf
# если root нужен для других проектов — вместо строки выше:
# printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\n' | sudo tee /etc/ssh/sshd_config.d/00-hardening.conf
sudo sshd -t && sudo systemctl restart ssh
```

> Не закрывайте текущую SSH-сессию, пока не проверите вход `ssh deploy@IP_СЕРВЕРА`
> из второго окна.

Файрвол — только SSH. **Если на сервере есть сайты или панели, сначала прочитайте
раздел выше:** эти команды закроют им доступ.

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
```

Автоматические обновления безопасности:

```bash
sudo dpkg-reconfigure -plow unattended-upgrades
```

Если оперативной памяти 4 ГБ и меньше — добавьте swap, иначе сборка образа
и Whisper могут упасть по нехватке памяти:

```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Дальше всё под пользователем `deploy`.

## 2. Установка Docker

Из официального репозитория Docker (в нём есть Debian 13 trixie):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker deploy
```

Перелогиньтесь (выйдите и снова зайдите по SSH), чтобы группа `docker` применилась,
и проверьте:

```bash
docker run --rm hello-world
docker compose version
```

> Группа `docker` даёт права, равные root. Не добавляйте в неё посторонних.

### Если Docker Hub недоступен из РФ

Если `docker run hello-world` или сборка падают с ошибкой загрузки образа
(`403`, `TLS handshake timeout`, `toomanyrequests`), подключите зеркало:

```bash
echo '{ "registry-mirrors": ["https://mirror.gcr.io"] }' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

## 3. Ограничение размера логов Docker

Без этого логи контейнеров со временем занимают весь диск:

```bash
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "registry-mirrors": ["https://mirror.gcr.io"],
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" }
}
EOF
sudo systemctl restart docker
```

Строку `registry-mirrors` уберите, если зеркало не нужно. Если файл
`/etc/docker/daemon.json` уже существовал, не перезаписывайте его, а допишите ключи
в существующий JSON. Перезапуск Docker перезапускает все контейнеры на сервере.

## 4. Клонирование репозитория

Репозиторий приватный, поэтому серверу нужен свой ключ только на чтение (deploy key):

```bash
ssh-keygen -t ed25519 -C "prorab-bot-server" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

Скопируйте выведенный ключ в GitHub: репозиторий → **Settings → Deploy keys →
Add deploy key**, галочку «Allow write access» **не** ставьте.

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF

sudo mkdir -p /opt/prorab-bot && sudo chown deploy:deploy /opt/prorab-bot
git clone git@github.com:ruby-rs/tg-project-1.git /opt/prorab-bot
cd /opt/prorab-bot
```

Пока PR с MVP не влит в `main`, переключитесь на ветку с кодом:

```bash
git checkout feature/mvp
```

## 5. Настройка `.env`

```bash
cp .env.example .env
chmod 600 .env
openssl rand -hex 24   # пароль для БД — только hex, чтобы не ломать URL подключения
nano .env
```

Обязательно заполните:

| Переменная | Значение |
|------------|----------|
| `BOT_TOKEN` | токен от @BotFather |
| `POSTGRES_PASSWORD` | сгенерированный пароль |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | см. шаг 6 |

Остальное можно оставить по умолчанию. `DATABASE_URL` в `.env` при работе
в Docker не нужен: compose собирает его сам из `POSTGRES_*`.

Расшифровка голосовых по умолчанию локальная: `TRANSCRIBER=local`, `LOCAL_WHISPER=1`,
модель `small`. При первом голосовом воркер скачает модель (около 500 МБ)
в том `models`, поэтому первая расшифровка будет долгой.

## 6. Подключение LLM

Нужен любой сервис с OpenAI-совместимым API. Два основных варианта.

### Вариант А. Внешний API (проще и быстрее)

Например, YandexGPT через OpenAI-совместимый API Yandex Cloud:

```ini
LLM_BASE_URL=https://llm.api.cloud.yandex.net/v1
LLM_API_KEY=<API-ключ сервисного аккаунта с ролью ai.languageModels.user>
LLM_MODEL=gpt://<folder_id>/yandexgpt/latest
LLM_VISION_MODEL=
```

Формат адреса модели и авторизации сверьте с актуальной документацией
провайдера. Если какой-то параметр не поддерживается, напишите — добавим
настройку. Если провайдер не поддерживает `response_format`, поставьте
`LLM_JSON_MODE=false`.

### Вариант Б. Ollama на этом же сервере (данные не покидают сервер)

Создайте файл `docker-compose.override.yml`. Docker Compose подхватывает его
автоматически, а Git не отслеживает:

```bash
cat > docker-compose.override.yml <<'EOF'
services:
  ollama:
    image: ollama/ollama:latest
    volumes:
      - ollama:/root/.ollama
    restart: unless-stopped

volumes:
  ollama:
EOF

docker compose up -d ollama
docker compose exec ollama ollama pull qwen2.5:7b
```

В `.env`:

```ini
LLM_BASE_URL=http://ollama:11434/v1
LLM_API_KEY=
LLM_MODEL=qwen2.5:7b
# На CPU отчёт собирается долго — увеличьте таймаут
LLM_TIMEOUT=600
```

Для описания фото можно дополнительно скачать мультимодальную модель
(например, `qwen2.5vl:7b`) и указать её в `LLM_VISION_MODEL`. На CPU это
сильно нагрузит сервер — включайте только при наличии GPU.

## 7. Запуск

```bash
cd /opt/prorab-bot
docker compose up -d --build
```

Первая сборка занимает 5–15 минут: качаются образы и зависимости, включая faster-whisper.

Проверка:

```bash
docker compose ps
# db — healthy, migrate — exited (0), bot и worker — running

docker compose logs migrate      # «Running upgrade -> 0001»
docker compose logs -f bot worker
# бот: «Бот @имя_бота запущен»; воркер: «Воркер запущен»
```

Контейнеры с `restart: unless-stopped` поднимаются сами после перезагрузки сервера.

## 8. Проверка в Telegram

1. Напишите боту `/start` и введите название компании — вы станете руководителем.
2. `/new_object` — создайте объект.
3. Отправьте текст, фото и голосовое. На голосовое бот ответит расшифровкой.
4. `/report` — должен прийти отчёт.
5. `/invite` — ссылка для прорабов.

Проверьте, что файлы попали в архив:

```bash
docker compose exec worker ls -R /data/media | head
```

## 9. Обновление

```bash
cd /opt/prorab-bot
git pull
docker compose up -d --build
```

Миграции применяются автоматически: сервис `migrate` запускается перед ботом
и воркером при каждом `up`. Перед обновлением сделайте бэкап (шаг 10).

Посмотреть, что изменилось перед обновлением: `git fetch && git log --oneline HEAD..@{u}`.

## 10. Бэкапы

Архив фото — доказательная база, поэтому бэкап обязателен: и БД, и медиа.

```bash
sudo mkdir -p /var/backups/prorab-bot && sudo chown deploy:deploy /var/backups/prorab-bot

cat > /opt/prorab-bot/backup.sh <<'EOF'
#!/bin/sh
set -eu
cd /opt/prorab-bot
DEST=/var/backups/prorab-bot
STAMP=$(date +%F_%H%M)

# База данных
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$DEST/db_$STAMP.dump"

# Медиа: файлы только добавляются, поэтому достаточно синхронизации
docker run --rm -v prorab-bot_media:/media:ro -v "$DEST/media":/backup alpine \
  sh -c 'apk add --no-cache rsync >/dev/null && rsync -a /media/ /backup/'

# Дампы БД храним 14 дней
find "$DEST" -maxdepth 1 -name 'db_*.dump' -mtime +14 -delete
EOF
chmod +x /opt/prorab-bot/backup.sh
```

Имя тома `prorab-bot_media` складывается из имени каталога проекта (`prorab-bot`)
и имени тома. Если клонировали в другой каталог, проверьте имя: `docker volume ls`.

Каждую ночь в 3:30:

```bash
(crontab -l 2>/dev/null; echo "30 3 * * * /opt/prorab-bot/backup.sh >> /var/backups/prorab-bot/backup.log 2>&1") | crontab -
```

> Бэкап на том же сервере не спасёт, если сервер пропадёт. Копируйте
> `/var/backups/prorab-bot` в другое место: в S3 через `rclone` или `restic`
> либо на другой сервер через `rsync`.

Восстановление БД:

```bash
docker compose stop bot worker
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
  < /var/backups/prorab-bot/db_ДАТА.dump
docker compose start bot worker
```

## 11. Полезные команды

```bash
docker compose ps                         # состояние сервисов
docker compose logs -f --tail=100 worker  # логи воркера
docker compose restart bot                # перезапуск бота
docker compose exec db psql -U foreman foreman   # консоль БД

# очередь обработки: сколько сообщений в каждом статусе
docker compose exec db psql -U foreman foreman -c \
  "select status, count(*) from entries group by status;"

# последние ошибки обработки
docker compose exec db psql -U foreman foreman -c \
  "select id, kind, attempts, error from entries where error is not null order by id desc limit 10;"
```

## 12. Частые проблемы

| Симптом | Причина и решение |
|---------|-------------------|
| `adduser` пишет `Waiting for lock to become available...` | Предыдущий `adduser` приостановлен через Ctrl+Z (в терминале было `[1]+ Stopped`) и держит блокировку `/etc/passwd`. Выполните `jobs`, затем `fg` и завершите ввод, или `kill -9 %1`. Ctrl+Z приостанавливает процесс, отменяет — Ctrl+C. |
| `TelegramConflictError: terminated by other getUpdates request` | Бот с этим токеном запущен где-то ещё (например, локально у разработчика). Остановите второй экземпляр. |
| Ошибка загрузки образов при сборке | Docker Hub недоступен — включите зеркало (шаг 2). |
| Голосовые висят в статусе `pending`, в логах воркера ошибка загрузки модели | Нет доступа к Hugging Face. Проверьте `curl -I https://huggingface.co`; при необходимости укажите в `.env` зеркало `HF_ENDPOINT=...` или используйте `TRANSCRIBER=api` со своим Whisper-сервером. |
| Воркер падает с `Killed` / код выхода 137 | Не хватает памяти: добавьте swap или возьмите модель Whisper меньше (`WHISPER_LOCAL_MODEL=base`). |
| `/report` отвечает «Не удалось сформировать отчёт» | Смотрите `docker compose logs bot`: неверные `LLM_*`, таймаут (увеличьте `LLM_TIMEOUT`) или провайдер не поддерживает `response_format` (`LLM_JSON_MODE=false`). |
| После изменения `.env` ничего не поменялось | Пересоздайте контейнеры: `docker compose up -d`. Команда `restart` не перечитывает `.env`. |
