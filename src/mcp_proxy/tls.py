"""Auto-generated self-signed certificates for the default HTTPS listener.

MCPy serves HTTPS by default. When the operator hasn't supplied a
certificate (no ``--ssl-certfile`` / ``--ssl-keyfile`` on the CLI and
no ``tls.certfile`` / ``tls.keyfile`` in the config), :func:`cmd_serve`
calls :func:`ensure_dev_cert` to get a cached self-signed cert for
``localhost`` / ``127.0.0.1`` / ``::1`` from the state directory. The
first call generates the cert and key; subsequent calls reuse them.

This is explicitly a *development* cert — clients need to pass ``-k``
to curl or trust the cert via their OS keychain. Production deployments
should pass real cert paths via ``--ssl-certfile`` / ``--ssl-keyfile``
or set ``tls.enabled=true`` with ``tls.certfile`` / ``tls.keyfile`` in
the config.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

_DEV_CERT_SUBDIR = "tls"
_DEV_CERT_FILENAME = "cert.pem"
_DEV_KEY_FILENAME = "key.pem"
# 10 years — this is a dev cert reused across restarts, not something
# that should pester operators with yearly regeneration.
_VALIDITY_DAYS = 3650


def ensure_dev_cert(state_dir: Path) -> tuple[str, str]:
    """Return ``(certfile, keyfile)`` for the auto-generated dev cert.

    If ``<state_dir>/tls/cert.pem`` and ``<state_dir>/tls/key.pem``
    already exist they're reused as-is. Otherwise a fresh self-signed
    RSA-2048 certificate is generated, written to disk (key mode 0600),
    and its paths returned. The cert includes a SAN for ``localhost``,
    ``127.0.0.1``, and ``::1`` so the proxy can be reached over the
    loopback without a hostname mismatch warning.
    """
    tls_dir = Path(state_dir) / _DEV_CERT_SUBDIR
    cert_path = tls_dir / _DEV_CERT_FILENAME
    key_path = tls_dir / _DEV_KEY_FILENAME

    if cert_path.is_file() and key_path.is_file():
        return str(cert_path), str(key_path)

    tls_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MCPy"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Development"),
        ]
    )
    now = _dt.datetime.now(_dt.timezone.utc)
    san = x509.SubjectAlternativeName(
        [
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
            x509.IPAddress(ipaddress.IPv6Address("::1")),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(minutes=1))
        .not_valid_after(now + _dt.timedelta(days=_VALIDITY_DAYS))
        .add_extension(san, critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(private_key=key, algorithm=hashes.SHA256())
    )

    cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
    key_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    cert_path.write_bytes(cert_bytes)
    key_path.write_bytes(key_bytes)
    try:
        os.chmod(key_path, 0o600)
    except OSError:  # pragma: no cover - best-effort on non-POSIX filesystems
        pass

    logger.info(
        "auto-generated self-signed TLS cert at %s (valid %d days)",
        cert_path,
        _VALIDITY_DAYS,
    )
    return str(cert_path), str(key_path)
