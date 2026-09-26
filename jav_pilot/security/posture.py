from __future__ import annotations

import ipaddress
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path


INSECURE_REMOTE_ENV = "JAV_PILOT_ALLOW_INSECURE_REMOTE"
TRUSTED_PROXY_ENV = "JAV_PILOT_TRUSTED_PROXY_CIDRS"
MIN_PASSWORD_LENGTH = 12
MIN_SECRET_LENGTH = 32
BASELINE_CAPABILITY_MASK = (1 << 0) | (1 << 1) | (1 << 3)
MAX_PROC_STATUS_BYTES = 256 * 1024
MAX_PROC_NET_BYTES = 2 * 1024 * 1024
MAX_PROC_NET_ROWS = 4096
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_WEAK_PASSWORDS = frozenset(
    {
        "admin",
        "changeme",
        "change-this-password",
        "replace-with-unique-password-32chars",
        "jav-pilot",
        "password",
        "password123",
        "qwerty123",
    }
)


class SecurityPostureError(RuntimeError):
    pass


@dataclass(frozen=True)
class SecurityFinding:
    code: str
    severity: str
    message: str
    remediation: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class SecurityPosture:
    safe: bool
    startup_allowed: bool
    override_active: bool
    findings: tuple[SecurityFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "safe": self.safe,
            "startup_allowed": self.startup_allowed,
            "override_active": self.override_active,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def validate_new_password(password: object, username: object = "") -> str:
    clean = str(password or "")
    if len(clean) < MIN_PASSWORD_LENGTH:
        raise SecurityPostureError(
            f"password must contain at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(clean) > 512:
        raise SecurityPostureError("password must contain at most 512 characters")
    folded = clean.casefold()
    if folded in _WEAK_PASSWORDS or folded == str(username or "").strip().casefold():
        raise SecurityPostureError("password is a known weak or account-derived value")
    return clean


def validate_session_secret(secret: object, password_material: object = "") -> str:
    clean = str(secret or "").strip()
    if len(clean) < MIN_SECRET_LENGTH:
        raise SecurityPostureError(
            f"session secret must contain at least {MIN_SECRET_LENGTH} characters"
        )
    if password_material and clean == str(password_material):
        raise SecurityPostureError("session secret must be independent from the password")
    return clean


def evaluate_security_posture(
    *,
    host: object,
    auth_enabled: bool,
    auth_configured: bool,
    secret_persistent: bool,
    plain_password: str = "",
    config_path: Path | None = None,
    environ: dict[str, str] | None = None,
    listen_port: object | None = None,
    effective_capabilities: object | None = None,
    listening_ports: object | None = None,
) -> SecurityPosture:
    environment = os.environ if environ is None else environ
    remote = not is_loopback_host(host)
    override_active = str(environment.get(INSECURE_REMOTE_ENV, "")).strip().lower() in _TRUE_VALUES
    findings: list[SecurityFinding] = []

    if remote and (not auth_enabled or not auth_configured):
        findings.append(
            SecurityFinding(
                "remote_auth_required",
                "critical",
                "非本机监听尚未启用完整鉴权。",
                "配置 WebUI 密码与独立会话密钥，或仅监听 127.0.0.1。",
            )
        )
    if auth_enabled and not secret_persistent:
        findings.append(
            SecurityFinding(
                "persistent_secret_required",
                "critical",
                "会话密钥未持久化，登录会话无法可靠验证。",
                "设置独立且至少 32 字符的 JAV_PILOT_AUTH_SECRET。",
            )
        )
    if plain_password:
        try:
            validate_new_password(plain_password)
        except SecurityPostureError:
            findings.append(
                SecurityFinding(
                    "weak_plaintext_password",
                    "critical",
                    "环境变量中的 WebUI 密码不符合生产强度要求。",
                    "改用至少 12 字符且非示例值的密码，并通过设置页迁移为哈希。",
                )
            )
    if config_path is not None and config_path.exists() and not _private_file(config_path):
        findings.append(
            SecurityFinding(
                "config_permissions",
                "warning",
                "运行配置可能被同机其他账号读取。",
                "将配置文件权限限制为仅服务账号可读写。",
            )
        )
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        findings.append(
            SecurityFinding(
                "container_root",
                "warning",
                "服务当前以 root 身份运行。",
                "完成 NAS 目录 ACL 审计后切换到专用非 root UID，不要递归修改媒体库所有权。",
            )
        )

    capability_mask = (
        _linux_effective_capabilities()
        if effective_capabilities is None
        else _capability_mask(effective_capabilities)
    )
    if (
        capability_mask is not None
        and capability_mask & ~BASELINE_CAPABILITY_MASK
    ):
        findings.append(
            SecurityFinding(
                "unexpected_capabilities",
                "warning",
                "容器进程拥有超出 JAV Pilot 基线的 Linux capability。",
                "保持 cap_drop=ALL，并只恢复 CHOWN、DAC_OVERRIDE 与 FOWNER。",
            )
        )

    clean_port = _listen_port(listen_port)
    if remote and clean_port is not None and clean_port < 1024:
        findings.append(
            SecurityFinding(
                "privileged_listen_port",
                "warning",
                "服务正在非回环地址使用特权端口。",
                "改用非特权容器端口，并由受控反向代理提供外部端口。",
            )
        )
    if clean_port is not None:
        actual_ports = (
            _linux_exposed_tcp_ports()
            if listening_ports is None
            else _listening_port_set(listening_ports)
        )
        if actual_ports is not None and actual_ports - {clean_port}:
            findings.append(
                SecurityFinding(
                    "unexpected_listening_ports",
                    "warning",
                    "容器网络命名空间存在未纳入 JAV Pilot 基线的非回环监听端口。",
                    "检查容器进程与端口映射，只保留声明的 WebUI 监听端口。",
                )
            )

    critical = any(item.severity == "critical" for item in findings)
    startup_allowed = not critical or override_active
    return SecurityPosture(
        safe=not findings,
        startup_allowed=startup_allowed,
        override_active=override_active,
        findings=tuple(findings),
    )


def enforce_startup_security(**kwargs: object) -> SecurityPosture:
    posture = evaluate_security_posture(**kwargs)  # type: ignore[arg-type]
    if not posture.startup_allowed:
        codes = ", ".join(item.code for item in posture.findings if item.severity == "critical")
        raise SecurityPostureError(f"unsafe remote configuration: {codes}")
    return posture


def is_loopback_host(host: object) -> bool:
    clean = str(host or "").strip().strip("[]").lower()
    if clean == "localhost":
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def trusted_proxy_peer(peer: object, environ: dict[str, str] | None = None) -> bool:
    environment = os.environ if environ is None else environ
    configured = str(environment.get(TRUSTED_PROXY_ENV, "")).strip()
    if not configured:
        return False
    try:
        address = ipaddress.ip_address(str(peer or "").strip())
    except ValueError:
        return False
    for raw in configured.split(","):
        try:
            network = ipaddress.ip_network(raw.strip(), strict=True)
        except ValueError:
            continue
        if address in network:
            return True
    return False


def _private_file(path: Path) -> bool:
    if os.name == "nt":
        return True
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode & 0o077 == 0


def _linux_effective_capabilities(
    path: Path = Path("/proc/self/status"),
) -> int | None:
    if os.name != "posix":
        return None
    try:
        text = _read_bounded_ascii(path, MAX_PROC_STATUS_BYTES)
    except (OSError, UnicodeError):
        return None
    if text is None:
        return None
    for line in text.splitlines():
        if not line.startswith("CapEff:"):
            continue
        value = line.partition(":")[2].strip()
        if not value or len(value) > 16:
            return None
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


def _linux_exposed_tcp_ports() -> frozenset[int] | None:
    if os.name != "posix":
        return None
    ports: set[int] = set()
    inspected = False
    for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            text = _read_bounded_ascii(path, MAX_PROC_NET_BYTES)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            return None
        if text is None:
            return None
        inspected = True
        parsed = _parse_linux_tcp_listeners(text)
        if parsed is None:
            return None
        ports.update(parsed)
    return frozenset(ports) if inspected else None


def _parse_linux_tcp_listeners(text: str) -> frozenset[int] | None:
    ports: set[int] = set()
    rows = text.splitlines()
    if len(rows) > MAX_PROC_NET_ROWS + 1:
        return None
    for row in rows[1:]:
        fields = row.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        address_port = fields[1].split(":", 1)
        if len(address_port) != 2:
            return None
        address, raw_port = address_port
        if _linux_tcp_address_is_loopback(address):
            continue
        try:
            port = int(raw_port, 16)
        except ValueError:
            return None
        if not 1 <= port <= 65535:
            return None
        ports.add(port)
    return frozenset(ports)


def _linux_tcp_address_is_loopback(address: str) -> bool:
    if len(address) == 8:
        try:
            packed = bytes.fromhex(address)
        except ValueError:
            return False
        return ipaddress.IPv4Address(packed[::-1]).is_loopback
    return address == "00000000000000000000000001000000"


def _capability_mask(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if 0 <= parsed < 1 << 64 else None


def _listen_port(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SecurityPostureError("listen port is invalid")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise SecurityPostureError("listen port is invalid") from exc
    if not 1 <= parsed <= 65535:
        raise SecurityPostureError("listen port is invalid")
    return parsed


def _listening_port_set(value: object) -> frozenset[int] | None:
    if isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        values = tuple(value)  # type: ignore[arg-type]
    except TypeError:
        return None
    if len(values) > 256:
        return None
    ports: set[int] = set()
    for item in values:
        try:
            port = _listen_port(item)
        except SecurityPostureError:
            return None
        if port is not None:
            ports.add(port)
    return frozenset(ports)


def _read_bounded_ascii(path: Path, maximum_bytes: int) -> str | None:
    with path.open("r", encoding="ascii", errors="strict", newline="") as stream:
        text = stream.read(maximum_bytes + 1)
    return text if len(text) <= maximum_bytes else None
