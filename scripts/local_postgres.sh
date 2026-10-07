#!/usr/bin/env bash
# Start a throwaway Postgres 16 cluster for local development and tests.
# Prints the ADMIN_DATABASE_URL the test suite needs. Usage:
#   eval "$(scripts/local_postgres.sh start)"   # exports ADMIN_DATABASE_URL
#   scripts/local_postgres.sh stop
set -euo pipefail
PGBIN="${PGBIN:-$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)}"
DATA="${PGDATA_DIR:-/tmp/aieir-pg}"
PORT="${PGPORT:-55432}"
RUNAS=()
if [ "$(id -u)" = "0" ]; then RUNAS=(runuser -u postgres --); fi

case "${1:-start}" in
  start)
    if [ ! -f "$DATA/PG_VERSION" ]; then
      mkdir -p "$DATA"; [ "$(id -u)" = "0" ] && chown postgres "$DATA"
      "${RUNAS[@]}" "$PGBIN/initdb" -D "$DATA" -U postgres --auth=trust -E UTF8 --locale=C.UTF-8 >/dev/null
    fi
    if ! "${RUNAS[@]}" "$PGBIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1; then
      "${RUNAS[@]}" "$PGBIN/pg_ctl" -D "$DATA" -o "-p $PORT -k /tmp" -l "$DATA/log" -w start >/dev/null
    fi
    echo "export ADMIN_DATABASE_URL=postgresql://postgres@localhost:$PORT/postgres"
    ;;
  stop)
    "${RUNAS[@]}" "$PGBIN/pg_ctl" -D "$DATA" -m fast stop >/dev/null && echo stopped
    ;;
esac
