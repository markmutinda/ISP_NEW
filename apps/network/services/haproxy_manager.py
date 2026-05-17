"""HAProxy Config Manager — Dynamic TCP proxy for remote Winbox/API access"""
import logging
import subprocess
import os
from django.conf import settings

logger = logging.getLogger(__name__)

HAPROXY_CONFIG_PATH = os.environ.get('HAPROXY_CONFIG_PATH', '/app/docker/haproxy/haproxy.cfg')
HAPROXY_TEMPLATE_PATH = os.environ.get('HAPROXY_TEMPLATE_PATH', '/app/docker/haproxy/haproxy.cfg.template')

WINBOX_PORT_BASE = int(os.environ.get('WINBOX_PORT_BASE', '40000')) # Ports 40000+
API_PORT_BASE = int(os.environ.get('API_PORT_BASE', '50000'))       # Ports 50000+

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
        name = router.get('name', f'router_{router_id}').replace(' ', '_').lower()

        if not vpn_ip:
            continue

        winbox_port = get_router_winbox_port(router_id)
        api_port = get_router_api_port(router_id)

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
    try:
        result = subprocess.run(
            ['docker', 'exec', 'netily_haproxy', 'kill', '-USR2', '1'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("[HAPROXY] Reloaded successfully (zero-downtime)")
            return True
        logger.error(f"[HAPROXY] Reload failed: {result.stderr}")
        return False
    except Exception as e:
        logger.error(f"[HAPROXY] Reload error: {e}")
        return False

def sync_haproxy_config() -> bool:
    from django_tenants.utils import schema_context, get_tenant_model
    from apps.network.models.router_models import Router

    TenantModel = get_tenant_model()
    all_routers = []

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                routers = Router.objects.filter(
                    vpn_provisioned=True,
                    is_active=True,
                    vpn_ip_address__isnull=False,
                ).values('id', 'name', 'vpn_ip_address')
                all_routers.extend(list(routers))
        except Exception as e:
            logger.error(f"[HAPROXY] Error reading routers for {tenant.schema_name}: {e}")

    config = generate_haproxy_config(all_routers)

    try:
        with open(HAPROXY_CONFIG_PATH, 'w') as f:
            f.write(config)
        logger.info(f"[HAPROXY] Config written with {len(all_routers)} routers")
    except Exception as e:
        logger.error(f"[HAPROXY] Config write failed: {e}")
        return False

    return reload_haproxy()