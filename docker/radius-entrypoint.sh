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
echo "Database Host: ${DB_HOST:-localhost}"
echo "Database Port: ${DB_PORT:-5432}"
echo "Database Name: ${DB_NAME:-isp_management}"
echo "Database User: ${DB_USER:-postgres}"
echo "Schema: ${DB_SCHEMA:-public}"
echo "=========================================="

# Set default values for environment variables
export DB_HOST="${DB_HOST:-localhost}"
export DB_PORT="${DB_PORT:-5432}"
export DB_USER="${DB_USER:-postgres}"
export DB_PASS="${DB_PASS:-}"
export DB_NAME="${DB_NAME:-isp_management}"
export DB_SCHEMA="${DB_SCHEMA:-public}"

echo "Configuring SQL module..."

# Use envsubst to process only DB_* variables, then fix the escaped $$
envsubst '$DB_HOST $DB_PORT $DB_USER $DB_PASS $DB_NAME $DB_SCHEMA' < /etc/raddb/sql.template | sed 's/\$\$/$/g' > /etc/raddb/mods-available/sql

# Create symlink to enable SQL module
ln -sf /etc/raddb/mods-available/sql /etc/raddb/mods-enabled/sql

# Fix ownership for freerad user
chown -R freerad:freerad /etc/raddb

echo "SQL module configured successfully"

# Test database connection
echo "Testing database connection..."
if PGPASSWORD="${DB_PASS}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c "SET search_path TO ${DB_SCHEMA}; SELECT COUNT(*) FROM radcheck;" 2>/dev/null; then
    echo "✓ Database connection successful"
else
    echo "✗ WARNING: Could not connect to database or read radcheck table."
    echo "  RADIUS will start but SQL auth may not work."
    echo "  Make sure PostgreSQL allows connections from Docker."
fi

# Start FreeRADIUS as freerad user
echo ""
echo "Starting FreeRADIUS as freerad user..."
exec gosu freerad "$@"
