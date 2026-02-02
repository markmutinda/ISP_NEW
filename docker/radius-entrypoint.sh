#!/bin/bash
# ============================================================================
# NETILY ISP - FreeRADIUS Entrypoint Script
# ============================================================================
# This script configures FreeRADIUS SQL module with environment variables
# before starting the server.
# Runs as root to configure files, then drops to freerad user for RADIUS.
# ============================================================================

set -e

echo "=========================================="
echo "NETILY RADIUS Server Starting..."
echo "=========================================="

# Ensure defaults
export DB_HOST="${DB_HOST:-netily_db}"
export DB_PORT="${DB_PORT:-5432}"
export DB_USER="${DB_USER:-isp_user}"
export DB_PASS="${DB_PASS:-2202}"
export DB_PASSWORD="${DB_PASSWORD:-2202}" # Safety net
export DB_NAME="${DB_NAME:-isp_management}"
export RADIUS_SECRET="${RADIUS_SECRET:-testing123}"
export RADIUS_LOCAL_SECRET="${RADIUS_LOCAL_SECRET:-testing123}"
export RADIUS_DOCKER_SECRET="${RADIUS_DOCKER_SECRET:-docker_testing}"

echo "Configuring SQL module..."
# Process sql.template and output to mods-available/sql
# We instruct envsubst to ONLY replace specific variables to avoid breaking other config syntax
envsubst '$DB_HOST $DB_PORT $DB_USER $DB_PASSWORD $DB_NAME' < /etc/raddb/sql.template > /etc/raddb/mods-available/sql

echo "Configuring Clients..."
# Process clients.conf
envsubst '$RADIUS_SECRET $RADIUS_LOCAL_SECRET $RADIUS_DOCKER_SECRET' < /etc/raddb/clients.conf > /etc/raddb/clients.conf.tmp
mv /etc/raddb/clients.conf.tmp /etc/raddb/clients.conf

# Enable SQL module
ln -sf /etc/raddb/mods-available/sql /etc/raddb/mods-enabled/sql

# Fix permissions
chown -R freerad:freerad /etc/raddb

echo "Configuration complete."

# Test database connection
echo "Testing database connection..."
if PGPASSWORD="${DB_PASSWORD}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT 1;" 2>/dev/null; then
    echo "✓ Database connection successful"
else
    echo "✗ WARNING: Could not connect to database."
fi

echo "Starting FreeRADIUS..."
exec gosu freerad "$@"