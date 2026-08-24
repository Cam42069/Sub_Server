"""Self-signed certificate generation for the HTTPS listener.

The server is meant for a local network, where no public CA will issue a
certificate.  On first run it mints its own, valid for the machine's hostname
and every non-loopback IPv4 address it can find, so browsers on the LAN can at
least verify the address that was typed.  Browsers still warn about the unknown
issuer -- import ``data/certs/server.crt`` as a trusted root to silence that.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import os
import socket
from pathlib import Path

log = logging.getLogger(__name__)

VALID_DAYS = 825  # the maximum most browsers accept for a leaf certificate


def local_addresses() -> list[str]:
    """Best-effort list of this machine's own IPv4 addresses."""
    found: set[str] = {"127.0.0.1"}
    hostname = socket.gethostname()
    for candidate in (hostname, f"{hostname}.local"):
        try:
            for info in socket.getaddrinfo(candidate, None, socket.AF_INET):
                found.add(info[4][0])
        except socket.gaierror:
            continue
    # Ask the routing table which address would be used to reach the network.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: routable-looking, never reached
        found.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(found)


def ensure_certificate(cert_path: str | Path, key_path: str | Path, extra_names: list[str] | None = None) -> tuple[Path, Path]:
    """Return ``(cert, key)``, generating a self-signed pair if either is missing."""
    cert_path, key_path = Path(cert_path), Path(key_path)
    if cert_path.is_file() and key_path.is_file():
        return cert_path, key_path

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except Exception as exc:  # noqa: BLE001 - a broken install raises more than ImportError
        log.warning("cryptography unavailable (%s); falling back to the openssl command", exc)
        return _openssl_certificate(cert_path, key_path, extra_names)

    hostname = socket.gethostname()
    names: list[str] = ["localhost", hostname, f"{hostname}.local"]
    names.extend(extra_names or [])

    alt_names: list = []
    seen: set[str] = set()
    for name in names:
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            alt_names.append(x509.DNSName(name))
    for address in local_addresses():
        if address in seen:
            continue
        seen.add(address)
        alt_names.append(x509.IPAddress(ipaddress.ip_address(address)))
    alt_names.append(x509.IPAddress(ipaddress.ip_address("::1")))

    log.info("generating a self-signed certificate for: %s", ", ".join(sorted(seen)))
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64] or "sub-server"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sub_Server"),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))  # tolerate small clock skew
        .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, content_commitment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.ObjectIdentifier("1.3.6.1.5.5.7.3.1")]),  # serverAuth
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(key_path, 0o600)  # the private key must not be world-readable
    except OSError:
        pass
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def _openssl_certificate(cert_path: Path, key_path: Path, extra_names: list[str] | None) -> tuple[Path, Path]:
    """Generate the certificate with the openssl command line instead.

    Used only when the ``cryptography`` package is missing or broken, so a
    machine with openssl installed can still start an HTTPS listener.
    """
    import shutil
    import subprocess
    import tempfile

    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError(
            "Cannot create an HTTPS certificate: neither the 'cryptography' package nor the\n"
            "'openssl' command is available.\n"
            "  Install one:   pip install cryptography\n"
            "  Or supply your own certificate via [server] cert_file / key_file in config.toml."
        )

    hostname = socket.gethostname()
    entries: list[str] = []
    dns_i = ip_i = 0
    seen: set[str] = set()
    for name in ["localhost", hostname, f"{hostname}.local", *(extra_names or []), *local_addresses()]:
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            ipaddress.ip_address(name)
        except ValueError:
            dns_i += 1
            entries.append(f"DNS.{dns_i} = {name}")
        else:
            ip_i += 1
            entries.append(f"IP.{ip_i} = {name}")

    config = (
        "[req]\ndistinguished_name = dn\nx509_extensions = ext\nprompt = no\n"
        f"[dn]\nCN = {hostname[:64] or 'sub-server'}\nO = Sub_Server\n"
        "[ext]\nbasicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\nextendedKeyUsage = serverAuth\n"
        "subjectAltName = @alt\n[alt]\n" + "\n".join(entries) + "\n"
    )
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False) as handle:
        handle.write(config)
        config_path = handle.name
    try:
        subprocess.run(
            [openssl, "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256",
             "-days", str(VALID_DAYS), "-keyout", str(key_path), "-out", str(cert_path),
             "-config", config_path],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"openssl failed to create a certificate: {exc.stderr.decode(errors='replace')}") from exc
    finally:
        try:
            os.unlink(config_path)
        except OSError:
            pass
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    log.info("generated a self-signed certificate with openssl for: %s", ", ".join(sorted(seen)))
    return cert_path, key_path
