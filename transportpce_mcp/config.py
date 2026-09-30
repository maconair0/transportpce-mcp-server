"""
Where TransportPCE is, and how to authenticate to it. All of it from the
environment; none of it hardcoded.

TransportPCE is not installed yet, so every value here has to be pointable at
wherever it ends up — a local Karaf build, a lighty.io runtime, a container on
another host. The defaults describe OpenDaylight's out-of-the-box Karaf (8181,
admin/admin) because that is what an untouched install answers on, not because
this is expected to talk to one.

The RESTCONF root is configurable for a reason that bit the documentation
itself: OpenDaylight served RESTCONF at `/restconf` before the RFC 8040
rewrite and at `/rests` after it. TransportPCE's own test harness still
selects between them (`USE_ODL_RESTCONF_VERSION`, mapping `rfc8040` to
`/rests` and `draft02` to `/restconf`), so a server that hardcodes either one
is wrong against half the releases in the field.
"""
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

# The two roots OpenDaylight has served RESTCONF under, named as TransportPCE's
# own test harness names them.
RESTCONF_ROOTS: Dict[str, str] = {"rfc8040": "/rests", "draft02": "/restconf"}


def _env(name: str, default: str = "") -> str:
    """TPCE_-prefixed, falling back to the names ODL's own tooling uses."""
    return os.getenv(f"TPCE_{name}", "") or default


@dataclass
class TransportPceConfig:
    """Everything needed to reach one TransportPCE instance."""

    base_url: str = ""
    username: str = ""
    password: str = ""
    restconf_root: str = ""
    timeout: float = 60.0
    # Path computation can take a while on a large topology; service-create
    # walks the whole renderer. Separate from the read timeout so a slow
    # computation does not force every inventory read to wait as long.
    long_timeout: float = 180.0
    verify_tls: bool = True
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "TransportPceConfig":
        version = (_env("RESTCONF_VERSION") or
                   os.getenv("USE_ODL_RESTCONF_VERSION", "") or "rfc8040").strip()
        root = _env("RESTCONF_ROOT") or RESTCONF_ROOTS.get(version, "")
        if not root:
            known = ", ".join(sorted(RESTCONF_ROOTS))
            raise ValueError(
                f"unknown RESTCONF version {version!r}; known: {known}. "
                f"Set TPCE_RESTCONF_ROOT explicitly to override.")
        port = _env("PORT") or os.getenv("USE_ODL_ALT_RESTCONF_PORT", "") or "8181"
        host = _env("HOST", "127.0.0.1")
        scheme = _env("SCHEME", "http")
        return cls(
            base_url=(_env("BASE_URL") or f"{scheme}://{host}:{port}").rstrip("/"),
            username=_env("USERNAME", "admin"),
            password=_env("PASSWORD", "admin"),
            restconf_root="/" + root.strip("/"),
            timeout=float(_env("TIMEOUT", "60")),
            long_timeout=float(_env("LONG_TIMEOUT", "180")),
            # Only ever relaxed deliberately. A self-signed Karaf certificate is
            # a real reason; the default is not to be casual about it.
            verify_tls=_env("VERIFY_TLS", "true").strip().lower() not in
            ("0", "false", "no", "off"),
        )

    @property
    def data_root(self) -> str:
        """Where configuration and operational datastore reads live."""
        return f"{self.base_url}{self.restconf_root}/data"

    @property
    def operations_root(self) -> str:
        """Where RPCs are POSTed."""
        return f"{self.base_url}{self.restconf_root}/operations"

    def describe(self) -> Dict[str, object]:
        """Safe to log and to return from a tool: no password."""
        return {
            "base_url": self.base_url,
            "restconf_root": self.restconf_root,
            "username": self.username,
            "password_set": bool(self.password),
            "verify_tls": self.verify_tls,
            "timeout": self.timeout,
        }


def auth_of(config: TransportPceConfig) -> Optional[tuple]:
    """Basic auth, or none at all.

    ODL ships with basic auth enabled, but an instance behind a gateway that
    does its own authentication should not have credentials bolted on — so an
    empty username means send none rather than send `""`.
    """
    if not config.username:
        return None
    return (config.username, config.password)
