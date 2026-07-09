"""HAProxy Config Manager — Dynamic TCP proxy for remote Winbox/API access"""
import logging
import subprocess
import os
import re
import time
from django.conf import settings

logger = logging.getLogger(__name__)

HAPROXY_CONFIG_PATH = os.environ.get('HAPROXY_CONFIG_PATH', '/app/docker/haproxy/haproxy.cfg')
HAPROXY_TEMPLATE_PATH = os.environ.get('HAPROXY_TEMPLATE_PATH', '/app/docker/haproxy/haproxy.cfg.template')

WINBOX_PORT_BASE = int(os.environ.get('WINBOX_PORT_BASE', '40000')) # Ports 40000+
API_PORT_BASE = int(os.environ.get('API_PORT_BASE', '50000'))       # Ports 50000+


def sanitize_for_haproxy(name: str) -> str:
    """
    Sanitize a router name so it is safe to use in HAProxy
    frontend/backend declaration names.
    HAProxy forbids: spaces, (), [], {}, /, \\, :, ;, #, !, @, |, =, ~
    
    This only sanitizes for HAProxy config generation — never stored in DB.
    The original display name is preserved in the database.
    """
    # Replace spaces and hyphens with underscores
    safe = name.replace(' ', '_').replace('-', '_')
    # Remove any character that is not alphanumeric or underscore
    safe = re.sub(r'[^\w]', '', safe)
    # Collapse multiple consecutive underscores
    safe = re.sub(r'_+', '_', safe)
    # Strip leading/trailing underscores
    safe = safe.strip('_')
    # Fallback if name becomes empty
    return safe or 'router'


def get_router_winbox_port(router_id: int) -> int:
    return WINBOX_PORT_BASE + router_id


def get_router_api_port(router_id: int) -> int:
    return API_PORT_BASE + router_id


def generate_haproxy_config(routers: list) -> str:
    with open(HAPROXY_TEMPLATE_PATH, 'r') as f:
        config = f.read()

    sections = []
    for router in routers:
        vpn_ip = router.get('vpn_ip_address')
        router_id = router.get('id')
        
        # Sanitize name for HAProxy config ONLY — original display name stays in DB
        raw_name = router.get('name', f'router_{router_id}')
        name = sanitize_for_haproxy(raw_name).lower()

        if not vpn_ip:
            continue

        # 🟢 Read pre-allocated unique ports directly instead of calculating from tenant ID
        winbox_port = router.get('winbox_remote_port')
        api_port = router.get('api_remote_port')

        if not winbox_port or not api_port:
            logger.warning(f"[HAPROXY] Router {name} (ID: {router_id}) missing remote port assignments. Skipping.")
            continue  

        sections.append(f"""
frontend winbox_{name}_{router_id}
    bind *:{winbox_port}
    default_backend be_winbox_{name}_{router_id}

backend be_winbox_{name}_{router_id}
    server router {vpn_ip}:8291 check inter 10s fall 3 rise 2

frontend api_{name}_{router_id}
    bind *:{api_port}
    default_backend be_api_{name}_{router_id}

backend be_api_{name}_{router_id}
    server router {vpn_ip}:8728 check inter 10s fall 3 rise 2
""")
    return config + '\n'.join(sections)


def reload_haproxy() -> bool:
    """
    Reload HAProxy with zero-downtime USR2 signal.
    Falls back to a hard restart if the container is not in a running state.
    """
    # First check if container is actually running
    check = subprocess.run(
        ['docker', 'inspect', '--format', '{{.State.Status}}', 'netily_haproxy'],
        capture_output=True, text=True, timeout=10
    )
    container_state = check.stdout.strip()

    if container_state != 'running':
        logger.warning(
            "[HAPROXY] Container state is '%s', attempting hard restart...",
            container_state
        )
        # Wait briefly if it's mid-restart
        if container_state == 'restarting':
            time.sleep(5)

        restart = subprocess.run(
            ['docker', 'restart', 'netily_haproxy'],
            capture_output=True, text=True, timeout=30
        )
        if restart.returncode == 0:
            logger.info("[HAPROXY] Hard restart successful.")
            return True
        else:
            logger.error("[HAPROXY] Hard restart failed: %s", restart.stderr.strip())
            return False

    # Container is running — try graceful zero-downtime reload first
    result = subprocess.run(
        ['docker', 'exec', 'netily_haproxy', 'kill', '-USR2', '1'],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        logger.info("[HAPROXY] Graceful reload successful (zero-downtime).")
        return True

    logger.warning(
        "[HAPROXY] Graceful reload failed (rc=%s), falling back to restart: %s",
        result.returncode, result.stderr.strip()
    )
    restart = subprocess.run(
        ['docker', 'restart', 'netily_haproxy'],
        capture_output=True, text=True, timeout=30
    )
    if restart.returncode == 0:
        logger.info("[HAPROXY] Fallback restart successful.")
        return True

    logger.error("[HAPROXY] Both reload and restart failed: %s", restart.stderr.strip())
    return False


def sync_haproxy_config() -> bool:
    from django_tenants.utils import schema_context, get_tenant_model
    from apps.network.models.router_models import Router

    TenantModel = get_tenant_model()
    all_routers = []

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                # 🟢 Explicitly fetch 'winbox_remote_port' and 'api_remote_port'
                routers = Router.objects.filter(
                    vpn_provisioned=True,
                    is_active=True,
                    vpn_ip_address__isnull=False,
                ).values('id', 'name', 'vpn_ip_address', 'winbox_remote_port', 'api_remote_port')
                all_routers.extend(list(routers))
        except Exception as e:
            logger.error(f"[HAPROXY] Error reading routers for {tenant.schema_name}: {e}")

    config = generate_haproxy_config(all_routers)

    try:
        with open(HAPROXY_CONFIG_PATH, 'w') as f:
            f.write(config + '\n\n')  # <--- THE FIX: Guaranteed new lines
        logger.info(f"[HAPROXY] Config written with {len(all_routers)} routers")
    except Exception as e:
        logger.error(f"[HAPROXY] Config write failed: {e}")
        return False

    return reload_haproxy()