#!/bin/sh
# Бэкап базы данных и медиа-архива.
#
#   scripts/backup.sh [каталог]        по умолчанию /var/backups/prorab-bot
#
# Дампы БД хранятся KEEP_DAYS дней (по умолчанию 14). Медиа синхронизируются
# без удаления: файлы в архиве только добавляются.
set -eu

cd "$(dirname "$0")/.."
DEST=${1:-/var/backups/prorab-bot}
KEEP_DAYS=${KEEP_DAYS:-14}
STAMP=$(date +%F_%H%M)
mkdir -p "$DEST/media"

# База: сначала во временный файл, чтобы оборванный дамп не выглядел готовым
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' \
  > "$DEST/db_$STAMP.dump.part"
mv "$DEST/db_$STAMP.dump.part" "$DEST/db_$STAMP.dump"

# Медиа: берём тома контейнера воркера, имя тома знать не нужно
WORKER=$(docker compose ps -aq worker)
if [ -z "$WORKER" ]; then
  echo "Контейнер worker не найден — медиа не скопированы" >&2
  exit 1
fi
docker run --rm --volumes-from "$WORKER:ro" -v "$DEST/media:/backup" alpine \
  sh -c 'apk add --no-cache rsync >/dev/null && rsync -a /data/media/ /backup/'

find "$DEST" -maxdepth 1 -name 'db_*.dump' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') бэкап готов: $DEST"
