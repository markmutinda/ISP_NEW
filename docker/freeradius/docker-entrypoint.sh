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

# Copy custom configuration if provided
if [ -f /etc/raddb-custom/sql.conf ]; then
    echo "Copying custom SQL configuration..."
    cp /etc/raddb-custom/sql.conf /etc/raddb/mods-available/sql
fi

if [ -f /etc/raddb-custom/clients.conf ]; then
    echo "Copying custom clients configuration..."
    cp /etc/raddb-custom/clients.conf /etc/raddb/clients.conf
fi

# Enable SQL module if not already enabled
if [ ! -L /etc/raddb/mods-enabled/sql ]; then
    echo "Enabling SQL module..."
    ln -sf /etc/raddb/mods-available/sql /etc/raddb/mods-enabled/sql
fi

# Update SQL configuration with environment variables
echo "Configuring SQL connection..."
sed -i "s/server = .*/server = \"$DB_HOST\"/" /etc/raddb/mods-available/sql
sed -i "s/port = .*/port = $DB_PORT/" /etc/raddb/mods-available/sql
sed -i "s/login = .*/login = \"$DB_USER\"/" /etc/raddb/mods-available/sql
sed -i "s/password = .*/password = \"$DB_PASSWORD\"/" /etc/raddb/mods-available/sql
sed -i "s/radius_db = .*/radius_db = \"$DB_NAME\"/" /etc/raddb/mods-available/sql

# Set driver to PostgreSQL
sed -i "s/driver = .*/driver = \"rlm_sql_postgresql\"/" /etc/raddb/mods-available/sql
sed -i "s/dialect = .*/dialect = \"postgresql\"/" /etc/raddb/mods-available/sql

# Enable read_clients from database
sed -i "s/read_clients = .*/read_clients = yes/" /etc/raddb/mods-available/sql

echo "Starting FreeRADIUS in debug mode..."
exec radiusd -X
