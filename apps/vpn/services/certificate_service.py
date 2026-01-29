"""
Certificate Service - OpenVPN Certificate Generation and Management

This service handles:
1. Certificate Authority (CA) creation
2. Server certificate generation
3. Client certificate generation for routers
4. Certificate revocation
5. CRL (Certificate Revocation List) generation
"""

import logging
import subprocess
import tempfile
import os
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict, Any
from django.utils import timezone
from django.conf import settings

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False

from ..models import CertificateAuthority, VPNCertificate, generate_serial_number

logger = logging.getLogger(__name__)


class CertificateService:
    """
    Service for generating and managing OpenVPN certificates.
    Uses the cryptography library for certificate generation.
    """
    
    # Key size for RSA keys
    RSA_KEY_SIZE = 2048
    
    # Default validity periods
    CA_VALIDITY_DAYS = 3650  # 10 years
    SERVER_VALIDITY_DAYS = 825  # ~2.25 years (Apple requirement)
    CLIENT_VALIDITY_DAYS = 365  # 1 year
    
    def __init__(self):
        if not CRYPTOGRAPHY_AVAILABLE:
            raise ImportError(
                "cryptography library is required for certificate generation. "
                "Install it with: pip install cryptography"
            )
    
    # ────────────────────────────────────────────────────────────────
    # CERTIFICATE AUTHORITY
    # ────────────────────────────────────────────────────────────────
    
    def create_ca(
        self,
        name: str,
        common_name: str,
        organization: str = "Netily ISP",
        country: str = "KE",
        validity_days: int = None
    ) -> CertificateAuthority:
        """
        Create a new Certificate Authority.
        
        Args:
            name: Unique name for the CA
            common_name: CN for the CA certificate
            organization: Organization name
            country: 2-letter country code
            validity_days: CA validity in days
            
        Returns:
            CertificateAuthority instance
        """
        validity_days = validity_days or self.CA_VALIDITY_DAYS
        
        # Generate CA private key
        ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=self.RSA_KEY_SIZE,
            backend=default_backend()
        )
        
        # Build CA certificate
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        
        valid_from = datetime.utcnow()
        valid_until = valid_from + timedelta(days=validity_days)
        
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(valid_from)
            .not_valid_after(valid_until)
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_cert_sign=True,
                    crl_sign=True,
                    key_encipherment=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False
                ),
                critical=True
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                critical=False
            )
            .sign(ca_key, hashes.SHA256(), default_backend())
        )
        
        # Generate DH parameters (this takes a while)
        logger.info(f"Generating DH parameters for CA {name}...")
        dh_params = self._generate_dh_parameters()
        
        # Generate TLS Auth key
        tls_auth = self._generate_tls_auth_key()
        
        # Serialize to PEM
        ca_cert_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
        ca_key_pem = ca_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')
        
        # Create CA record
        ca = CertificateAuthority.objects.create(
            name=name,
            common_name=common_name,
            organization=organization,
            country=country,
            ca_certificate=ca_cert_pem,
            ca_private_key=ca_key_pem,
            dh_parameters=dh_params,
            tls_auth_key=tls_auth,
            valid_from=timezone.make_aware(valid_from),
            valid_until=timezone.make_aware(valid_until),
            validity_days=validity_days,
            is_active=True
        )
        
        logger.info(f"Created Certificate Authority: {ca.name}")
        return ca
    
    def _generate_dh_parameters(self, key_size: int = 2048) -> str:
        """
        Generate Diffie-Hellman parameters.
        Uses OpenSSL for faster generation.
        """
        try:
            # Try using OpenSSL CLI (much faster)
            result = subprocess.run(
                ['openssl', 'dhparam', '-out', '-', str(key_size)],
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            if result.returncode == 0:
                return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        # Fallback: Use cryptography library (slower)
        from cryptography.hazmat.primitives.asymmetric import dh
        from cryptography.hazmat.primitives.serialization import Encoding, ParameterFormat
        
        parameters = dh.generate_parameters(generator=2, key_size=key_size)
        return parameters.parameter_bytes(Encoding.PEM, ParameterFormat.PKCS3).decode('utf-8')
    
    def _generate_tls_auth_key(self) -> str:
        """Generate OpenVPN TLS Auth key"""
        try:
            result = subprocess.run(
                ['openvpn', '--genkey', 'secret', '/dev/stdout'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        
        # Fallback: Generate random key manually
        import secrets
        key_bytes = secrets.token_hex(256)
        lines = [key_bytes[i:i+32] for i in range(0, len(key_bytes), 32)]
        return (
            "-----BEGIN OpenVPN Static key V1-----\n" +
            "\n".join(lines) +
            "\n-----END OpenVPN Static key V1-----\n"
        )
    
    # ────────────────────────────────────────────────────────────────
    # SERVER CERTIFICATE
    # ────────────────────────────────────────────────────────────────
    
    def generate_server_certificate(
        self,
        ca: CertificateAuthority,
        common_name: str,
        validity_days: int = None
    ) -> VPNCertificate:
        """
        Generate a server certificate signed by the CA.
        
        Args:
            ca: Certificate Authority to sign with
            common_name: Server common name
            validity_days: Certificate validity
            
        Returns:
            VPNCertificate instance
        """
        validity_days = validity_days or self.SERVER_VALIDITY_DAYS
        
        # Load CA certificate and key
        ca_cert = x509.load_pem_x509_certificate(
            ca.ca_certificate.encode('utf-8'),
            default_backend()
        )
        ca_key = serialization.load_pem_private_key(
            ca.ca_private_key.encode('utf-8'),
            password=None,
            backend=default_backend()
        )
        
        # Generate server key
        server_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=self.RSA_KEY_SIZE,
            backend=default_backend()
        )
        
        # Build server certificate
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, ca.country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ca.organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        
        valid_from = datetime.utcnow()
        valid_until = valid_from + timedelta(days=validity_days)
        serial = int(generate_serial_number(), 16)
        
        server_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(server_key.public_key())
            .serial_number(serial)
            .not_valid_before(valid_from)
            .not_valid_after(valid_until)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    key_cert_sign=False,
                    crl_sign=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False
                ),
                critical=True
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
                critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False
            )
            .sign(ca_key, hashes.SHA256(), default_backend())
        )
        
        # Serialize to PEM
        cert_pem = server_cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
        key_pem = server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')
        
        # Create certificate record
        vpn_cert = VPNCertificate.objects.create(
            ca=ca,
            common_name=common_name,
            certificate_type='server',
            serial_number=hex(serial)[2:].upper(),
            certificate=cert_pem,
            private_key=key_pem,
            valid_from=timezone.make_aware(valid_from),
            valid_until=timezone.make_aware(valid_until),
            validity_days=validity_days,
            status='active'
        )
        
        logger.info(f"Generated server certificate: {common_name}")
        return vpn_cert
    
    # ────────────────────────────────────────────────────────────────
    # CLIENT CERTIFICATE
    # ────────────────────────────────────────────────────────────────
    
    def generate_client_certificate(
        self,
        ca: CertificateAuthority,
        router,
        common_name: str = None,
        validity_days: int = None
    ) -> VPNCertificate:
        """
        Generate a client certificate for a router.
        
        Args:
            ca: Certificate Authority to sign with
            router: Router model instance
            common_name: Client CN (defaults to router name)
            validity_days: Certificate validity
            
        Returns:
            VPNCertificate instance
        """
        validity_days = validity_days or self.CLIENT_VALIDITY_DAYS
        common_name = common_name or f"router_{router.id}"
        
        # Load CA certificate and key
        ca_cert = x509.load_pem_x509_certificate(
            ca.ca_certificate.encode('utf-8'),
            default_backend()
        )
        ca_key = serialization.load_pem_private_key(
            ca.ca_private_key.encode('utf-8'),
            password=None,
            backend=default_backend()
        )
        
        # Generate client key
        client_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=self.RSA_KEY_SIZE,
            backend=default_backend()
        )
        
        # Build client certificate
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, ca.country),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ca.organization),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ])
        
        valid_from = datetime.utcnow()
        valid_until = valid_from + timedelta(days=validity_days)
        serial = int(generate_serial_number(), 16)
        
        client_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(client_key.public_key())
            .serial_number(serial)
            .not_valid_before(valid_from)
            .not_valid_after(valid_until)
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    content_commitment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    encipher_only=False,
                    decipher_only=False
                ),
                critical=True
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(client_key.public_key()),
                critical=False
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                critical=False
            )
            .sign(ca_key, hashes.SHA256(), default_backend())
        )
        
        # Serialize to PEM
        cert_pem = client_cert.public_bytes(serialization.Encoding.PEM).decode('utf-8')
        key_pem = client_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ).decode('utf-8')
        
        # Create certificate record
        vpn_cert = VPNCertificate.objects.create(
            ca=ca,
            router=router,
            common_name=common_name,
            certificate_type='client',
            serial_number=hex(serial)[2:].upper(),
            certificate=cert_pem,
            private_key=key_pem,
            valid_from=timezone.make_aware(valid_from),
            valid_until=timezone.make_aware(valid_until),
            validity_days=validity_days,
            status='active'
        )
        
        logger.info(f"Generated client certificate for router: {router.name}")
        return vpn_cert
    
    # ────────────────────────────────────────────────────────────────
    # CERTIFICATE REVOCATION
    # ────────────────────────────────────────────────────────────────
    
    def revoke_certificate(
        self,
        certificate: VPNCertificate,
        reason: str = "Revoked by administrator"
    ) -> bool:
        """
        Revoke a certificate.
        
        Args:
            certificate: Certificate to revoke
            reason: Revocation reason
            
        Returns:
            True if revoked successfully
        """
        if certificate.status == 'revoked':
            logger.warning(f"Certificate {certificate.common_name} is already revoked")
            return False
        
        certificate.revoke(reason)
        logger.info(f"Revoked certificate: {certificate.common_name} - {reason}")
        return True
    
    def generate_crl(self, ca: CertificateAuthority) -> str:
        """
        Generate Certificate Revocation List for a CA.
        
        Args:
            ca: Certificate Authority
            
        Returns:
            PEM-encoded CRL
        """
        # Load CA key
        ca_key = serialization.load_pem_private_key(
            ca.ca_private_key.encode('utf-8'),
            password=None,
            backend=default_backend()
        )
        
        ca_cert = x509.load_pem_x509_certificate(
            ca.ca_certificate.encode('utf-8'),
            default_backend()
        )
        
        # Build CRL
        builder = x509.CertificateRevocationListBuilder()
        builder = builder.issuer_name(ca_cert.subject)
        builder = builder.last_update(datetime.utcnow())
        builder = builder.next_update(datetime.utcnow() + timedelta(days=7))
        
        # Add revoked certificates
        revoked_certs = ca.certificates.filter(status='revoked')
        for cert in revoked_certs:
            revoked = x509.RevokedCertificateBuilder().serial_number(
                int(cert.serial_number, 16)
            ).revocation_date(
                cert.revoked_at or timezone.now()
            ).build()
            builder = builder.add_revoked_certificate(revoked)
        
        crl = builder.sign(ca_key, hashes.SHA256(), default_backend())
        
        return crl.public_bytes(serialization.Encoding.PEM).decode('utf-8')
    
    # ────────────────────────────────────────────────────────────────
    # OPENVPN CONFIG GENERATION
    # ────────────────────────────────────────────────────────────────
    
    def generate_client_config(
        self,
        certificate: VPNCertificate,
        server_address: str,
        server_port: int = 1194,
        protocol: str = 'udp'
    ) -> str:
        """
        Generate OpenVPN client configuration file.
        
        Args:
            certificate: Client certificate
            server_address: VPN server address
            server_port: VPN server port
            protocol: UDP or TCP
            
        Returns:
            OpenVPN client config content
        """
        ca = certificate.ca
        
        config = f"""# Netily VPN Client Configuration
# Generated for: {certificate.common_name}
# Router: {certificate.router.name if certificate.router else 'N/A'}
# Generated at: {timezone.now().isoformat()}

client
dev tun
proto {protocol}
remote {server_address} {server_port}

resolv-retry infinite
nobind
persist-key
persist-tun

# Security
cipher AES-256-GCM
auth SHA256
key-direction 1
remote-cert-tls server

# Logging
verb 3
mute 20

# Certificates
<ca>
{ca.ca_certificate}
</ca>

<cert>
{certificate.certificate}
</cert>

<key>
{certificate.private_key}
</key>

<tls-auth>
{ca.tls_auth_key}
</tls-auth>
"""
        return config
    
    def generate_server_config(
        self,
        server_cert: VPNCertificate,
        vpn_network: str = "10.8.0.0/24",
        port: int = 1194,
        protocol: str = 'udp'
    ) -> str:
        """
        Generate OpenVPN server configuration.
        
        Args:
            server_cert: Server certificate
            vpn_network: VPN subnet
            port: Server port
            protocol: UDP or TCP
            
        Returns:
            OpenVPN server config content
        """
        ca = server_cert.ca
        network, mask = vpn_network.rsplit('/', 1)
        
        # Convert CIDR to netmask
        cidr = int(mask)
        netmask = '.'.join([str((0xffffffff << (32 - cidr) >> i) & 0xff) for i in [24, 16, 8, 0]])
        
        config = f"""# Netily OpenVPN Server Configuration
# Generated at: {timezone.now().isoformat()}

port {port}
proto {protocol}
dev tun

# Certificates
ca /etc/openvpn/ca.crt
cert /etc/openvpn/server.crt
key /etc/openvpn/server.key
dh /etc/openvpn/dh.pem
tls-auth /etc/openvpn/ta.key 0

# Network
server {network} {netmask}
topology subnet

# Push routes to clients
push "redirect-gateway def1 bypass-dhcp"
push "dhcp-option DNS 8.8.8.8"
push "dhcp-option DNS 8.8.4.4"

# Keep clients alive
keepalive 10 120

# Security
cipher AES-256-GCM
auth SHA256
user nobody
group nogroup

# Persist across restarts
persist-key
persist-tun

# Status and logging
status /var/log/openvpn/status.log 10
log-append /var/log/openvpn/openvpn.log
verb 3
mute 20

# Management interface
management localhost 7505

# Client config directory
client-config-dir /etc/openvpn/ccd

# Duplicate CN allowed (for testing)
;duplicate-cn

# Max clients
max-clients 100
"""
        return config
