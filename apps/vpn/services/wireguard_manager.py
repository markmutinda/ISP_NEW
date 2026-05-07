"""WireGuard Server-Side Peer Manager
Replaces CCD Manager and OpenVPN management."""
import base64
import logging
import os
import subprocess
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat)
from django.conf import settings

logger = logging.getLogger(__name__)

WG_INTERFACE   = os.environ.get('WG_INTERFACE', 'wg0')
WG_PEERS_DIR   = os.environ.get('WG_PEERS_DIR', '/etc/wireguard/peers')
WG_SERVER_PORT = int(os.environ.get('WG_SERVER_PORT', '51820'))

def generate_keypair() -> dict:
    """Generate a WireGuard X25519 keypair — no wg binary needed."""
    priv = X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes  = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        'private_key': base64.b64encode(priv_bytes).decode(),
        'public_key':  base64.b64encode(pub_bytes).decode(),
    }

def get_server_public_key() -> str:
    """Read server public key from settings / env."""
    key = getattr(settings, 'WG_SERVER_PUBLIC_KEY', '')
    if not key:
        key = os.environ.get('WG_SERVER_PUBLIC_KEY', '')
    if not key:
        priv_path = os.environ.get('WG_SERVER_PRIVATE_KEY_PATH', '/etc/wireguard/server_private.key')
        try:
            with open(priv_path) as f:
                priv_b64 = f.read().strip()
            priv_bytes = base64.b64decode(priv_b64)
            priv = X25519PrivateKey.from_private_bytes(priv_bytes)
            pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            key = base64.b64encode(pub_bytes).decode()
        except Exception as e:
            logger.error(f"[WG] Cannot read server public key: {e}")
    return key

def get_server_endpoint() -> str:
    """Return host:port for the WireGuard server endpoint."""
    host = getattr(settings, 'WG_SERVER_HOST', os.environ.get('WG_SERVER_HOST', ''))
    if not host:
        host = getattr(settings, 'VPN_SERVER_IP', os.environ.get('VPN_SERVER_IP', ''))
    return f"{host}:{WG_SERVER_PORT}"

def add_peer(public_key: str, allowed_ip: str) -> bool:
    """Add a router peer to the WireGuard server."""
    try:
        Path(WG_PEERS_DIR).mkdir(parents=True, exist_ok=True)
        safe_name = public_key.replace('/', '_').replace('+', '-')[:16]
        peer_file = os.path.join(WG_PEERS_DIR, f"{safe_name}.conf")
        peer_conf = f"[Peer]\nPublicKey = {public_key}\nAllowedIPs = {allowed_ip}/32\n"
        with open(peer_file, 'w') as f:
            f.write(peer_conf)
            
        r = subprocess.run(
            ['wg', 'set', WG_INTERFACE, 'peer', public_key, 'allowed-ips', f'{allowed_ip}/32'],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            logger.warning(f"[WG] wg set non-zero: {r.stderr.strip()}")
        logger.info(f"[WG] Peer added: {public_key[:8]}... → {allowed_ip}")
        return True
    except Exception as e:
        logger.error(f"[WG] add_peer failed for {allowed_ip}: {e}")
        return False

def remove_peer(public_key: str) -> bool:
    """Remove a router peer from the WireGuard server."""
    try:
        safe_name = public_key.replace('/', '_').replace('+', '-')[:16]
        peer_file = os.path.join(WG_PEERS_DIR, f"{safe_name}.conf")
        if os.path.exists(peer_file):
            os.remove(peer_file)
        subprocess.run(
            ['wg', 'set', WG_INTERFACE, 'peer', public_key, 'remove'],
            capture_output=True, timeout=10
        )
        logger.info(f"[WG] Peer removed: {public_key[:8]}...")
        return True
    except Exception as e:
        logger.error(f"[WG] remove_peer failed: {e}")
        return False

def list_connected_peers() -> list:
    """Return list of dicts with info about currently active WireGuard peers."""
    try:
        r = subprocess.run(
            ['wg', 'show', WG_INTERFACE, 'dump'],
            capture_output=True, text=True, timeout=10
        )
        peers = []
        for line in r.stdout.strip().splitlines()[1:]:  # skip server line
            parts = line.split('\t')
            if len(parts) >= 5:
                peers.append({
                    'public_key':       parts[0],
                    'preshared_key':    parts[1],
                    'endpoint':         parts[2],
                    'allowed_ips':      parts[3],
                    'latest_handshake': int(parts[4]),
                    'transfer_rx':      int(parts[5]) if len(parts) > 5 else 0,
                    'transfer_tx':      int(parts[6]) if len(parts) > 6 else 0,
                })
        return peers
    except Exception as e:
        logger.error(f"[WG] list_connected_peers failed: {e}")
        return []