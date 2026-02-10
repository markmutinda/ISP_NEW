"""
CCD (Client Config Directory) Manager — OpenVPN Static IP Mapping

Manages the CCD files on the OpenVPN server that map certificate
Common Names (CN) to static VPN IP addresses.

Each file is named after the CN and contains:
    ifconfig-push 10.8.0.55 255.255.255.0

This ensures a router always gets the same VPN IP regardless of
when or where it connects.
"""

import logging
import os
from typing import List, Dict, Optional

from django.conf import settings

logger = logging.getLogger(__name__)


class CCDManagerError(Exception):
    """Raised when CCD file operations fail."""
    pass


class CCDManager:
    """
    Manages OpenVPN Client Config Directory files.
    
    In Docker deployments, the CCD path is a mounted volume shared
    between the Django container and the OpenVPN container.
    """

    def __init__(self, ccd_path: Optional[str] = None):
        self.ccd_path = ccd_path or getattr(settings, 'OPENVPN_CCD_PATH', '/etc/openvpn/ccd')

    def create_ccd_file(self, common_name: str, vpn_ip: str, netmask: str = '255.255.255.0') -> str:
        """
        Create a CCD file that maps a certificate CN to a static VPN IP.
        
        Args:
            common_name: Certificate CN (the filename)
            vpn_ip: The static IP to assign (e.g. '10.8.0.55')
            netmask: Subnet mask (default 255.255.255.0)
            
        Returns:
            Path to the created CCD file.
        """
        self._ensure_ccd_directory()
        
        filepath = os.path.join(self.ccd_path, common_name)
        content = f"ifconfig-push {vpn_ip} {netmask}\n"
        
        try:
            with open(filepath, 'w') as f:
                f.write(content)
            logger.info(f"CCD file created: {filepath} -> {vpn_ip}")
            return filepath
        except OSError as e:
            logger.error(f"Failed to write CCD file {filepath}: {e}")
            raise CCDManagerError(f"Cannot write CCD file for {common_name}: {e}")

    def remove_ccd_file(self, common_name: str) -> bool:
        """
        Remove a CCD file when a router is deprovisioned.
        
        Returns True if file was removed, False if it didn't exist.
        """
        filepath = os.path.join(self.ccd_path, common_name)
        
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"CCD file removed: {filepath}")
                return True
            else:
                logger.warning(f"CCD file not found for removal: {filepath}")
                return False
        except OSError as e:
            logger.error(f"Failed to remove CCD file {filepath}: {e}")
            raise CCDManagerError(f"Cannot remove CCD file for {common_name}: {e}")

    def update_ccd_file(self, common_name: str, new_vpn_ip: str, netmask: str = '255.255.255.0') -> str:
        """Update an existing CCD file with a new IP."""
        return self.create_ccd_file(common_name, new_vpn_ip, netmask)

    def get_ccd_content(self, common_name: str) -> Optional[str]:
        """Read the content of a CCD file."""
        filepath = os.path.join(self.ccd_path, common_name)
        
        try:
            if os.path.exists(filepath):
                with open(filepath, 'r') as f:
                    return f.read().strip()
            return None
        except OSError as e:
            logger.error(f"Failed to read CCD file {filepath}: {e}")
            return None

    def list_ccd_files(self) -> List[Dict[str, str]]:
        """
        List all CCD files with their assigned IPs.
        
        Returns list of dicts: [{'common_name': '...', 'vpn_ip': '...', 'netmask': '...'}]
        """
        results = []
        
        if not os.path.isdir(self.ccd_path):
            return results
        
        try:
            for filename in os.listdir(self.ccd_path):
                filepath = os.path.join(self.ccd_path, filename)
                if os.path.isfile(filepath):
                    content = self.get_ccd_content(filename)
                    if content:
                        parts = content.split()
                        if len(parts) >= 2 and parts[0] == 'ifconfig-push':
                            results.append({
                                'common_name': filename,
                                'vpn_ip': parts[1],
                                'netmask': parts[2] if len(parts) > 2 else '255.255.255.0',
                            })
        except OSError as e:
            logger.error(f"Failed to list CCD directory {self.ccd_path}: {e}")
        
        return results

    def ccd_file_exists(self, common_name: str) -> bool:
        """Check if a CCD file exists for a given CN."""
        filepath = os.path.join(self.ccd_path, common_name)
        return os.path.isfile(filepath)

    def _ensure_ccd_directory(self):
        """Create the CCD directory if it doesn't exist."""
        try:
            os.makedirs(self.ccd_path, exist_ok=True)
        except OSError as e:
            logger.error(f"Cannot create CCD directory {self.ccd_path}: {e}")
            raise CCDManagerError(f"Cannot create CCD directory: {e}")
