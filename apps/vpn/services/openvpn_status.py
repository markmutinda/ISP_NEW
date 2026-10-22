import logging
import subprocess

logger = logging.getLogger(__name__)

OVPN_CONTAINER = 'netily-ovpn'
STATUS_FILE = '/tmp/ovpn-status.log'


def get_connected_vpn_ips():
    """Set of VPN IPs (CN == VPN IP) currently connected, or None if status is unreadable."""
    try:
        r = subprocess.run(
            ['docker', 'exec', OVPN_CONTAINER, 'cat', STATUS_FILE],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        logger.error(f"[OVPN STATUS] exec failed: {e}")
        return None
    if r.returncode != 0:
        logger.error(f"[OVPN STATUS] cannot read status: {r.stderr.strip()}")
        return None

    ips, in_clients = set(), False
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith('Common Name,Real Address'):
            in_clients = True
            continue
        if line.startswith(('ROUTING TABLE', 'GLOBAL STATS', 'END')):
            in_clients = False
            continue
        if in_clients and ',' in line:
            ips.add(line.split(',', 1)[0])
    return ips