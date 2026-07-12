"""WireGuard Server-Side Peer Manager
Uses Docker Socket to safely execute commands inside the VPN container."""

import base64
import logging
import os
import json
import socket
import time
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat)
from django.conf import settings

logger = logging.getLogger(__name__)

WG_INTERFACE   = os.environ.get('WG_INTERFACE', 'wg0')
WG_SERVER_PORT = int(os.environ.get('WG_SERVER_PORT', '51820'))
WG_CONTAINER   = 'netily-openvpn'

def docker_exec(cmd_list):
    """Executes a command inside the WireGuard container via the mounted Docker socket."""
    try:
        # 1. Create Exec Instance
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)  # ← ADDED: prevent hanging
        s.connect('/var/run/docker.sock')
        payload = json.dumps({"AttachStdout": True, "AttachStderr": True, "Cmd": cmd_list}).encode('utf-8')
        req = (f"POST /containers/{WG_CONTAINER}/exec HTTP/1.0\r\n"
               f"Content-Type: application/json\r\n"
               f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload
        s.sendall(req)
        resp = s.recv(4096).decode('utf-8')
        s.close()
        
        # Extract Exec ID
        exec_id = json.loads(resp.split('\r\n\r\n')[1])['Id']
        
        # 2. Start Exec Instance
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)  # ← ADDED: prevent hanging
        s.connect('/var/run/docker.sock')
        payload = json.dumps({"Detach": False, "Tty": False}).encode('utf-8')
        req = (f"POST /exec/{exec_id}/start HTTP/1.0\r\n"
               f"Content-Type: application/json\r\n"
               f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload
        s.sendall(req)
        
        # 3. Read Output Stream
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk: break
            data += chunk
        s.close()
        
        # Remove HTTP Headers
        header_end = data.find(b'\r\n\r\n')
        if header_end != -1:
            data = data[header_end+4:]
            
        # Parse Docker Stream Multiplexing (8-byte header per frame)
        output = ""
        idx = 0
        while idx < len(data):
            if idx + 8 > len(data): break
            size = int.from_bytes(data[idx+4:idx+8], byteorder='big')
            idx += 8
            output += data[idx:idx+size].decode('utf-8', errors='ignore')
            idx += size
            
        return True, output.strip()
    except Exception as e:
        logger.error(f"[WG] Docker exec failed: {e}")
        return False, str(e)


def generate_keypair() -> dict:
    priv = X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes  = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {
        'private_key': base64.b64encode(priv_bytes).decode(),
        'public_key':  base64.b64encode(pub_bytes).decode(),
    }

def get_server_public_key() -> str:
    key = getattr(settings, 'WG_SERVER_PUBLIC_KEY', '')
    if not key:
        key = os.environ.get('WG_SERVER_PUBLIC_KEY', '')
    return key

def get_server_endpoint() -> str:
    host = getattr(settings, 'WG_SERVER_HOST', os.environ.get('WG_SERVER_HOST', ''))
    if not host:
        host = getattr(settings, 'VPN_SERVER_IP', os.environ.get('VPN_SERVER_IP', ''))
    return f"{host}:{WG_SERVER_PORT}"

def add_peer(public_key: str, allowed_ip: str) -> bool:
    """Add a router peer dynamically AND save it permanently."""
    try:
        # Hot-add peer
        cmd = ['wg', 'set', WG_INTERFACE, 'peer', public_key, 'allowed-ips', f'{allowed_ip}/32']
        success, output = docker_exec(cmd)
        if not success:
            logger.warning(f"[WG] Hot-add issue (might be okay): {output}")
            
        # Save state to survive server reboots
        docker_exec(['sh', '-c', f'wg showconf {WG_INTERFACE} > /config/wg_confs/{WG_INTERFACE}.conf'])
        
        logger.info(f"[WG] Peer added & saved: {public_key[:8]}... -> {allowed_ip}")
        return True
    except Exception as e:
        logger.error(f"[WG] add_peer failed for {allowed_ip}: {e}")
        return False

def remove_peer(public_key: str) -> bool:
    """Remove a router peer from the server."""
    try:
        docker_exec(['wg', 'set', WG_INTERFACE, 'peer', public_key, 'remove'])
        docker_exec(['sh', '-c', f'wg showconf {WG_INTERFACE} > /config/wg_confs/{WG_INTERFACE}.conf'])
        logger.info(f"[WG] Peer removed: {public_key[:8]}...")
        return True
    except Exception as e:
        logger.error(f"[WG] remove_peer failed: {e}")
        return False

def list_connected_peers() -> list:
    """List actively connected peers with their usage stats."""
    try:
        success, output = docker_exec(['wg', 'show', WG_INTERFACE, 'dump'])
        if not success or not output: return []
            
        peers = []
        for line in output.strip().split('\n'):
            parts = line.split('\t')
            # Look for valid peer lines
            if len(parts) >= 5 and len(parts[0]) == 44 and parts[0].endswith('='):
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

def get_peer_handshake_age(public_key: str):
    """
    Returns seconds since last WireGuard handshake for this peer,
    or None if no handshake has ever occurred / peer not found.
    """
    if not public_key:
        return None
    try:
        peers = list_connected_peers()
    except Exception:
        return None
    for p in peers:
        if p.get('public_key') == public_key:
            latest = p.get('latest_handshake') or 0
            if latest <= 0:
                return None
            return time.time() - latest
    return None

def get_wireguard_interface_stats() -> dict:
    """
    Fetches basic health stats for the WireGuard interface.
    Required by the Celery check_vpn_health task.
    """
    try:
        # Ask the WireGuard container if the interface is running
        success, output = docker_exec(['wg', 'show', WG_INTERFACE])
        
        # If the command succeeds and the interface name is in the output, it's alive
        if success and WG_INTERFACE in output:
            return {
                'status': 'active',
                'interface': WG_INTERFACE,
            }
        return {'status': 'inactive'}
        
    except Exception as e:
        logger.error(f"[WG] Interface stats check failed: {e}")
        return {'status': 'error', 'error': str(e)}