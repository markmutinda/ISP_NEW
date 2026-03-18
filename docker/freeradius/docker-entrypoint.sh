#!/bin/bash
# ────────────────────────────────────────────────────────────────
# FreeRADIUS Startup Script for Netily
# Waits for PostgreSQL and configures SQL connection
# ────────────────────────────────────────────────────────────────

set -e

echo "Netily FreeRADIUS Container Starting..."

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL..."
until PGPASSWORD=$DB_PASSWORD psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c '\q' 2>/dev/null; do
    echo "PostgreSQL is unavailable - sleeping"
    sleep 2
done
echo "PostgreSQL is ready!"

# Use the correct path for Ubuntu-based images
CONF_DIR="/etc/freeradius"

# ============================================================================
# CRITICAL: Use our sql.template with environment variable substitution
# This template contains the FULL SQL config with accounting queries
# ============================================================================
if [ -f /etc/raddb/sql.template ]; then
    echo "Generating SQL config from template..."
    
    # Export vars for envsubst
    export DB_HOST DB_PORT DB_USER DB_PASSWORD DB_NAME
    
    # Substitute environment variables and write to mods-available/sql
    envsubst '${DB_HOST} ${DB_PORT} ${DB_USER} ${DB_PASSWORD} ${DB_NAME}' \
        < /etc/raddb/sql.template \
        > ${CONF_DIR}/mods-available/sql
    
    echo "SQL config generated successfully!"
else
    echo "WARNING: No sql.template found! Using default config."
    
    # Fallback: Copy custom configuration if provided
    if [ -f /etc/raddb-custom/sql.conf ]; then
        echo "Copying custom SQL configuration..."
        cp /etc/raddb-custom/sql.conf ${CONF_DIR}/mods-available/sql
    fi
    
    # Update SQL configuration with environment variables
    echo "Configuring SQL connection..."
    sed -i "s/server = .*/server = \"$DB_HOST\"/" ${CONF_DIR}/mods-available/sql
    sed -i "s/port = .*/port = $DB_PORT/" ${CONF_DIR}/mods-available/sql
    sed -i "s/login = .*/login = \"$DB_USER\"/" ${CONF_DIR}/mods-available/sql
    sed -i "s/password = .*/password = \"$DB_PASSWORD\"/" ${CONF_DIR}/mods-available/sql
    sed -i "s/radius_db = .*/radius_db = \"$DB_NAME\"/" ${CONF_DIR}/mods-available/sql
    sed -i "s/driver = .*/driver = \"rlm_sql_postgresql\"/" ${CONF_DIR}/mods-available/sql
    sed -i "s/dialect = .*/dialect = \"postgresql\"/" ${CONF_DIR}/mods-available/sql
    sed -i "s/read_clients = .*/read_clients = yes/" ${CONF_DIR}/mods-available/sql
fi

# Copy custom clients configuration if provided
if [ -f /etc/raddb-custom/clients.conf ]; then
    echo "Copying custom clients configuration..."
    cp /etc/raddb-custom/clients.conf ${CONF_DIR}/clients.conf
fi

# Ensure SQL is enabled
if [ ! -L ${CONF_DIR}/mods-enabled/sql ]; then
    echo "Enabling SQL module..."
    ln -sf ${CONF_DIR}/mods-available/sql ${CONF_DIR}/mods-enabled/sql
fi

echo "Starting FreeRADIUS in debug mode..."
exec radiusd -X