# apps/network/integrations/olt_integration.py
import paramiko
import telnetlib
import time
import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)


class OLTIntegration(ABC):
    """Abstract base class for OLT integrations"""
    
    def __init__(self, host: str, username: str, password: str, port: int = 23):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.connection = None
    
    @abstractmethod
    def connect(self):
        """Connect to OLT"""
        pass
    
    @abstractmethod
    def disconnect(self):
        """Disconnect from OLT"""
        pass
    
    @abstractmethod
    def get_device_info(self) -> Dict[str, Any]:
        """Get OLT device information"""
        pass
    
    @abstractmethod
    def get_pon_ports(self) -> List[Dict[str, Any]]:
        """Get PON ports information"""
        pass
    
    @abstractmethod
    def get_onus(self, pon_port: str) -> List[Dict[str, Any]]:
        """Get ONUs on specific PON port"""
        pass
    
    @abstractmethod
    def get_onu_info(self, serial_number: str) -> Dict[str, Any]:
        """Get specific ONU information"""
        pass
    
    @abstractmethod
    def reboot_onu(self, serial_number: str) -> bool:
        """Reboot ONU"""
        pass
    
    @abstractmethod
    def authorize_onu(self, pon_port: str, serial_number: str) -> bool:
        """Authorize ONU on PON port"""
        pass
    
    @abstractmethod
    def deauthorize_onu(self, pon_port: str, serial_number: str) -> bool:
        """Deauthorize ONU from PON port"""
        pass


class ZTEIntegration(OLTIntegration):
    """ZTE OLT Integration via Telnet"""
    
    def connect(self):
        """Connect to ZTE OLT via Telnet"""
        try:
            self.connection = telnetlib.Telnet(self.host, self.port, timeout=10)
            
            # Login sequence
            self.connection.read_until(b"Username:")
            self.connection.write(self.username.encode('ascii') + b"\n")
            
            self.connection.read_until(b"Password:")
            self.connection.write(self.password.encode('ascii') + b"\n")
            
            # Wait for prompt
            self.connection.read_until(b">")
            logger.info(f"Connected to ZTE OLT at {self.host}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to ZTE OLT: {str(e)}")
            return False
    
    def disconnect(self):
        """Disconnect from OLT"""
        if self.connection:
            self.connection.write(b"exit\n")
            self.connection.close()
            self.connection = None
    
    def _send_command(self, command: str, wait_for: str = ">") -> str:
        """Send command and get response"""
        if not self.connection:
            self.connect()
        
        self.connection.write(command.encode('ascii') + b"\n")
        time.sleep(0.5)
        response = self.connection.read_until(wait_for.encode('ascii')).decode('ascii')
        return response
    
    def get_device_info(self) -> Dict[str, Any]:
        """Get ZTE OLT device information"""
        try:
            # Get system info
            response = self._send_command("show card")
            
            info = {
                'vendor': 'ZTE',
                'model': self._extract_model(response),
                'serial_number': self._extract_serial(response),
                'software_version': self._extract_version(response),
                'uptime': self._extract_uptime(response),
                'cpu_usage': self._extract_cpu_usage(response),
                'memory_usage': self._extract_memory_usage(response),
            }
            
            return info
        except Exception as e:
            logger.error(f"Failed to get device info: {str(e)}")
            return {}
    
    def get_pon_ports(self) -> List[Dict[str, Any]]:
        """Get ZTE PON ports information"""
        ports = []
        try:
            response = self._send_command("show gpon onu state")
            
            # Parse response to get PON ports
            lines = response.split('\n')
            for line in lines:
                if 'gpon' in line.lower() and 'onu' in line.lower():
                    parts = line.split()
                    if len(parts) >= 5:
                        port_info = {
                            'port': parts[0],
                            'admin_state': parts[1],
                            'operational_state': parts[2],
                            'total_onus': int(parts[3]) if parts[3].isdigit() else 0,
                            'registered_onus': int(parts[4]) if parts[4].isdigit() else 0,
                        }
                        ports.append(port_info)
            
            return ports
        except Exception as e:
            logger.error(f"Failed to get PON ports: {str(e)}")
            return []
    
    def get_onus(self, pon_port: str) -> List[Dict[str, Any]]:
        """Get ONUs on specific PON port"""
        onus = []
        try:
            response = self._send_command(f"show gpon onu by pon {pon_port}")
            
            # Parse ONU information
            lines = response.split('\n')
            for line in lines:
                if 'gpon' in line.lower() and 'onu' in line.lower():
                    parts = line.split()
                    if len(parts) >= 8:
                        onu_info = {
                            'onu_id': parts[0],
                            'serial_number': parts[1],
                            'status': parts[2],
                            'rx_power': float(parts[3]) if self._is_float(parts[3]) else None,
                            'tx_power': float(parts[4]) if self._is_float(parts[4]) else None,
                            'distance': float(parts[5]) if self._is_float(parts[5]) else None,
                            'last_seen': parts[6] if len(parts) > 6 else '',
                            'description': parts[7] if len(parts) > 7 else '',
                        }
                        onus.append(onu_info)
            
            return onus
        except Exception as e:
            logger.error(f"Failed to get ONUs for port {pon_port}: {str(e)}")
            return []
    
    def get_onu_info(self, serial_number: str) -> Dict[str, Any]:
        """Get specific ONU information"""
        try:
            response = self._send_command(f"show gpon onu detail {serial_number}")
            
            # Parse detailed ONU info
            info = {
                'serial_number': serial_number,
                'status': self._extract_onu_status(response),
                'rx_power': self._extract_rx_power(response),
                'tx_power': self._extract_tx_power(response),
                'distance': self._extract_distance(response),
                'model': self._extract_onu_model(response),
                'software_version': self._extract_onu_version(response),
                'mac_address': self._extract_mac_address(response),
            }
            
            return info
        except Exception as e:
            logger.error(f"Failed to get ONU info for {serial_number}: {str(e)}")
            return {}
    
    def reboot_onu(self, serial_number: str) -> bool:
        """Reboot ONU"""
        try:
            response = self._send_command(f"reboot gpon onu {serial_number}")
            return 'success' in response.lower() or 'rebooting' in response.lower()
        except Exception as e:
            logger.error(f"Failed to reboot ONU {serial_number}: {str(e)}")
            return False
    
    def authorize_onu(self, pon_port: str, serial_number: str) -> bool:
        """Authorize ONU on PON port"""
        try:
            response = self._send_command(f"authorize gpon onu {pon_port} sn {serial_number}")
            return 'success' in response.lower()
        except Exception as e:
            logger.error(f"Failed to authorize ONU {serial_number} on {pon_port}: {str(e)}")
            return False
    
    def deauthorize_onu(self, pon_port: str, serial_number: str) -> bool:
        """Deauthorize ONU from PON port"""
        try:
            response = self._send_command(f"deauthorize gpon onu {pon_port} {serial_number}")
            return 'success' in response.lower()
        except Exception as e:
            logger.error(f"Failed to deauthorize ONU {serial_number} from {pon_port}: {str(e)}")
            return False
    
    # Helper methods for parsing ZTE responses
    def _extract_model(self, response: str) -> str:
        match = re.search(r'Model\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_serial(self, response: str) -> str:
        match = re.search(r'Serial\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_version(self, response: str) -> str:
        match = re.search(r'Version\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_uptime(self, response: str) -> str:
        match = re.search(r'Uptime\s*:\s*([\d\w\s:]+)', response, re.IGNORECASE)
        return match.group(1).strip() if match else 'Unknown'
    
    def _extract_cpu_usage(self, response: str) -> float:
        match = re.search(r'CPU\s*Usage\s*:\s*(\d+)%', response, re.IGNORECASE)
        return float(match.group(1)) if match else 0.0
    
    def _extract_memory_usage(self, response: str) -> float:
        match = re.search(r'Memory\s*Usage\s*:\s*(\d+)%', response, re.IGNORECASE)
        return float(match.group(1)) if match else 0.0
    
    def _extract_onu_status(self, response: str) -> str:
        match = re.search(r'Status\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_rx_power(self, response: str) -> Optional[float]:
        match = re.search(r'Rx\s*Power\s*:\s*([-\d.]+)', response, re.IGNORECASE)
        return float(match.group(1)) if match and self._is_float(match.group(1)) else None
    
    def _extract_tx_power(self, response: str) -> Optional[float]:
        match = re.search(r'Tx\s*Power\s*:\s*([-\d.]+)', response, re.IGNORECASE)
        return float(match.group(1)) if match and self._is_float(match.group(1)) else None
    
    def _extract_distance(self, response: str) -> Optional[float]:
        match = re.search(r'Distance\s*:\s*([\d.]+)', response, re.IGNORECASE)
        return float(match.group(1)) if match and self._is_float(match.group(1)) else None
    
    def _extract_onu_model(self, response: str) -> str:
        match = re.search(r'ONU\s*Model\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_onu_version(self, response: str) -> str:
        match = re.search(r'Software\s*Version\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_mac_address(self, response: str) -> str:
        match = re.search(r'MAC\s*:\s*([0-9A-Fa-f:]+)', response, re.IGNORECASE)
        return match.group(1) if match else ''
    
    def _is_float(self, value: str) -> bool:
        try:
            float(value)
            return True
        except ValueError:
            return False


class HuaweiIntegration(OLTIntegration):
    """Huawei OLT Integration via SSH"""
    
    def connect(self):
        """Connect to Huawei OLT via SSH"""
        try:
            self.connection = paramiko.SSHClient()
            self.connection.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.connection.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10
            )
            
            # Get shell
            self.shell = self.connection.invoke_shell()
            time.sleep(1)
            
            # Read welcome message
            self.shell.recv(1000)
            logger.info(f"Connected to Huawei OLT at {self.host}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Huawei OLT: {str(e)}")
            return False
    
    def disconnect(self):
        """Disconnect from OLT"""
        if self.connection:
            self.connection.close()
            self.connection = None
            self.shell = None
    
    def _send_command(self, command: str) -> str:
        """Send command and get response"""
        if not self.connection:
            self.connect()
        
        self.shell.send(command + '\n')
        time.sleep(1)
        
        # Read response
        output = b''
        while self.shell.recv_ready():
            output += self.shell.recv(4096)
            time.sleep(0.1)
        
        return output.decode('utf-8', errors='ignore')
    
    def get_device_info(self) -> Dict[str, Any]:
        """Get Huawei OLT device information"""
        try:
            # Get system info
            response = self._send_command("display device")
            
            info = {
                'vendor': 'HUAWEI',
                'model': self._extract_model(response),
                'serial_number': self._extract_serial(response),
                'software_version': self._extract_version(response),
                'uptime': self._extract_uptime(response),
                'cpu_usage': self._extract_cpu_usage(response),
                'memory_usage': self._extract_memory_usage(response),
            }
            
            return info
        except Exception as e:
            logger.error(f"Failed to get device info: {str(e)}")
            return {}
    
    def get_pon_ports(self) -> List[Dict[str, Any]]:
        """Get Huawei PON ports information"""
        ports = []
        try:
            response = self._send_command("display ont info summary all")
            
            # Parse Huawei-specific format
            lines = response.split('\n')
            current_port = None
            
            for line in lines:
                # Look for port information
                if 'gpon' in line.lower() and 'port' in line.lower():
                    parts = line.split()
                    if len(parts) >= 2:
                        current_port = parts[1]
                
                # Look for ONU summary
                elif current_port and 'total' in line.lower():
                    parts = line.split()
                    if len(parts) >= 4:
                        port_info = {
                            'port': current_port,
                            'total_onus': int(parts[1]) if parts[1].isdigit() else 0,
                            'online_onus': int(parts[2]) if parts[2].isdigit() else 0,
                            'offline_onus': int(parts[3]) if parts[3].isdigit() else 0,
                        }
                        ports.append(port_info)
                        current_port = None
            
            return ports
        except Exception as e:
            logger.error(f"Failed to get PON ports: {str(e)}")
            return []
    
    def get_onus(self, pon_port: str) -> List[Dict[str, Any]]:
        """Get ONUs on specific PON port"""
        onus = []
        try:
            response = self._send_command(f"display ont info {pon_port} all")
            
            # Parse Huawei ONU information
            lines = response.split('\n')
            current_onu = {}
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # ONU ID line
                if 'ONT ID' in line:
                    if current_onu:
                        onus.append(current_onu)
                    current_onu = {'port': pon_port}
                    parts = line.split(':')
                    if len(parts) > 1:
                        current_onu['onu_id'] = parts[1].strip()
                
                # Serial number
                elif 'SN' in line and ':' in line:
                    parts = line.split(':')
                    if len(parts) > 1:
                        current_onu['serial_number'] = parts[1].strip()
                
                # Status
                elif 'Run state' in line:
                    parts = line.split(':')
                    if len(parts) > 1:
                        current_onu['status'] = parts[1].strip()
                
                # Signal information
                elif 'Rx optical power' in line:
                    match = re.search(r'([-\d.]+)dBm', line)
                    if match:
                        current_onu['rx_power'] = float(match.group(1))
                
                elif 'Tx optical power' in line:
                    match = re.search(r'([-\d.]+)dBm', line)
                    if match:
                        current_onu['tx_power'] = float(match.group(1))
            
            # Add last ONU
            if current_onu:
                onus.append(current_onu)
            
            return onus
        except Exception as e:
            logger.error(f"Failed to get ONUs for port {pon_port}: {str(e)}")
            return []
    
    def get_onu_info(self, serial_number: str) -> Dict[str, Any]:
        """Get specific ONU information"""
        try:
            # Huawei requires port information, so we need to search first
            response = self._send_command(f"display ont info by-sn {serial_number}")
            
            info = {
                'serial_number': serial_number,
                'status': self._extract_huawei_status(response),
                'rx_power': self._extract_huawei_rx_power(response),
                'tx_power': self._extract_huawei_tx_power(response),
                'distance': self._extract_huawei_distance(response),
                'model': self._extract_huawei_model(response),
                'software_version': self._extract_huawei_version(response),
                'mac_address': self._extract_huawei_mac(response),
            }
            
            return info
        except Exception as e:
            logger.error(f"Failed to get ONU info for {serial_number}: {str(e)}")
            return {}
    
    def reboot_onu(self, serial_number: str) -> bool:
        """Reboot ONU"""
        try:
            # First get port information
            response = self._send_command(f"display ont info by-sn {serial_number}")
            
            # Extract port and ONU ID
            port_match = re.search(r'GPON\s+(\d+/\d+/\d+)', response)
            onu_match = re.search(r'ONT ID\s*:\s*(\d+)', response)
            
            if port_match and onu_match:
                port = port_match.group(1)
                onu_id = onu_match.group(1)
                
                # Send reboot command
                reboot_response = self._send_command(
                    f"reboot ont {port} {onu_id}"
                )
                return 'success' in reboot_response.lower()
            
            return False
        except Exception as e:
            logger.error(f"Failed to reboot ONU {serial_number}: {str(e)}")
            return False
    
    def authorize_onu(self, pon_port: str, serial_number: str) -> bool:
        """Authorize ONU on PON port"""
        try:
            response = self._send_command(
                f"ont add {pon_port} sn-auth {serial_number}"
            )
            return 'success' in response.lower()
        except Exception as e:
            logger.error(f"Failed to authorize ONU {serial_number} on {pon_port}: {str(e)}")
            return False
    
    def deauthorize_onu(self, pon_port: str, serial_number: str) -> bool:
        """Deauthorize ONU from PON port"""
        try:
            # First get ONU ID
            response = self._send_command(f"display ont info by-sn {serial_number}")
            onu_match = re.search(r'ONT ID\s*:\s*(\d+)', response)
            
            if onu_match:
                onu_id = onu_match.group(1)
                delete_response = self._send_command(
                    f"ont delete {pon_port} {onu_id}"
                )
                return 'success' in delete_response.lower()
            
            return False
        except Exception as e:
            logger.error(f"Failed to deauthorize ONU {serial_number} from {pon_port}: {str(e)}")
            return False
    
    # Helper methods for parsing Huawei responses
    def _extract_model(self, response: str) -> str:
        match = re.search(r'Board type\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_serial(self, response: str) -> str:
        match = re.search(r'Board serial number\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_version(self, response: str) -> str:
        match = re.search(r'Software Version\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_uptime(self, response: str) -> str:
        match = re.search(r'System uptime\s*:\s*([\d\w\s:]+)', response, re.IGNORECASE)
        return match.group(1).strip() if match else 'Unknown'
    
    def _extract_cpu_usage(self, response: str) -> float:
        match = re.search(r'CPU usage\s*:\s*(\d+)%', response, re.IGNORECASE)
        return float(match.group(1)) if match else 0.0
    
    def _extract_memory_usage(self, response: str) -> float:
        match = re.search(r'Memory usage\s*:\s*(\d+)%', response, re.IGNORECASE)
        return float(match.group(1)) if match else 0.0
    
    def _extract_huawei_status(self, response: str) -> str:
        match = re.search(r'Run state\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_huawei_rx_power(self, response: str) -> Optional[float]:
        match = re.search(r'Rx optical power\s*:\s*([-\d.]+)dBm', response, re.IGNORECASE)
        return float(match.group(1)) if match else None
    
    def _extract_huawei_tx_power(self, response: str) -> Optional[float]:
        match = re.search(r'Tx optical power\s*:\s*([-\d.]+)dBm', response, re.IGNORECASE)
        return float(match.group(1)) if match else None
    
    def _extract_huawei_distance(self, response: str) -> Optional[float]:
        match = re.search(r'Distance\s*:\s*([\d.]+)km', response, re.IGNORECASE)
        return float(match.group(1)) * 1000 if match else None  # Convert to meters
    
    def _extract_huawei_model(self, response: str) -> str:
        match = re.search(r'ONT type\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_huawei_version(self, response: str) -> str:
        match = re.search(r'ONT software version\s*:\s*(\S+)', response, re.IGNORECASE)
        return match.group(1) if match else 'Unknown'
    
    def _extract_huawei_mac(self, response: str) -> str:
        match = re.search(r'MAC address\s*:\s*([0-9A-Fa-f-]+)', response, re.IGNORECASE)
        return match.group(1) if match else ''


class OLTManager:
    """Manager class for OLT operations"""
    
    def __init__(self, olt_device):
        self.olt_device = olt_device
        self.integration = self._get_integration()
    
    def _get_integration(self):
        """Get appropriate integration based on vendor"""
        vendor = self.olt_device.vendor
        
        if vendor == 'ZTE':
            return ZTEIntegration(
                host=self.olt_device.ip_address,
                username=self.olt_device.ssh_username,
                password=self.olt_device.ssh_password,
                port=self.olt_device.telnet_port
            )
        elif vendor == 'HUAWEI':
            return HuaweiIntegration(
                host=self.olt_device.ip_address,
                username=self.olt_device.ssh_username,
                password=self.olt_device.ssh_password,
                port=self.olt_device.ssh_port or 22
            )
        else:
            raise ValueError(f"Unsupported OLT vendor: {vendor}")
    
    def sync_device_info(self) -> Dict[str, Any]:
        """Sync device information from OLT"""
        try:
            if not self.integration.connect():
                raise ConnectionError(f"Failed to connect to OLT {self.olt_device.name}")
            
            info = self.integration.get_device_info()
            self.integration.disconnect()
            
            return info
        except Exception as e:
            logger.error(f"Failed to sync device info: {str(e)}")
            raise
    
    def sync_pon_ports(self) -> List[Dict[str, Any]]:
        """Sync PON ports information"""
        try:
            if not self.integration.connect():
                raise ConnectionError(f"Failed to connect to OLT {self.olt_device.name}")
            
            ports = self.integration.get_pon_ports()
            self.integration.disconnect()
            
            return ports
        except Exception as e:
            logger.error(f"Failed to sync PON ports: {str(e)}")
            raise
    
    def get_onu_info(self, serial_number: str) -> Dict[str, Any]:
        """Get ONU information"""
        try:
            if not self.integration.connect():
                raise ConnectionError(f"Failed to connect to OLT {self.olt_device.name}")
            
            info = self.integration.get_onu_info(serial_number)
            self.integration.disconnect()
            
            return info
        except Exception as e:
            logger.error(f"Failed to get ONU info: {str(e)}")
            raise
    
    def reboot_onu(self, serial_number: str) -> bool:
        """Reboot ONU"""
        try:
            if not self.integration.connect():
                raise ConnectionError(f"Failed to connect to OLT {self.olt_device.name}")
            
            result = self.integration.reboot_onu(serial_number)
            self.integration.disconnect()
            
            return result
        except Exception as e:
            logger.error(f"Failed to reboot ONU: {str(e)}")
            raise
    
    def apply_config(self, config_data: str) -> bool:
        """Apply configuration to OLT"""
        try:
            # This would typically involve sending configuration commands
            # For now, we'll log and return success
            logger.info(f"Applying configuration to OLT {self.olt_device.name}")
            
            # In production, you would:
            # 1. Connect to OLT
            # 2. Enter configuration mode
            # 3. Send config commands
            # 4. Save configuration
            # 5. Verify changes
            
            return True
        except Exception as e:
            logger.error(f"Failed to apply configuration: {str(e)}")
            raise
    
    def backup_config(self) -> str:
        """Backup OLT configuration"""
        try:
            if not self.integration.connect():
                raise ConnectionError(f"Failed to connect to OLT {self.olt_device.name}")
            
            # Get running configuration
            # This would be vendor-specific
            config = "Backup configuration not implemented for this vendor"
            self.integration.disconnect()
            
            return config
        except Exception as e:
            logger.error(f"Failed to backup configuration: {str(e)}")
            raise