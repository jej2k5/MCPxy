"""Tests for the auto-generated self-signed dev cert helper."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mcp_proxy.tls import ensure_dev_cert


def test_ensure_dev_cert_generates_cert_and_key(tmp_path: Path) -> None:
    certfile, keyfile = ensure_dev_cert(tmp_path)

    assert Path(certfile).is_file()
    assert Path(keyfile).is_file()
    # Both live under <state_dir>/tls/ so they're easy to clean up /
    # mount into a container.
    assert Path(certfile).parent == tmp_path / "tls"
    assert Path(keyfile).parent == tmp_path / "tls"


def test_ensure_dev_cert_reuses_existing_files(tmp_path: Path) -> None:
    certfile, keyfile = ensure_dev_cert(tmp_path)
    cert_mtime = Path(certfile).stat().st_mtime_ns
    key_mtime = Path(keyfile).stat().st_mtime_ns
    original_cert_bytes = Path(certfile).read_bytes()

    certfile2, keyfile2 = ensure_dev_cert(tmp_path)

    # Second call is a no-op: same paths, same bytes, same mtimes.
    assert (certfile2, keyfile2) == (certfile, keyfile)
    assert Path(certfile).stat().st_mtime_ns == cert_mtime
    assert Path(keyfile).stat().st_mtime_ns == key_mtime
    assert Path(certfile).read_bytes() == original_cert_bytes


def test_ensure_dev_cert_has_localhost_san(tmp_path: Path) -> None:
    certfile, _ = ensure_dev_cert(tmp_path)
    cert = x509.load_pem_x509_certificate(Path(certfile).read_bytes())

    # Common name is localhost.
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "localhost"

    # SAN covers the loopback hostnames / IPs MCPy actually binds to.
    san = cert.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    dns_names = san.get_values_for_type(x509.DNSName)
    ip_addrs = san.get_values_for_type(x509.IPAddress)
    assert "localhost" in dns_names
    assert ipaddress.IPv4Address("127.0.0.1") in ip_addrs
    assert ipaddress.IPv6Address("::1") in ip_addrs


def test_ensure_dev_cert_is_server_auth_only(tmp_path: Path) -> None:
    certfile, _ = ensure_dev_cert(tmp_path)
    cert = x509.load_pem_x509_certificate(Path(certfile).read_bytes())

    eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in list(eku)
    # Not a CA — just a leaf cert for the TLS listener.
    bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert bc.ca is False
