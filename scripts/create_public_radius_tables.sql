-- ═══════════════════════════════════════════════════════════════════════════════
-- NETILY ISP - PUBLIC SCHEMA RADIUS TABLES FOR MULTI-TENANT AUTHENTICATION
-- ═══════════════════════════════════════════════════════════════════════════════
--
-- PURPOSE:
--   FreeRADIUS cannot query tenant-specific schemas dynamically.
--   This script creates RADIUS tables in the PUBLIC schema that FreeRADIUS
--   will query for ALL tenants. The RadiusSyncService performs "dual-write"
--   to both tenant schema (for Admin UI) and public schema (for RADIUS auth).
--
-- RUN THIS ONCE:
--   psql -U postgres -d isp_management -f create_public_radius_tables.sql
--
-- ═══════════════════════════════════════════════════════════════════════════════

SET search_path TO public;

-- ───────────────────────────────────────────────────────────────────────────────
-- 1. RADCHECK - Authentication Table (Password Verification)
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radcheck (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) NOT NULL DEFAULT ':=',
    value VARCHAR(253) NOT NULL,
    tenant_schema VARCHAR(63),  -- Track which tenant owns this user
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

COMMENT ON TABLE radcheck IS 'FreeRADIUS authentication checks - all tenants merged';
COMMENT ON COLUMN radcheck.tenant_schema IS 'Source tenant schema (e.g., tenant_yellow1)';

-- ───────────────────────────────────────────────────────────────────────────────
-- 2. RADREPLY - Reply Attributes (Bandwidth, IP, Session Settings)
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radreply (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) NOT NULL DEFAULT '=',
    value VARCHAR(253) NOT NULL,
    tenant_schema VARCHAR(63),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

COMMENT ON TABLE radreply IS 'FreeRADIUS reply attributes (rate-limits, IPs)';

-- ───────────────────────────────────────────────────────────────────────────────
-- 3. RADUSERGROUP - User to Group Mapping
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radusergroup (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    groupname VARCHAR(64) NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    tenant_schema VARCHAR(63)
);

COMMENT ON TABLE radusergroup IS 'Maps users to bandwidth/service groups';

-- ───────────────────────────────────────────────────────────────────────────────
-- 4. RADGROUPCHECK - Group-level Check Attributes
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radgroupcheck (
    id SERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) NOT NULL DEFAULT ':=',
    value VARCHAR(253) NOT NULL
);

COMMENT ON TABLE radgroupcheck IS 'Group-level authentication checks';

-- ───────────────────────────────────────────────────────────────────────────────
-- 5. RADGROUPREPLY - Group-level Reply Attributes
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radgroupreply (
    id SERIAL PRIMARY KEY,
    groupname VARCHAR(64) NOT NULL,
    attribute VARCHAR(64) NOT NULL,
    op VARCHAR(2) NOT NULL DEFAULT '=',
    value VARCHAR(253) NOT NULL
);

COMMENT ON TABLE radgroupreply IS 'Group-level reply attributes (shared bandwidth limits)';

-- ───────────────────────────────────────────────────────────────────────────────
-- 6. RADACCT - Accounting Table (Session Tracking)
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radacct (
    radacctid BIGSERIAL PRIMARY KEY,
    acctsessionid VARCHAR(64) NOT NULL,
    acctuniqueid VARCHAR(32) NOT NULL,
    username VARCHAR(64) NOT NULL,
    groupname VARCHAR(64),
    realm VARCHAR(64),
    nasipaddress INET NOT NULL,
    nasportid VARCHAR(50),
    nasporttype VARCHAR(32),
    acctstarttime TIMESTAMP WITH TIME ZONE,
    acctupdatetime TIMESTAMP WITH TIME ZONE,
    acctstoptime TIMESTAMP WITH TIME ZONE,
    acctinterval INTEGER,
    acctsessiontime BIGINT,
    acctauthentic VARCHAR(32),
    connectinfo_start VARCHAR(128),
    connectinfo_stop VARCHAR(128),
    acctinputoctets BIGINT,
    acctoutputoctets BIGINT,
    calledstationid VARCHAR(50),
    callingstationid VARCHAR(50),
    acctterminatecause VARCHAR(32),
    servicetype VARCHAR(32),
    framedprotocol VARCHAR(32),
    framedipaddress INET,
    tenant_schema VARCHAR(63)
);

COMMENT ON TABLE radacct IS 'RADIUS accounting - tracks sessions, data usage';

-- Unique constraint on session ID
CREATE UNIQUE INDEX IF NOT EXISTS idx_radacct_acctuniqueid ON radacct(acctuniqueid);

-- ───────────────────────────────────────────────────────────────────────────────
-- 7. RADPOSTAUTH - Post-Authentication Logging (Login Attempts)
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS radpostauth (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    pass VARCHAR(64),
    reply VARCHAR(32),
    authdate TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    tenant_schema VARCHAR(63)
);

COMMENT ON TABLE radpostauth IS 'Logs all authentication attempts (success/failure)';

-- ───────────────────────────────────────────────────────────────────────────────
-- 8. NAS - Network Access Servers (Router Registry)
-- ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nas (
    id SERIAL PRIMARY KEY,
    nasname VARCHAR(128) NOT NULL,
    shortname VARCHAR(32),
    type VARCHAR(30) DEFAULT 'other',
    ports INTEGER,
    secret VARCHAR(60) NOT NULL,
    server VARCHAR(64),
    community VARCHAR(50),
    description VARCHAR(200),
    tenant_schema VARCHAR(63)
);

COMMENT ON TABLE nas IS 'RADIUS NAS clients (routers allowed to authenticate)';

-- Unique constraint on NAS name (IP or hostname)
CREATE UNIQUE INDEX IF NOT EXISTS idx_nas_nasname ON nas(nasname);

-- ═══════════════════════════════════════════════════════════════════════════════
-- PERFORMANCE INDEXES
-- ═══════════════════════════════════════════════════════════════════════════════

-- radcheck indexes
CREATE INDEX IF NOT EXISTS idx_radcheck_username ON radcheck(username);
CREATE INDEX IF NOT EXISTS idx_radcheck_username_attribute ON radcheck(username, attribute);
CREATE INDEX IF NOT EXISTS idx_radcheck_tenant ON radcheck(tenant_schema);

-- radreply indexes
CREATE INDEX IF NOT EXISTS idx_radreply_username ON radreply(username);
CREATE INDEX IF NOT EXISTS idx_radreply_username_attribute ON radreply(username, attribute);
CREATE INDEX IF NOT EXISTS idx_radreply_tenant ON radreply(tenant_schema);

-- radusergroup indexes
CREATE INDEX IF NOT EXISTS idx_radusergroup_username ON radusergroup(username);
CREATE INDEX IF NOT EXISTS idx_radusergroup_groupname ON radusergroup(groupname);

-- radacct indexes (critical for performance with large datasets)
CREATE INDEX IF NOT EXISTS idx_radacct_username ON radacct(username);
CREATE INDEX IF NOT EXISTS idx_radacct_acctsessionid ON radacct(acctsessionid);
CREATE INDEX IF NOT EXISTS idx_radacct_nasipaddress ON radacct(nasipaddress);
CREATE INDEX IF NOT EXISTS idx_radacct_acctstarttime ON radacct(acctstarttime);
CREATE INDEX IF NOT EXISTS idx_radacct_acctstoptime ON radacct(acctstoptime);
CREATE INDEX IF NOT EXISTS idx_radacct_username_starttime ON radacct(username, acctstarttime);
CREATE INDEX IF NOT EXISTS idx_radacct_tenant ON radacct(tenant_schema);

-- radpostauth indexes
CREATE INDEX IF NOT EXISTS idx_radpostauth_username ON radpostauth(username);
CREATE INDEX IF NOT EXISTS idx_radpostauth_authdate ON radpostauth(authdate);

-- ═══════════════════════════════════════════════════════════════════════════════
-- AUTOMATIC TIMESTAMP UPDATE TRIGGER
-- ═══════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION update_radius_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply trigger to radcheck
DROP TRIGGER IF EXISTS trigger_radcheck_updated_at ON radcheck;
CREATE TRIGGER trigger_radcheck_updated_at
    BEFORE UPDATE ON radcheck
    FOR EACH ROW
    EXECUTE FUNCTION update_radius_updated_at();

-- Apply trigger to radreply
DROP TRIGGER IF EXISTS trigger_radreply_updated_at ON radreply;
CREATE TRIGGER trigger_radreply_updated_at
    BEFORE UPDATE ON radreply
    FOR EACH ROW
    EXECUTE FUNCTION update_radius_updated_at();

-- ═══════════════════════════════════════════════════════════════════════════════
-- VERIFICATION QUERIES
-- ═══════════════════════════════════════════════════════════════════════════════

-- Run these to verify tables were created:
-- SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name LIKE 'rad%';
-- SELECT * FROM public.radcheck LIMIT 5;
-- SELECT * FROM public.nas;

-- ═══════════════════════════════════════════════════════════════════════════════
-- END OF SCRIPT
-- ═══════════════════════════════════════════════════════════════════════════════

\echo 'Public RADIUS tables created successfully!'
\echo 'FreeRADIUS can now authenticate users from all tenants via public schema.'
