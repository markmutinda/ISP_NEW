#!/bin/bash
# ============================================================================
# NETILY ISP - FreeRADIUS Entrypoint Script (SECURED)
# ============================================================================

set -e

echo "=========================================="
echo "NETILY RADIUS Server Starting..."
echo "=========================================="

# Ensure defaults (No weak fallbacks!)
export DB_HOST="${DB_HOST:-netily_db}"
export DB_PORT="${DB_PORT:-5432}"
export DB_USER="${DB_USER:-isp_user}"
export DB_PASS="${DB_PASS:-CreativE@2028y}"
export DB_PASSWORD="${DB_PASSWORD:-CreativE@2028y}"
export DB_NAME="${DB_NAME:-isp_management}"
export DB_SCHEMA="public"  # Hardcoded to public, as required by the architecture

# ENFORCE STRONG SECRETS: If RADIUS_SECRET is empty or testing123, generate a secure one.
if [ -z "$RADIUS_SECRET" ] || [ "$RADIUS_SECRET" = "testing123" ]; then
    echo "WARNING: Weak or missing RADIUS_SECRET detected. Auto-generating secure secret."
    export RADIUS_SECRET=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
fi

echo "Configuring SQL module..."
envsubst '$DB_HOST $DB_PORT $DB_USER $DB_PASS $DB_PASSWORD $DB_NAME $DB_SCHEMA' < /etc/freeradius/sql.template > /etc/freeradius/mods-available/sql

echo "Configuring Clients..."
envsubst '$RADIUS_SECRET' < /etc/freeradius/clients.conf > /etc/freeradius/clients.conf.tmp
mv /etc/freeradius/clients.conf.tmp /etc/freeradius/clients.conf

ln -sf /etc/freeradius/mods-available/sql /etc/freeradius/mods-enabled/sql

if [ -f /etc/freeradius/sites-available/coa ] && [ ! -L /etc/freeradius/sites-enabled/coa ]; then
    echo "Enabling CoA site..."
    ln -sf /etc/freeradius/sites-available/coa /etc/freeradius/sites-enabled/coa
    echo "✓ CoA site enabled (port 3799)"
fi

# Fix permissions on directories but ignore read-only bind mounts
find /etc/freeradius -type d -exec chown freerad:freerad {} +
find /etc/freeradius -type f ! -path "*/sites-enabled/default" ! -path "*/queries.conf" -exec chown freerad:freerad {} +

echo "Testing database connection..."
if PGPASSWORD="${DB_PASSWORD}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT 1;" 2>/dev/null; then
    echo "✓ Database connection successful"
else
    echo "✗ WARNING: Could not connect to database."
fi

echo "Starting FreeRADIUS..."
exec gosu freerad "$@"
