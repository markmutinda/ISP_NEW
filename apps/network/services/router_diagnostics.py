"""
Router Diagnostics Engine
==========================
Compares a router's LIVE RouterOS config against the Netily provisioning
baseline (mikrotik_script_generator.py) and can auto-fix drift.

To add a new check in the future: write a function, decorate it with
@register_check(...), done. No other file needs to change.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Any
from urllib.parse import urlparse

from librouteros.query import Key

logger = logging.getLogger(__name__)


@dataclass
class DiagnosticContext:
    router: Any
    api: Any                      # connected MikrotikAPI instance
    live: dict = field(default_factory=dict)   # cached live router state


@dataclass
class DiagnosticCheck:
    id: str
    label: str
    category: str
    severity: str                 # 'critical' | 'warning'
    check_fn: Callable[[DiagnosticContext], bool]
    fix_fn: Optional[Callable[[DiagnosticContext], None]] = None


CHECKS: list[DiagnosticCheck] = []


def register_check(id: str, label: str, category: str, severity: str = "warning", fix_fn=None):
    def decorator(fn):
        CHECKS.append(DiagnosticCheck(
            id=id, label=label, category=category,
            severity=severity, check_fn=fn, fix_fn=fix_fn,
        ))
        return fn
    return decorator


# ────────────────────────────────────────────────────────────────
# LIVE STATE GATHERING — one connection, cached, reused by all checks
# ────────────────────────────────────────────────────────────────

def gather_live_state(api) -> dict:
    """Single round of reads. Every check works off this cached dict —
    no check opens its own connection, keeping diagnosis fast."""
    def safe(path):
        try:
            return list(api._execute(path))
        except Exception as e:
            logger.warning(f"[DIAGNOSE] read failed for {path}: {e}")
            return []

    return {
        'walled_garden': safe('/ip/hotspot/walled-garden'),
        'walled_garden_ip': safe('/ip/hotspot/walled-garden/ip'),
        'address_lists': safe('/ip/firewall/address-list'),
        'bridges': safe('/interface/bridge'),
        'bridge_ports': safe('/interface/bridge/port'),
        'wireguard': safe('/interface/wireguard'),
        'radius': safe('/radius'),
        'hotspot_profiles': safe('/ip/hotspot/profile'),
        'hotspot_servers': safe('/ip/hotspot'),
        'firewall_mangle': safe('/ip/firewall/mangle'),
        'firewall_nat': safe('/ip/firewall/nat'),
        'ip_services': safe('/ip/service'),
        'users': safe('/user'),
        'dhcp_servers': safe('/ip/dhcp-server'),
        'files': safe('/file'),
    }


def _own_domains(router) -> list[str]:
    from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator
    gen = MikrotikScriptGenerator(router)
    tenant_domain = urlparse(gen.get_tenant_portal_url()).netloc
    api_domain = urlparse(gen.api_url).netloc
    portal_domain = gen.portal_url.split('://')[-1]
    domains = []
    for d in (tenant_domain, api_domain, portal_domain):
        if d and d not in domains:
            domains.append(d)
    return domains


# ────────────────────────────────────────────────────────────────
# 🔥 FIX: _read_login_html_contents using librouteros's real query API
# ────────────────────────────────────────────────────────────────

def _read_login_html_contents(api, live_files) -> str:
    """
    Reads login.html contents from the router (small text file, safe to pull inline).
    
    Uses librouteros's real select() API via Key('name').select(name_k, contents_k)
    instead of the broken _execute(get=...) which expects a different signature.
    """
    try:
        # Find the netily-profile to get its html-directory
        profiles = api._execute('/ip/hotspot/profile')
        prof = next((p for p in profiles if p.get('name') == 'netily-profile'), None)
        html_dir = (prof.get('html-directory') or 'hotspot') if prof else 'hotspot'
        target = f"{html_dir}/login.html"

        # Check if we already have contents from the cached file list
        f = next((x for x in live_files if x.get('name') == target), None)
        if f and 'contents' in f:
            return f.get('contents') or ''

        # Contents weren't in the default listing — query for them explicitly
        # via librouteros' real select() API (NOT api._execute(get=...), which
        # is broken for anything but plain listings).
        name_k, contents_k = Key('name'), Key('contents')
        rows = list(api.api.path('file').select(name_k, contents_k))
        match = next((r for r in rows if r.get('name') == target), None)
        return (match.get('contents') or '') if match else ''
    except Exception as e:
        logger.warning(f"[DIAGNOSE] login.html read failed: {e}")
        return ''


# ════════════════════════════════════════════════════════════════
# CHECKS — add new ones below, each is self-contained
# ════════════════════════════════════════════════════════════════

# ── CRITICAL: the HTTP-Injector / SNI-spoof walled-garden bypass ──
def _check_walled_garden_hardened(ctx: DiagnosticContext) -> bool:
    wg_ip = ctx.live['walled_garden_ip']
    has_80 = any(r.get('comment') == 'Netily-Portal-80' for r in wg_ip)
    has_443 = any(r.get('comment') == 'Netily-Portal-443' for r in wg_ip)
    # Old exploitable hostname rules must be GONE too
    old_hostname_rules = any(
        r.get('comment') in ('Netily-Tenant-Portal', 'Netily-Backend-Core')
        for r in ctx.live['walled_garden']
    )
    return has_80 and has_443 and not old_hostname_rules


def _fix_walled_garden_hardened(ctx: DiagnosticContext):
    router, api = ctx.router, ctx.api

    # 1. Remove the old exploitable hostname rules
    for rule in ctx.live['walled_garden']:
        if rule.get('comment') in ('Netily-Tenant-Portal', 'Netily-Backend-Core'):
            api._execute('/ip/hotspot/walled-garden', remove={'.id': rule['.id']})

    # 2. Wipe any partial/stale pinned entries (idempotent re-apply)
    for entry in ctx.live['address_lists']:
        if entry.get('list') == 'netily-portal-ips':
            api._execute('/ip/firewall/address-list', remove={'.id': entry['.id']})
    for rule in ctx.live['walled_garden_ip']:
        if rule.get('comment') in ('Netily-Portal-80', 'Netily-Portal-443'):
            api._execute('/ip/hotspot/walled-garden/ip', remove={'.id': rule['.id']})

    # 3. Re-add hardened address-list based rules
    for domain in _own_domains(router):
        api._execute('/ip/firewall/address-list', add={
            'list': 'netily-portal-ips', 'address': domain,
            'comment': 'Netily-Portal-Domain',
        })
    api._execute('/ip/hotspot/walled-garden/ip', add={
        'action': 'accept', 'protocol': 'tcp', 'dst-port': '80',
        'dst-address-list': 'netily-portal-ips', 'comment': 'Netily-Portal-80',
    })
    api._execute('/ip/hotspot/walled-garden/ip', add={
        'action': 'accept', 'protocol': 'tcp', 'dst-port': '443',
        'dst-address-list': 'netily-portal-ips', 'comment': 'Netily-Portal-443',
    })


register_check(
    id='walled_garden_hardened',
    label='HotSpot bypass uses exact destination IP lists',
    category='Security',
    severity='critical',
    fix_fn=_fix_walled_garden_hardened,
)(_check_walled_garden_hardened)


# ── Force-DNS through router (blocks the DNS-tunnel bypass variant) ──
def _check_force_dns(ctx: DiagnosticContext) -> bool:
    comments = {r.get('comment') for r in ctx.live['firewall_nat']}
    return 'Netily-Force-DNS' in comments and 'Netily-Force-DNS-TCP' in comments


def _fix_force_dns(ctx: DiagnosticContext):
    api = ctx.api
    existing = {r.get('comment') for r in ctx.live['firewall_nat']}
    if 'Netily-Force-DNS' not in existing:
        api._execute('/ip/firewall/nat', add={
            'chain': 'dstnat', 'action': 'redirect', 'to-ports': '53',
            'protocol': 'udp', 'dst-port': '53',
            'in-interface': 'netily-bridge', 'comment': 'Netily-Force-DNS',
        })
    if 'Netily-Force-DNS-TCP' not in existing:
        api._execute('/ip/firewall/nat', add={
            'chain': 'dstnat', 'action': 'redirect', 'to-ports': '53',
            'protocol': 'tcp', 'dst-port': '53',
            'in-interface': 'netily-bridge', 'comment': 'Netily-Force-DNS-TCP',
        })


register_check(
    id='force_dns_enforced',
    label='Hotspot DNS is forced through the router (anti DNS-tunnel)',
    category='Security',
    severity='critical',
    fix_fn=_fix_force_dns,
)(_check_force_dns)


# ── Bridge exists ──
def _check_bridge_exists(ctx: DiagnosticContext) -> bool:
    return any(b.get('name') == 'netily-bridge' for b in ctx.live['bridges'])


register_check(
    id='bridge_exists',
    label='Netily bridge exists',
    category='Network',
    severity='critical',
)(_check_bridge_exists)


# ── WireGuard interface present ──
def _check_wireguard_present(ctx: DiagnosticContext) -> bool:
    return any(w.get('name') == 'Netily-VPN' for w in ctx.live['wireguard'])


register_check(
    id='wireguard_present',
    label='WireGuard VPN interface is present',
    category='VPN',
    severity='critical',
)(_check_wireguard_present)


# ── RADIUS configured ──
def _check_radius_configured(ctx: DiagnosticContext) -> bool:
    return any('Netily' in str(r.get('comment', '')) for r in ctx.live['radius'])


register_check(
    id='radius_configured',
    label='Netily RADIUS server is configured',
    category='RADIUS',
    severity='critical',
)(_check_radius_configured)


# ── Hotspot profile uses RADIUS accounting ──
def _check_hotspot_radius_accounting(ctx: DiagnosticContext) -> bool:
    def is_yes(v) -> bool:
        """RouterOS returns boolean-ish fields as 'yes'/'no', not Python True/False."""
        return str(v).strip().lower() in ('yes', 'true', '1')

    for p in ctx.live['hotspot_profiles']:
        if p.get('name') == 'netily-profile':
            return is_yes(p.get('use-radius')) and is_yes(p.get('radius-accounting'))
    return False


def _fix_hotspot_radius_accounting(ctx: DiagnosticContext):
    """Apply fix to enable RADIUS accounting on the hotspot profile."""
    api = ctx.api
    for p in ctx.live['hotspot_profiles']:
        if p.get('name') == 'netily-profile':
            api._execute('/ip/hotspot/profile', update={
                '.id': p['.id'],
                'use-radius': 'yes',
                'radius-accounting': 'yes',
                'radius-interim-update': '00:03:00',
            })
            logger.info(f"[DIAGNOSE] Fixed hotspot profile '{p.get('name')}' RADIUS accounting settings")
            return
    
    # Profile doesn't exist at all — nothing to patch, this is a deeper
    # provisioning gap that needs a reprovision, not a diagnose fix.
    logger.warning("[DIAGNOSE] Hotspot profile 'netily-profile' not found, cannot fix RADIUS accounting")


register_check(
    id='hotspot_radius_accounting',
    label='Hotspot profile uses RADIUS accounting',
    category='Billing',
    severity='critical',
    fix_fn=_fix_hotspot_radius_accounting,
)(_check_hotspot_radius_accounting)


# ── Anti-sharing TTL rule exists ──
def _check_anti_sharing(ctx: DiagnosticContext) -> bool:
    return any(m.get('comment') == 'Netily-AntiShare-Enforce' for m in ctx.live['firewall_mangle'])


register_check(
    id='anti_sharing_ttl',
    label='Hotspot anti-sharing TTL rule exists',
    category='Security',
    severity='warning',
)(_check_anti_sharing)


# ── NAT masquerade present ──
def _check_nat_masquerade(ctx: DiagnosticContext) -> bool:
    return any(n.get('comment') == 'Netily-Masquerade' for n in ctx.live['firewall_nat'])


register_check(
    id='nat_masquerade',
    label='NAT masquerade is configured',
    category='Network',
    severity='critical',
)(_check_nat_masquerade)


# ── API service restricted to VPN/local only ──
def _check_api_service_restricted(ctx: DiagnosticContext) -> bool:
    for svc in ctx.live['ip_services']:
        if svc.get('name') == 'api':
            addr = str(svc.get('address', ''))
            return addr != '' and '0.0.0.0/0' not in addr
    return False


register_check(
    id='api_service_restricted',
    label='RouterOS API service is restricted to VPN/local',
    category='Security',
    severity='critical',
)(_check_api_service_restricted)


# ── API user exists (not default admin) ──
def _check_api_user_exists(ctx: DiagnosticContext) -> bool:
    return any(u.get('comment') == 'Netily Cloud API' for u in ctx.live['users'])


register_check(
    id='api_user_exists',
    label='Netily management API user exists',
    category='Security',
    severity='warning',
)(_check_api_user_exists)


# ────────────────────────────────────────────────────────────────
# 🔥 FIXED: Login HTML version check — uses DB-stamped version
# instead of unreliable file-content reading via RouterOS API
# ────────────────────────────────────────────────────────────────

def _check_login_html_current(ctx: DiagnosticContext) -> bool:
    """
    Checks if the router has the current login.html version by comparing
    the DB-stamped version against the current LOGIN_HTML_VERSION.
    
    This is faster and more reliable than reading file contents over
    the RouterOS API, which is broken on many v7 builds (contents field
    returns empty for /file).
    """
    from apps.network.services.mikrotik_script_generator import LOGIN_HTML_VERSION
    return ctx.router.last_login_html_version == LOGIN_HTML_VERSION


def _fix_login_html_current(ctx: DiagnosticContext):
    """
    Re-downloads login.html + status.html via RouterOS /tool fetch.

    /tool fetch does NOT overwrite an existing file — it silently creates
    'login.html1' if one already exists and the hotspot server keeps serving
    the stale copy. So we delete the existing file first, then fetch.

    Verification is done by polling the DB flag that the provisioning
    endpoint stamps the instant the router's GET request lands — reading
    file contents back over the RouterOS API is unreliable and was the
    root cause of "fix applied but verification failed".
    """
    from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator, LOGIN_HTML_VERSION

    router, api = ctx.router, ctx.api
    gen = MikrotikScriptGenerator(router)

    login_url = f"{gen.active_url}/api/v1/network/provision/{router.auth_key}/hotspot/login.html"
    status_url = f"{gen.active_url}/api/v1/network/provision/{router.auth_key}/hotspot/status.html"

    prof = next((p for p in ctx.live['hotspot_profiles'] if p.get('name') == 'netily-profile'), None)
    html_dir = (prof.get('html-directory') or 'hotspot') if prof else 'hotspot'

    for url, filename in ((login_url, 'login.html'), (status_url, 'status.html')):
        dst = f"{html_dir}/{filename}"

        # Delete existing file first so /tool fetch actually overwrites it
        try:
            for f in api._execute('/file'):
                if f.get('name') == dst:
                    api._execute('/file', remove={'.id': f['.id']})
                    logger.info(f"[DIAGNOSE FIX] Removed stale {dst} from router {router.id}")
        except Exception as e:
            logger.warning(f"[DIAGNOSE FIX] Could not clear stale {dst}: {e}")

        ok = api.fetch_url(url, dst)
        if not ok:
            logger.warning(f"[DIAGNOSE FIX] fetch({filename}) failed to trigger")

    # Poll for the DB stamp written by ProvisionHotspotHTMLView when the
    # router's GET request for login.html actually lands. The router fetches
    # over its own WAN (not the VPN tunnel), so DNS + TLS + download can
    # take longer than a few seconds on slower connections — give it more room.
    for attempt in range(20):  # ~30s total (20 x 1.5s)
        time.sleep(1.5)
        router.refresh_from_db(fields=['last_login_html_version'])
        if router.last_login_html_version == LOGIN_HTML_VERSION:
            logger.info(f"[DIAGNOSE FIX] login.html confirmed for router {router.id} after {attempt + 1} check(s)")
            break
    else:
        logger.warning(f"[DIAGNOSE FIX] login.html version stamp not confirmed for router {router.id} within timeout")

    logger.info(f"[DIAGNOSE FIX] Refreshed hotspot HTML pages for router {router.id}")


register_check(
    id='login_html_current',
    label='Hotspot login page is up to date (fast-redirect optimization)',
    category='Performance',
    severity='warning',
    fix_fn=_fix_login_html_current,
)(_check_login_html_current)


# ────────────────────────────────────────────────────────────────
# 🔥 FIX 3: Phantom DNS name check — the #1 cause of slow captive redirects
# ────────────────────────────────────────────────────────────────

def _check_hotspot_no_phantom_dns(ctx: DiagnosticContext) -> bool:
    """
    A dns-name on netily-profile that isn't independently resolved means
    every captive-portal redirect eats a DNS lookup first. This is the
    #1 cause of "randomly slow on some phones" — CNA/captive webviews are
    far less tolerant of a stalled/failed DNS lookup than a real browser.
    Blank dns-name = RouterOS redirects straight to its own IP, instantly.
    """
    for p in ctx.live['hotspot_profiles']:
        if p.get('name') == 'netily-profile':
            return not p.get('dns-name')
    return True  # profile not provisioned yet — nothing to flag


def _fix_hotspot_no_phantom_dns(ctx: DiagnosticContext):
    api = ctx.api
    for p in ctx.live['hotspot_profiles']:
        if p.get('name') == 'netily-profile' and p.get('dns-name'):
            api._execute('/ip/hotspot/profile', update={
                '.id': p['.id'],
                'dns-name': '',
            })
            logger.info(
                f"[DIAGNOSE] Cleared phantom hotspot dns-name on router {ctx.router.id} "
                f"(was: {p.get('dns-name')})"
            )
            return


register_check(
    id='hotspot_no_phantom_dns',
    label='Captive redirect uses router IP, not an unresolvable DNS name',
    category='Performance',
    severity='critical',
    fix_fn=_fix_hotspot_no_phantom_dns,
)(_check_hotspot_no_phantom_dns)


# ────────────────────────────────────────────────────────────────
# RUNNER
# ────────────────────────────────────────────────────────────────

def run_diagnosis(router) -> dict:
    """Connects once, runs every registered check, disconnects. Read-only."""
    import apps.network.integrations.mikrotik_api as mikrotik_api_module

    api = mikrotik_api_module.MikrotikAPI(router)
    if not api.connect():
        return {
            'router_id': router.id,
            'error': 'connection_failed',
            'message': f"Could not connect to router '{router.name}'. Ensure it is online.",
            'results': [], 'summary': {'total': 0, 'passed': 0, 'issues': 0},
        }

    try:
        live = gather_live_state(api)
        ctx = DiagnosticContext(router=router, api=api, live=live)

        results = []
        for chk in CHECKS:
            try:
                passed = bool(chk.check_fn(ctx))
            except Exception as e:
                logger.error(f"[DIAGNOSE] check '{chk.id}' raised: {e}")
                passed = False
            results.append({
                'id': chk.id,
                'label': chk.label,
                'category': chk.category,
                'severity': chk.severity,
                'status': 'pass' if passed else 'fail',
                'fixable': chk.fix_fn is not None,
            })

        passed_count = sum(1 for r in results if r['status'] == 'pass')
        return {
            'router_id': router.id,
            'results': results,
            'summary': {
                'total': len(results),
                'passed': passed_count,
                'issues': len(results) - passed_count,
            },
        }
    finally:
        api.disconnect()


def run_fix(router, check_id: str) -> dict:
    """Applies the fix for exactly ONE check on exactly ONE router.
    Router is whatever was passed in — caller (view) is responsible for
    tenant-safe resolution, so this never crosses tenant/router boundaries."""
    import apps.network.integrations.mikrotik_api as mikrotik_api_module

    chk = next((c for c in CHECKS if c.id == check_id), None)
    if not chk:
        return {'success': False, 'error': f"Unknown check '{check_id}'"}
    if not chk.fix_fn:
        return {'success': False, 'error': f"Check '{check_id}' has no automated fix"}

    api = mikrotik_api_module.MikrotikAPI(router)
    if not api.connect():
        return {'success': False, 'error': 'connection_failed', 'message': 'Router is offline'}

    try:
        live = gather_live_state(api)
        ctx = DiagnosticContext(router=router, api=api, live=live)
        chk.fix_fn(ctx)

        # Re-check to confirm the fix actually landed
        live_after = gather_live_state(api)
        ctx_after = DiagnosticContext(router=router, api=api, live=live_after)
        verified = bool(chk.check_fn(ctx_after))

        return {
            'success': verified,
            'check_id': check_id,
            'label': chk.label,
            'message': 'Fix applied and verified' if verified else 'Fix applied but verification failed — please re-run diagnosis',
        }
    except Exception as e:
        logger.error(f"[DIAGNOSE FIX] {check_id} failed on router {router.id}: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        api.disconnect()


def run_fix_all(router) -> dict:
    """Runs diagnosis, then applies every fixable failing check, in order,
    on this router only. Single connection reused across the whole batch
    for speed."""
    import apps.network.integrations.mikrotik_api as mikrotik_api_module

    api = mikrotik_api_module.MikrotikAPI(router)
    if not api.connect():
        return {'success': False, 'error': 'connection_failed', 'message': 'Router is offline'}

    applied, failed = [], []
    try:
        live = gather_live_state(api)
        ctx = DiagnosticContext(router=router, api=api, live=live)

        for chk in CHECKS:
            try:
                if chk.check_fn(ctx):
                    continue  # already passing
            except Exception:
                pass

            if not chk.fix_fn:
                continue

            try:
                chk.fix_fn(ctx)
                applied.append(chk.id)
            except Exception as e:
                logger.error(f"[DIAGNOSE FIX-ALL] {chk.id} failed on router {router.id}: {e}")
                failed.append({'id': chk.id, 'error': str(e)})

        return {'success': len(failed) == 0, 'applied': applied, 'failed': failed}
    finally:
        api.disconnect()