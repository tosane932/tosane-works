#!/usr/bin/env bash
# GitHub Actionsの使い捨てDockerのみ。Render・G580・実バックアップは接続しない。
set -Eeuo pipefail

network="phase3c-restore-ci-$$"
container="phase3c-pg18-ci-$$"
password_file="$(mktemp)"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  rm -f "$password_file"
}
trap cleanup EXIT
chmod 600 "$password_file"
printf '%s' 'synthetic-only-password' > "$password_file"

docker network create --driver bridge --internal "$network" >/dev/null
test "$(docker network inspect -f '{{.Internal}}' "$network")" = true
docker run -d --name "$container" --network "$network" \
  --tmpfs /var/lib/postgresql:rw,size=512m,uid=999,gid=999,mode=0700 \
  --mount "type=bind,src=$password_file,dst=/run/secrets/pg_password,readonly" \
  --env POSTGRES_USER=restore_operator --env POSTGRES_DB=isolated_restore \
  --env POSTGRES_PASSWORD_FILE=/run/secrets/pg_password postgres:18.6 >/dev/null

test "$(docker inspect -f '{{len .NetworkSettings.Networks}}' "$container")" = 1
test -z "$(docker port "$container")"
test "$(docker exec "$container" stat -c '%a %u %g' /var/lib/postgresql)" = '700 999 999'
test "$(docker inspect -f '{{len .HostConfig.Tmpfs}}' "$container")" = 1
address="$(docker inspect -f "{{ (index .NetworkSettings.Networks \"$network\").IPAddress }}" "$container")"
test -n "$address"

ready=0
for attempt in $(seq 1 30); do
  if PGPASSWORD=synthetic-only-password psql -X -A -t -v ON_ERROR_STOP=1 \
    -h "$address" -p 5432 -U restore_operator -d isolated_restore \
    -c 'SELECT 1' >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
test "$ready" = 1
data_dir="$(PGPASSWORD=synthetic-only-password psql -X -A -t -v ON_ERROR_STOP=1 \
  -h "$address" -p 5432 -U restore_operator -d isolated_restore -c 'SHOW data_directory')"
test "$data_dir" = '/var/lib/postgresql/18/docker'
test "$(PGPASSWORD=synthetic-only-password psql -X -A -t -v ON_ERROR_STOP=1 \
  -h "$address" -p 5432 -U restore_operator -d isolated_restore \
  -c "SELECT current_setting('server_version_num')::int / 10000")" = 18
echo 'PASS: PostgreSQL 18.6 on internal bridge, host-to-container IP, no published port, private tmpfs'
