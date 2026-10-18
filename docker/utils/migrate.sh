#!/bin/sh
# Lenny database migration runner
# Checks and applies pending Alembic migrations.

set -e

echo "[lenny] Checking database migrations..."

# Capture current revision before upgrade
CURRENT=$(alembic current 2>/dev/null | grep -oE '[a-f0-9]+' | head -1 || echo "none")
HEAD=$(alembic heads 2>/dev/null | grep -oE '[a-f0-9]+' | head -1 || echo "unknown")

if [ "$CURRENT" = "$HEAD" ] && [ "$CURRENT" != "none" ]; then
    echo "[lenny] Database is up to date (revision: $CURRENT). No migrations to apply."
else
    if [ "$CURRENT" = "none" ]; then
        echo "[lenny] No migration history found. Applying all migrations..."
    else
        echo "[lenny] Database at revision $CURRENT, head is $HEAD. Applying pending migrations..."
    fi

    alembic upgrade head

    NEW=$(alembic current 2>/dev/null | grep -oE '[a-f0-9]+' | head -1 || echo "unknown")
    echo "[lenny] Migrations complete. Database now at revision: $NEW"
fi

# The librarians migration has moved the admin credentials into the database;
# drop them from auth.env so the secret no longer sits on disk. The upgrade has
# committed by now (set -e), and the line is only present until it first runs.
if [ -w /app/auth.env ] && grep -qE '^ADMIN_(USERNAME|PASSWORD)=' /app/auth.env \
   && python -c "import sys; from lenny.core.db import session; from sqlalchemy import text; sys.exit(0 if session.execute(text('select 1 from librarians where is_super limit 1')).first() else 1)" 2>/dev/null; then
    # Rewrite in place: auth.env is a bind-mounted file, which `sed -i` cannot replace.
    TMP=$(mktemp) && grep -vE '^ADMIN_(USERNAME|PASSWORD)=' /app/auth.env > "$TMP"; cat "$TMP" > /app/auth.env; rm -f "$TMP"
    echo "[lenny] Admin credentials moved from auth.env into the database."
fi
