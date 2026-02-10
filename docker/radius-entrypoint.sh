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
# NOTE: Uses /etc/freeradius (Debian/Ubuntu) not /etc/raddb (RedHat/CentOS)
# We instruct envsubst to ONLY replace specific variables to avoid breaking other config syntax
envsubst '$DB_HOST $DB_PORT $DB_USER $DB_PASSWORD $DB_NAME' < /etc/freeradius/sql.template > /etc/freeradius/mods-available/sql

echo "Configuring Clients..."
# Process clients.conf
envsubst '$RADIUS_SECRET $RADIUS_LOCAL_SECRET $RADIUS_DOCKER_SECRET' < /etc/freeradius/clients.conf > /etc/freeradius/clients.conf.tmp
mv /etc/freeradius/clients.conf.tmp /etc/freeradius/clients.conf

# Enable SQL module
ln -sf /etc/freeradius/mods-available/sql /etc/freeradius/mods-enabled/sql

# Enable CoA (Change of Authorization) site for disconnect/bandwidth-change support
if [ -f /etc/freeradius/sites-available/coa ] && [ ! -L /etc/freeradius/sites-enabled/coa ]; then
    echo "Enabling CoA site..."
    ln -sf /etc/freeradius/sites-available/coa /etc/freeradius/sites-enabled/coa
    echo "✓ CoA site enabled (port 3799)"
else
    echo "✓ CoA site already enabled or not available"
fi

# Fix permissions
chown -R freerad:freerad /etc/freeradius

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