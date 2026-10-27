"""Operations layer for a single-group hosted Web Lab.

Before another research group can use an instance, three things are needed that
are not multi-tenancy: knowing who is at the door, keeping the traffic on TLS,
and being able to restore what was lost. This module holds those three pieces so
that ``serve_web_lab.py`` stays a thin command.

Deliberately absent: accounts, roles, per-project isolation and quotas. Those are
a different step, and pretending that a shared password is an account model would
be worse than saying plainly that it is not.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import tarfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

BACKUP_SCHEMA = "0.1"
INSTANCE_SCHEMA = "0.1"
MANIFEST_NAME = "manifest.json"
FILES_PREFIX = "files"
PROBE_PATH = "/api/lab/health"
PROBE_TIMEOUT = 3.0
INSTANCE_DIRNAME = "lab_instances"
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_USERNAME = re.compile(r"^[A-Za-z0-9._-]{2,32}$")

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback(host: str) -> bool:
    """True when the bind address cannot be reached from another machine."""

    candidate = (host or "").strip()
    if candidate in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class LabBasicAuth:
    """HTTP Basic credentials for one shared instance.

    Basic auth over TLS is a deliberate fit for "a handful of researchers in the
    same group", not for an institution: there is no account lifecycle, no
    revocation per user beyond editing a file, and no way to attribute an action
    to a person who shares a password. The access log records the username, so
    the instance can at least say under which name a run happened.
    """

    users: Mapping[str, str]

    @classmethod
    def from_file(cls, path: Path) -> LabBasicAuth:
        users: dict[str, str] = {}
        for line_number, raw in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), start=1
        ):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                raise ValueError(f"{Path(path).name}:{line_number}: expected user:password")
            user, _, password = line.partition(":")
            user = user.strip()
            if not user or not password:
                raise ValueError(f"{Path(path).name}:{line_number}: empty user or password")
            users[user] = password
        if not users:
            raise ValueError(f"{Path(path).name}: no credentials declared")
        return cls(users=users)

    @classmethod
    def from_spec(cls, spec: str) -> LabBasicAuth:
        """Build from ``user:password``, refusing an empty side."""

        user, separator, password = (spec or "").partition(":")
        if not separator or not user.strip() or not password:
            raise ValueError("credentials must be user:password")
        return cls(users={user.strip(): password})

    def check(self, header: str | None) -> str | None:
        """Return the authenticated user, or None when the header does not match."""

        if not header:
            return None
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        user, separator, password = decoded.partition(":")
        if not separator:
            return None
        expected = self.users.get(user)
        if expected is None:
            return None
        # Compare both sides in constant time; a length leak is not a secret
        # here, but the comparison style should not become a template.
        if not hmac.compare_digest(expected.encode("utf-8"), password.encode("utf-8")):
            return None
        return user


class LabAccessLog:
    """Append-only JSONL log of the requests an instance served.

    One line per response, including refusals: a 401 and a 409 are as much part
    of the record of who tried what as a successful run.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(
        self,
        *,
        method: str,
        path: str,
        status: int,
        remote: str,
        user: str | None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "method": method,
            "path": path,
            "status": int(status),
            "remote": remote,
            "user": user,
        }
        if extra:
            entry["extra"] = dict(extra)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def build_tls_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Server-side TLS context, refusing anything below TLS 1.2."""

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


@dataclass(frozen=True)
class LeafStatus:
    """How long the instance certificate still has, in plain terms."""

    cert: Path
    subject: str
    issuer: str
    not_after: datetime
    days_remaining: int

    @property
    def expired(self) -> bool:
        return self.not_after <= datetime.now(UTC)

    def notice(self, warn_days: int = 14) -> str | None:
        """The sentence to print, or None when there is time to spare."""

        if self.expired:
            return (
                f"the TLS certificate expired on {self.not_after.isoformat()}: "
                "renew it with `scripts/lab_ops.py renew-leaf` before serving"
            )
        if self.days_remaining <= warn_days:
            return (
                f"the TLS certificate expires in {self.days_remaining} day(s) "
                f"on {self.not_after.isoformat()}: run "
                "`scripts/lab_ops.py renew-leaf <slug>`"
            )
        return None


def leaf_status(cert_path: Path) -> LeafStatus:
    """Read a certificate and say how many whole days are left.

    Counts whole days: a certificate expiring in two hours reports 0, which is
    what an operator needs to read, not a rounding that looks comfortable.
    """

    from cryptography import x509

    path = Path(cert_path)
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    not_after = getattr(certificate, "not_valid_after_utc", None)
    if not_after is None:  # cryptography older than 42
        not_after = certificate.not_valid_after.replace(tzinfo=UTC)
    remaining = (not_after - datetime.now(UTC)).days
    return LeafStatus(
        cert=path,
        subject=certificate.subject.rfc4514_string(),
        issuer=certificate.issuer.rfc4514_string(),
        not_after=not_after,
        days_remaining=remaining,
    )


def generate_self_signed_certificate(
    cert_path: Path,
    key_path: Path,
    *,
    common_name: str = "integrity-lab.local",
    days: int = 365,
    extra_hosts: Sequence[str] = ("localhost", "127.0.0.1", "::1"),
) -> tuple[Path, Path]:
    """Create a self-signed certificate for a pilot instance.

    Useful on an internal network or before a real DNS name exists. It is not a
    substitute for a certificate a browser trusts: the runbook says so, and the
    certificate carries the given names in its SAN so a client can at least pin
    it deliberately.
    """

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    san_entries: list[x509.GeneralName] = []
    for name in extra_hosts:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            san_entries.append(x509.DNSName(name))
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


CA_DIRNAME = "ca"
CA_CERT_NAME = "ca-cert.pem"
CA_KEY_NAME = "ca-key.pem"


@dataclass(frozen=True)
class LocalCertificateAuthority:
    """The one anchor a collaborator installs, and the fingerprints to check it."""

    cert: Path
    key: Path
    fingerprint: str
    common_name: str


def certificate_fingerprint(path: Path) -> str:
    """SHA-256 of the DER certificate, the value a reader can compare by eye."""

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    certificate = x509.load_pem_x509_certificate(Path(path).read_bytes())
    return certificate.fingerprint(hashes.SHA256()).hex()


def ensure_local_ca(
    ca_dir: Path,
    *,
    common_name: str = "Integrity Behavioral Lab local CA",
    days: int = 3650,
) -> LocalCertificateAuthority:
    """Create the instance's own CA once; never replace an existing one.

    A CA that can be regenerated at will is not an anchor: every machine that
    trusted it would keep trusting a key nobody holds. The leaf rotates; the CA
    is moved aside deliberately if it must change.
    """

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_dir = Path(ca_dir)
    cert_path = ca_dir / CA_CERT_NAME
    key_path = ca_dir / CA_KEY_NAME
    if cert_path.exists() and key_path.exists():
        return LocalCertificateAuthority(
            cert=cert_path,
            key=key_path,
            fingerprint=certificate_fingerprint(cert_path),
            common_name=common_name,
        )
    if cert_path.exists() or key_path.exists():
        raise ValueError(
            "incomplete local CA: move the remaining file aside to regenerate"
        )

    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Integrity Labs"),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    ca_dir.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return LocalCertificateAuthority(
        cert=cert_path,
        key=key_path,
        fingerprint=certificate_fingerprint(cert_path),
        common_name=common_name,
    )


def issue_server_certificate(
    ca: LocalCertificateAuthority,
    cert_path: Path,
    key_path: Path,
    *,
    common_name: str,
    extra_hosts: Sequence[str] = ("localhost", "127.0.0.1", "::1"),
    days: int = 90,
) -> tuple[Path, Path]:
    """Sign the instance leaf with the local CA.

    Rotating the leaf this way changes nothing for anyone who trusted the CA:
    that is the reason the anchor and the leaf are separate pieces.
    """

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_certificate = x509.load_pem_x509_certificate(Path(ca.cert).read_bytes())
    ca_key = serialization.load_pem_private_key(Path(ca.key).read_bytes(), password=None)
    if not isinstance(ca_key, rsa.RSAPrivateKey):
        raise ValueError("local CA key must be RSA")
    ca_public = ca_certificate.public_key()
    if not isinstance(ca_public, rsa.RSAPublicKey):
        raise ValueError("local CA certificate must carry an RSA key")
    if ca_public.public_numbers() != ca_key.public_key().public_numbers():
        raise ValueError("local CA certificate and key do not match")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san_entries: list[x509.GeneralName] = []
    for name in extra_hosts:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            san_entries.append(x509.DNSName(name))
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
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
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_public),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def parse_ops_log(path: Path) -> list[dict[str, str]]:
    """Read a pipe-delimited operations log back into fields.

    Lines start with an ISO timestamp and continue as ``key=value`` pairs, the
    shape both the certificate check and the backup job write. Unreadable lines
    are skipped rather than guessed at.
    """

    resolved = Path(path)
    if not resolved.is_file():
        return []
    entries: list[dict[str, str]] = []
    for raw in resolved.read_text(encoding="utf-8").splitlines():
        parts = [part.strip() for part in raw.split("|")]
        if not parts or not parts[0]:
            continue
        entry = {"ts": parts[0]}
        for part in parts[1:]:
            key, separator, value = part.partition("=")
            if separator:
                entry[key.strip()] = value.strip()
        entries.append(entry)
    return entries


def _age_label(stamp: str, now: datetime) -> str:
    """A short, honest distance in time; unreadable stamps say so."""

    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return "con fecha ilegible"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = int((now - moment).total_seconds())
    if seconds < 0:
        return "con fecha futura"
    if seconds < 3600:
        return f"hace {seconds // 60} min"
    if seconds < 86400:
        return f"hace {seconds // 3600} h"
    return f"hace {seconds // 86400} d"


def _probe_reason(exc: BaseException) -> str:
    """A short cause a human can act on, never a raw traceback line."""

    if isinstance(exc, ssl.SSLCertVerificationError):
        return "el certificado no valida"
    if isinstance(exc, socket.gaierror):
        return "el host no resuelve"
    if isinstance(exc, ConnectionRefusedError):
        return "conexión rechazada"
    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
        return "conexión cortada"
    if isinstance(exc, ssl.SSLError):
        return "handshake TLS fallido"
    text = re.sub(r"[|\r\n]+", " ", str(exc)).strip()
    return text or exc.__class__.__name__


@dataclass(frozen=True)
class InstanceProbe:
    """What one unauthenticated request says about the door.

    Both halves matter. A port that answers is alive; a 200 on a protected route
    means authentication is no longer closing it, and no reading of the
    instance's own files can reveal that.
    """

    host: str
    port: int
    status: int | None
    phrase: str
    ok: bool

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


def probe_instance(
    host: str,
    port: int,
    *,
    ca_cert: Path | None = None,
    timeout: float = PROBE_TIMEOUT,
    path: str = PROBE_PATH,
) -> InstanceProbe:
    """Ask the running instance one question it must answer with a refusal.

    The request carries no credentials on purpose, so the expected answer is
    401. A 200, or any other status, means HTTP reached a route without
    authentication having closed it. TLS is verified against the instance's own
    CA when the summary names one; without a CA the probe says it is running
    unverified rather than pretending the certificate was checked.
    """

    if ca_cert is not None:
        context = ssl.create_default_context(cafile=str(ca_cert))
    else:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    connection = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": "integrity-lab-status", "Accept": "*/*"},
        )
        status = connection.getresponse().status
    except (socket.timeout, TimeoutError):
        return InstanceProbe(host, port, None, f"no responde en {timeout:g} s", False)
    except ssl.SSLError as exc:
        return InstanceProbe(
            host, port, None, f"TLS no completa: {_probe_reason(exc)}", False
        )
    except (OSError, http.client.HTTPException) as exc:
        return InstanceProbe(host, port, None, f"no responde: {_probe_reason(exc)}", False)
    finally:
        connection.close()

    if status == 401:
        return InstanceProbe(host, port, status, "responde 401 (auth cerrada)", True)
    if status == 200:
        return InstanceProbe(
            host, port, status, "responde 200 sin credenciales (auth abierta)", False
        )
    return InstanceProbe(host, port, status, f"responde {status} sin credenciales", False)


@dataclass(frozen=True)
class InstanceStatus:
    """One line an operator can read at a glance, and nothing that needs a UI."""

    slug: str
    service: str
    certificate: str
    certificate_days: int | None
    backup: str
    restore: str
    accesses: str
    attention: tuple[str, ...]

    def line(self) -> str:
        return " | ".join(
            [
                self.slug,
                f"servicio: {self.service}",
                f"certificado: {self.certificate}",
                f"copia: {self.backup}",
                f"restauración: {self.restore}",
                self.accesses,
            ]
        )


def instance_status(
    root: Path,
    slug: str,
    *,
    hours: int = 24,
    now: datetime | None = None,
    probe: bool = False,
    probe_host: str | None = None,
    probe_port: int | None = None,
    probe_timeout: float = PROBE_TIMEOUT,
) -> InstanceStatus:
    """Summarise service, certificate, last backup, drill and recent traffic.

    Files record what already happened; they cannot say whether the process is
    alive now or whether the door is still shut. ``probe`` adds one
    unauthenticated request to the instance's own health route and reports what
    came back. It stays off by default so that reading a restored copy never
    requires the original host to be reachable.
    """

    root = Path(root)
    instance_dir = root / "data" / INSTANCE_DIRNAME / slug
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    attention: list[str] = []

    summary: dict[str, Any] = {}
    try:
        loaded = json.loads((instance_dir / "instance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = None
    if isinstance(loaded, dict):
        summary = loaded

    service = "no comprobado"
    probe_at: datetime | None = None
    if probe:
        host = (probe_host or str(summary.get("host") or "")).strip()
        declared = summary.get("port")
        port = probe_port if probe_port is not None else declared
        ca_cert: Path | None = None
        ca_value = summary.get("ca_cert")
        if isinstance(ca_value, str) and ca_value:
            candidate = Path(ca_value)
            candidate = candidate if candidate.is_absolute() else root / candidate
            ca_cert = candidate if candidate.is_file() else None
        if not host or not isinstance(port, int):
            service = "sin host y puerto en instance.json"
            attention.append("servicio")
        else:
            probe_at = moment
            result = probe_instance(host, port, ca_cert=ca_cert, timeout=probe_timeout)
            service = result.phrase
            if not result.ok:
                service = f"{service} ({result.endpoint})"
                attention.append("servicio")

    certificate = "ilegible"
    certificate_days: int | None = None
    try:
        leaf = leaf_status(instance_dir / "tls" / "lab-cert.pem")
    except (OSError, ValueError):
        attention.append("certificado")
    else:
        certificate_days = leaf.days_remaining
        if leaf.expired:
            certificate = f"caducado el {leaf.not_after.date().isoformat()}"
            attention.append("certificado")
        else:
            certificate = (
                f"{leaf.days_remaining} d "
                f"(caduca {leaf.not_after.date().isoformat()})"
            )
            if leaf.days_remaining <= 14:
                attention.append("certificado")

    backup_entries = parse_ops_log(instance_dir / "ops" / "backup.log")
    if backup_entries:
        last = backup_entries[-1]
        state = last.get("status", "desconocido")
        backup = f"{state} {_age_label(last.get('ts', ''), moment)}"
        if state != "ok":
            attention.append("copia")
    else:
        backup = "ninguna"
        attention.append("copia")

    drill_entries = parse_ops_log(instance_dir / "ops" / "restore-drill.log")
    if drill_entries:
        last = drill_entries[-1]
        state = last.get("status", "desconocido")
        checks = last.get("checks")
        restore = f"{state} {_age_label(last.get('ts', ''), moment)}"
        if checks:
            restore = f"{restore} (checks {checks})"
        if state != "ok":
            attention.append("restauración")
    else:
        restore = "ninguna"
        attention.append("restauración")

    cutoff = moment - timedelta(hours=max(hours, 0))
    recent: list[dict[str, Any]] = []
    for entry in LabAccessLog(instance_dir / "access" / "access.jsonl").read():
        stamp = entry.get("ts")
        if not isinstance(stamp, str):
            continue
        try:
            seen = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        if seen < cutoff:
            continue
        # The command's own probe must not answer the question it is asking.
        if (
            probe_at is not None
            and entry.get("path") == PROBE_PATH
            and entry.get("status") in (401, "401")
            and not entry.get("user")
            and seen >= probe_at - timedelta(seconds=1)
        ):
            continue
        recent.append(entry)
    accesses = f"accesos {hours} h: {len(recent)}"
    if recent:
        last = recent[-1]
        who = last.get("user") or "anónimo"
        accesses += (
            f" (último {last.get('status', '?')} de {who} "
            f"{_age_label(str(last.get('ts', '')), moment)})"
        )

    return InstanceStatus(
        slug=slug,
        service=service,
        certificate=certificate,
        certificate_days=certificate_days,
        backup=backup,
        restore=restore,
        accesses=accesses,
        attention=tuple(attention),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(
    root: Path,
    out_dir: Path,
    include: Iterable[Path],
    *,
    created_at: str | None = None,
) -> Path:
    """Archive the instance state with a manifest of per-file digests.

    ``include`` may name files or directories; every path must live under
    ``root`` so the archive cannot silently grow a copy of the filesystem. Keys,
    access logs and exported packages belong here; the repository does not.
    """

    root = Path(root).resolve()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    moment = created_at or datetime.now(UTC).isoformat()
    files: list[tuple[Path, Path]] = []
    for item in include:
        candidate = Path(item)
        candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
        if not candidate.exists():
            continue
        if not candidate.is_relative_to(root):
            raise ValueError(f"backup source escapes the root: {candidate}")
        if candidate.is_file():
            files.append((candidate, candidate.relative_to(root)))
            continue
        for path in sorted(candidate.rglob("*")):
            if path.is_file():
                files.append((path, path.relative_to(root)))
    if not files:
        raise ValueError("nothing to back up: no file under the given paths")

    manifest = {
        "schema_version": BACKUP_SCHEMA,
        "created_at": moment,
        "root": str(root),
        "files": [
            {
                "path": relative.as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path, relative in files
        ],
    }
    stamp = re.sub(r"[^0-9]", "", moment)[:14] or "backup"
    archive = out_dir / f"integrity-lab-backup-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(payload)
        info.mtime = int(datetime.now(UTC).timestamp())
        tar.addfile(info, BytesIO(payload))
        for path, relative in files:
            tar.add(path, arcname=f"{FILES_PREFIX}/{relative.as_posix()}")
    return archive


def verify_backup(archive: Path) -> list[str]:
    """Check an archive against its own manifest. Empty list means it matches."""

    problems: list[str] = []
    try:
        with tarfile.open(Path(archive), "r:gz") as tar:
            members = {member.name: member for member in tar.getmembers()}
            if MANIFEST_NAME not in members:
                return ["archive has no manifest.json"]
            handle = tar.extractfile(members[MANIFEST_NAME])
            if handle is None:
                return ["manifest.json is not a regular file"]
            manifest = json.loads(handle.read().decode("utf-8"))
            declared = {
                str(entry.get("path")): entry
                for entry in manifest.get("files", [])
                if isinstance(entry, dict)
            }
            if not declared:
                problems.append("manifest lists no files")
            for relative, entry in declared.items():
                member_name = f"{FILES_PREFIX}/{relative}"
                member = members.get(member_name)
                if member is None:
                    problems.append(f"missing from the archive: {relative}")
                    continue
                payload = tar.extractfile(member)
                if payload is None:
                    problems.append(f"not a regular file: {relative}")
                    continue
                data = payload.read()
                digest = hashlib.sha256(data).hexdigest()
                if digest != entry.get("sha256"):
                    problems.append(f"digest mismatch: {relative}")
                if len(data) != entry.get("bytes"):
                    problems.append(f"size mismatch: {relative}")
            for name in members:
                if not name.startswith(f"{FILES_PREFIX}/"):
                    continue
                relative = name.removeprefix(f"{FILES_PREFIX}/")
                if relative not in declared:
                    problems.append(f"not listed in the manifest: {relative}")
    except (OSError, ValueError, tarfile.TarError) as exc:
        problems.append(f"archive cannot be read: {exc}")
    return problems


@dataclass(frozen=True)
class LabInstancePlan:
    """Material prepared for one externally shared instance.

    The dataclass deliberately carries paths and the public key id, never the
    generated password: that one lives only in ``credentials`` so it can be
    handed over out of band instead of being copied into logs or summaries.
    """

    slug: str
    user: str
    host: str
    port: int
    key_id: str
    directory: Path
    key_dir: Path
    tls_cert: Path
    tls_key: Path
    ca_cert: Path
    ca_fingerprint: str
    credentials: Path
    access_log: Path
    summary: Path

    def serve_command(self) -> str:
        """The exact command that opens this instance, TLS-only and authenticated."""

        return (
            "python scripts/serve_web_lab.py "
            f"--host {self.host} --port {self.port} "
            f"--tls-cert {self.tls_cert} --tls-key {self.tls_key} "
            f"--basic-auth-file {self.credentials} "
            f"--access-log {self.access_log} "
            f"--signing-key-dir {self.key_dir} "
            "--attributable-signing"
        )


def provision_instance(
    root: Path,
    slug: str,
    *,
    user: str,
    host: str,
    port: int = 8910,
    days: int = 90,
    created_at: str | None = None,
) -> LabInstancePlan:
    """Create the state one external researcher needs, and nothing else.

    Produces an attributable Ed25519 key pair, one credential line with a
    generated password, a local CA with a leaf signed for the given host, and a
    summary that records the public key id and the CA fingerprint. It refuses to
    reuse an existing directory: regenerating a key would invalidate every seal
    already issued and silently change the identity a collaborator pinned.
    """

    from autopilot.lab_evidence import ensure_lab_keypair, signing_key_info

    slug = (slug or "").strip().lower()
    if not _SLUG.match(slug):
        raise ValueError("slug must be 2-41 characters of a-z, 0-9 and '-'")
    user = (user or "").strip()
    if not _USERNAME.match(user):
        raise ValueError("user must be 2-32 characters of letters, digits, '.', '_' or '-'")
    if not (host or "").strip():
        raise ValueError("host is required")
    if not 0 < port < 65536:
        raise ValueError("port must be between 1 and 65535")

    root = Path(root).resolve()
    directory = root / "data" / INSTANCE_DIRNAME / slug
    if directory.exists():
        raise ValueError(f"instance already provisioned: {directory}")

    key_dir = directory / "keys"
    tls_dir = directory / "tls"
    credentials = directory / "users.txt"
    access_log = directory / "access" / "access.jsonl"
    summary = directory / "instance.json"

    key_dir.mkdir(parents=True)
    tls_dir.mkdir(parents=True)
    access_log.parent.mkdir(parents=True)
    ensure_lab_keypair(key_dir)
    key_id = signing_key_info(key_dir)["key_id"]

    password = secrets.token_urlsafe(18)
    credentials.write_text(
        f"# instancia {slug} · una sola persona · {user}\n{user}:{password}\n",
        encoding="utf-8",
    )
    try:
        os.chmod(credentials, 0o600)
    except OSError:
        pass

    ca = ensure_local_ca(tls_dir / CA_DIRNAME)
    tls_cert, tls_key = issue_server_certificate(
        ca,
        tls_dir / "lab-cert.pem",
        tls_dir / "lab-key.pem",
        common_name=host,
        days=days,
        extra_hosts=[host, "localhost", "127.0.0.1"],
    )

    moment = created_at or datetime.now(UTC).isoformat()
    summary.write_text(
        json.dumps(
            {
                "schema_version": INSTANCE_SCHEMA,
                "created_at": moment,
                "slug": slug,
                "user": user,
                "host": host,
                "port": port,
                "attributable_signing": True,
                "key_id": key_id,
                "key_dir": key_dir.relative_to(root).as_posix(),
                "tls_cert": tls_cert.relative_to(root).as_posix(),
                "tls_key": tls_key.relative_to(root).as_posix(),
                "ca_cert": ca.cert.relative_to(root).as_posix(),
                "ca_fingerprint": ca.fingerprint,
                "credentials": credentials.relative_to(root).as_posix(),
                "access_log": access_log.relative_to(root).as_posix(),
                "note": (
                    "The password lives only in users.txt; hand it over out of band. "
                    "The key id is the public identity a reader can pin."
                ),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return LabInstancePlan(
        slug=slug,
        user=user,
        host=host,
        port=port,
        key_id=key_id,
        directory=directory,
        key_dir=key_dir,
        tls_cert=tls_cert,
        tls_key=tls_key,
        ca_cert=ca.cert,
        ca_fingerprint=ca.fingerprint,
        credentials=credentials,
        access_log=access_log,
        summary=summary,
    )


def renew_instance_leaf(
    instance_dir: Path,
    *,
    days: int = 90,
    host: str | None = None,
    ca_common_name: str = "Integrity Behavioral Lab local CA",
) -> tuple[Path, Path, str]:
    """Reissue the instance leaf from its own CA, leaving the signing key alone.

    Used when a self-signed instance adopts a CA, when the leaf expires, or when
    the instance moves to another name. The attributable Ed25519 key and every
    seal it issued are untouched: only the TLS identity changes.
    """

    instance_dir = Path(instance_dir)
    summary_path = instance_dir / "instance.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read instance summary {summary_path}: {exc}") from exc
    if summary.get("schema_version") != INSTANCE_SCHEMA:
        raise ValueError("unsupported instance summary schema")

    resolved_host = host or str(summary.get("host") or "")
    if not resolved_host:
        raise ValueError("the instance summary declares no host")

    tls_dir = instance_dir / "tls"
    ca = ensure_local_ca(tls_dir / CA_DIRNAME, common_name=ca_common_name)
    cert_path, key_path = issue_server_certificate(
        ca,
        tls_dir / "lab-cert.pem",
        tls_dir / "lab-key.pem",
        common_name=resolved_host,
        days=days,
        extra_hosts=[resolved_host, "localhost", "127.0.0.1"],
    )

    summary["host"] = resolved_host
    try:
        summary["ca_cert"] = ca.cert.relative_to(instance_dir.parents[2]).as_posix()
    except (IndexError, ValueError):
        summary["ca_cert"] = ca.cert.as_posix()
    summary["ca_fingerprint"] = ca.fingerprint
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return cert_path, key_path, certificate_fingerprint(cert_path)


__all__ = [
    "BACKUP_SCHEMA",
    "INSTANCE_SCHEMA",
    "PROBE_TIMEOUT",
    "InstanceProbe",
    "InstanceStatus",
    "LeafStatus",
    "LocalCertificateAuthority",
    "instance_status",
    "parse_ops_log",
    "probe_instance",
    "certificate_fingerprint",
    "leaf_status",
    "ensure_local_ca",
    "issue_server_certificate",
    "renew_instance_leaf",
    "LabAccessLog",
    "LabBasicAuth",
    "LabInstancePlan",
    "provision_instance",
    "build_tls_context",
    "create_backup",
    "generate_self_signed_certificate",
    "is_loopback",
    "verify_backup",
]
