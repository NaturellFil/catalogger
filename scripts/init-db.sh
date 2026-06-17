#!/usr/bin/env bash
# One-time: create a user-owned Postgres cluster for catalogger and apply schema.
set -euo pipefail
ROOT="$HOME/.local/share/catalogger"
PGDATA="$ROOT/pgdata"
SOCK="$ROOT/sock"
source "$ROOT/.env"
PW=$(python3 -c "import urllib.parse,os;print(urllib.parse.urlparse(os.environ['CATALOGGER_DSN']).password)")
mkdir -p "$SOCK"

if [ ! -f "$PGDATA/PG_VERSION" ]; then
  echo "[init] initdb -> $PGDATA"
  initdb -D "$PGDATA" -U postgres -E UTF8 \
         --auth-local=trust --auth-host=scram-sha-256 >/dev/null
  cat >> "$PGDATA/postgresql.conf" <<CONF

# catalogger: local-only cluster
listen_addresses = '127.0.0.1'
port = 5432
unix_socket_directories = '$SOCK'
CONF
else
  echo "[init] cluster already exists, reusing"
fi

# start temporarily over the socket to provision role + db
pg_ctl -D "$PGDATA" -l "$ROOT/pg.log" -o "-k $SOCK" -w start
trap 'pg_ctl -D "$PGDATA" -o "-k $SOCK" -m fast stop || true' EXIT

psql -h "$SOCK" -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
"DO \$\$ BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='catalogger') THEN
     CREATE ROLE catalogger LOGIN PASSWORD '$PW';
   ELSE ALTER ROLE catalogger PASSWORD '$PW'; END IF;
 END \$\$;"
if [ -z "$(psql -h "$SOCK" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='catalogger'")" ]; then
  createdb -h "$SOCK" -U postgres -O catalogger catalogger
  echo "[init] created database 'catalogger'"
fi
echo "[init] role + db ready"
