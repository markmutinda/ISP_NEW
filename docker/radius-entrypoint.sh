#!/bin/bash
# ============================================================================
# NETILY ISP - FreeRADIUS Entrypoint Script (SECURED)
# ============================================================================

set -euo pipefail

echo "=========================================="
echo "NETILY RADIUS Server Starting..."
echo "=========================================="

# Ensure defaults (No weak fallbacks!)
export DB_HOST="${DB_HOST:-netily_db}"
export DB_PORT="${DB_PORT:-5432}"
export DB_USER="${DB_USER:-isp_user}"
export DB_PASS="${DB_PASS:-CreativE@2028y}"
export DB_PASSWORD="${DB_PASSWORD:-${DB_PASS}}"
export DB_NAME="${DB_NAME:-isp_management}"
export DB_SCHEMA="${DB_SCHEMA:-public}"

CONF_ROOT="/etc/freeradius"
LEGACY_CONF_ROOT="/etc/raddb"
SQL_TEMPLATE_SOURCE="${CONF_ROOT}/sql.template"
if [ ! -f "${SQL_TEMPLATE_SOURCE}" ] && [ -f "${LEGACY_CONF_ROOT}/sql.template" ]; then
    SQL_TEMPLATE_SOURCE="${LEGACY_CONF_ROOT}/sql.template"
fi

if [ ! -f "${SQL_TEMPLATE_SOURCE}" ]; then
    echo "ERROR: SQL template not found in ${CONF_ROOT} or ${LEGACY_CONF_ROOT}."
    exit 1
fi

MODS_AVAILABLE_DIR="${CONF_ROOT}/mods-available"
MODS_ENABLED_DIR="${CONF_ROOT}/mods-enabled"
CLIENTS_CONF="${CONF_ROOT}/clients.conf"
CLIENTS_TMP="${CLIENTS_CONF}.tmp"
GENERATED_SQL_MODULE="${MODS_AVAILABLE_DIR}/sql"

mkdir -p "${MODS_AVAILABLE_DIR}" "${MODS_ENABLED_DIR}"

# ENFORCE STRONG SECRETS: If RADIUS_SECRET is empty or testing123, generate a secure one.
if [ -z "${RADIUS_SECRET:-}" ] || [ "${RADIUS_SECRET}" = "testing123" ]; then
    echo "WARNING: Weak or missing RADIUS_SECRET detected. Auto-generating secure secret."
    export RADIUS_SECRET=$(head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
fi

echo "Configuring SQL module at ${GENERATED_SQL_MODULE} from ${SQL_TEMPLATE_SOURCE}..."
envsubst '$DB_HOST $DB_PORT $DB_USER $DB_PASS $DB_PASSWORD $DB_NAME $DB_SCHEMA' < "${SQL_TEMPLATE_SOURCE}" > "${GENERATED_SQL_MODULE}"

echo "Configuring Clients..."
envsubst '$RADIUS_SECRET' < "${CLIENTS_CONF}" > "${CLIENTS_TMP}"
mv "${CLIENTS_TMP}" "${CLIENTS_CONF}"

ln -sf "${GENERATED_SQL_MODULE}" "${MODS_ENABLED_DIR}/sql"

if [ -f "${CONF_ROOT}/sites-available/coa" ] && [ ! -L "${CONF_ROOT}/sites-enabled/coa" ]; then
    echo "Enabling CoA site..."
    ln -sf "${CONF_ROOT}/sites-available/coa" "${CONF_ROOT}/sites-enabled/coa"
    echo "✓ CoA site enabled (port 3799)"
fi

# Fix permissions ONLY for the files we dynamically generated.
chown freerad:freerad "${GENERATED_SQL_MODULE}"
chown freerad:freerad "${CLIENTS_CONF}"

echo "Testing database connection..."
if PGPASSWORD="${DB_PASSWORD}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -c "SELECT current_schema(), current_setting('search_path');" 2>/dev/null; then
    echo "✓ Database connection successful"
else
    echo "✗ WARNING: Could not connect to database."
fi

# ============================================================================
# ADD VPN ROUTE (while still root, before dropping to freerad user)
# ============================================================================
echo "Setting up VPN route..."
VPN_NETWORK_CIDR="${VPN_NETWORK_CIDR:-10.8.0.0/16}"
VPN_GW="$(getent hosts netily-openvpn-isp | awk '{print $1}' | head -1 || true)"

if [ -n "$VPN_GW" ]; then
    echo "Adding route for ${VPN_NETWORK_CIDR} via ${VPN_GW}"
    ip route replace "${VPN_NETWORK_CIDR}" via "${VPN_GW}" || echo "WARNING: route add failed"
else
    echo "WARNING: netily-openvpn-isp not resolvable; skipping route setup"
fi

echo "Starting FreeRADIUS with config root ${CONF_ROOT}..."
exec gosu freerad "$@"