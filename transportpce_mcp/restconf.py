"""
The RESTCONF transport. One place that knows how TransportPCE answers.

Two things here earn their keep.

**Errors arrive as structured RESTCONF documents, not status codes.** A failed
RPC returns 200 with `output.configuration-response-common.response-code` set,
and a rejected datastore read returns an `ietf-restconf:errors` body naming the
missing schema node. A transport that reports only "HTTP 409" throws away the
part that says why, which is the part an operator needs.

**Every reply is JSON, and stays JSON.** Worth stating because MCP servers in
the wild do not agree on it. Some return `str(python_dict)` — single quotes,
`True`, `None` — which is not JSON and cannot be parsed without guessing, so
every client ends up carrying a fallback. A caller here can use `json.loads` and
be done.
"""
import json
import logging
from typing import Any, Dict, Optional

from .config import TransportPceConfig, auth_of

logger = logging.getLogger("transportpce.restconf")

# RESTCONF's own media types. ODL accepts both the RFC 8040 names and plain
# application/json; the specific names are sent because an older ODL behind a
# strict proxy has been known to refuse the generic one.
JSON_RESTCONF = "application/yang-data+json"


class TransportPceError(Exception):
    """A request reached TransportPCE and it said no."""

    def __init__(self, message: str, status: int = 0, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class TransportPceUnreachable(Exception):
    """TransportPCE could not be reached at all."""


def _restconf_errors(body: Any) -> str:
    """Pull the human-readable part out of an ietf-restconf:errors document."""
    if not isinstance(body, dict):
        return ""
    errors = (body.get("ietf-restconf:errors") or body.get("errors") or {})
    items = errors.get("error") if isinstance(errors, dict) else None
    if not isinstance(items, list):
        return ""
    parts = []
    for err in items:
        if not isinstance(err, dict):
            continue
        detail = (err.get("error-message") or err.get("error-info") or
                  err.get("error-tag") or "")
        tag = err.get("error-tag", "")
        parts.append(f"{tag}: {detail}".strip(": ") if tag else str(detail))
    return "; ".join(p for p in parts if p)


def rpc_failed(body: Any) -> Optional[str]:
    """Why an RPC that returned HTTP 200 nevertheless failed, or None.

    OpenROADM RPCs carry their verdict in the body:
    `{"output": {"configuration-response-common": {"response-code": "500",
    "response-message": "..."}}}`. A 200 is the transport succeeding, not the
    request — reading it as success is how a refused service-create gets
    reported as provisioned.
    """
    if not isinstance(body, dict):
        return None
    out = body.get("output")
    if not isinstance(out, dict):
        return None
    common = out.get("configuration-response-common")
    if not isinstance(common, dict):
        return None
    code = str(common.get("response-code", ""))
    message = str(common.get("response-message", ""))
    # OpenROADM uses "200"/"OK" for success and an HTTP-ish code otherwise.
    if code and code not in ("200", "OK", "Successful"):
        return f"{code}: {message}" if message else code
    ack = str(out.get("ack-final-indicator", ""))
    if ack == "No" and message:
        # A "No" with no error code means the request was accepted for
        # asynchronous processing; report the message rather than inventing
        # either a success or a failure.
        return None
    return None


class RestconfClient:
    """Async RESTCONF calls against one TransportPCE."""

    def __init__(self, config: Optional[TransportPceConfig] = None, client: Any = None):
        self.config = config or TransportPceConfig.from_env()
        # Injectable so tests exercise the real request-building and
        # error-mapping code against canned responses rather than mocking it out.
        self._client = client

    async def _request(self, method: str, url: str, json_body: Any = None,
                       timeout: Optional[float] = None) -> Any:
        import httpx

        headers = {"Accept": JSON_RESTCONF + ", application/json",
                   **self.config.extra_headers}
        if json_body is not None:
            headers["Content-Type"] = JSON_RESTCONF
        kwargs = dict(headers=headers, auth=auth_of(self.config),
                      timeout=timeout or self.config.timeout)

        async def send(client):
            return await client.request(method, url, json=json_body, **kwargs)

        try:
            if self._client is not None:
                response = await send(self._client)
            else:
                async with httpx.AsyncClient(verify=self.config.verify_tls) as client:
                    response = await send(client)
        except Exception as e:  # noqa: BLE001 - every transport failure, one story
            raise TransportPceUnreachable(
                f"{self.config.base_url}: {type(e).__name__}: {e}") from e

        text = response.text or ""
        try:
            body = json.loads(text) if text.strip() else {}
        except ValueError:
            body = text

        if response.status_code == 404:
            # A RESTCONF 404 on a data path usually means "nothing configured
            # there yet", which is an answer rather than a fault.
            raise TransportPceError(
                _restconf_errors(body) or "not found in the datastore",
                status=404, body=body)
        if response.status_code == 401:
            raise TransportPceError(
                "authentication rejected — check TPCE_USERNAME/TPCE_PASSWORD",
                status=401, body=body)
        if response.status_code >= 400:
            detail = _restconf_errors(body) or (
                text[:300] if isinstance(body, str) else json.dumps(body)[:300])
            raise TransportPceError(f"HTTP {response.status_code}: {detail}",
                                    status=response.status_code, body=body)
        return body

    async def get(self, path: str, params: str = "") -> Any:
        """Read a datastore path, relative to the RESTCONF data root."""
        url = f"{self.config.data_root}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{params.lstrip('?')}"
        return await self._request("GET", url)

    async def put(self, path: str, body: Any) -> Any:
        url = f"{self.config.data_root}/{path.lstrip('/')}"
        return await self._request("PUT", url, json_body=body,
                                   timeout=self.config.long_timeout)

    async def delete(self, path: str) -> Any:
        url = f"{self.config.data_root}/{path.lstrip('/')}"
        return await self._request("DELETE", url, timeout=self.config.long_timeout)

    async def rpc(self, name: str, body: Any, long: bool = True) -> Any:
        """POST an RPC and raise if the body reports a failure.

        `name` is the qualified RPC, e.g. `transportpce-pce:path-computation-request`.
        """
        url = f"{self.config.operations_root}/{name}"
        got = await self._request(
            "POST", url, json_body=body,
            timeout=self.config.long_timeout if long else self.config.timeout)
        why = rpc_failed(got)
        if why:
            raise TransportPceError(f"{name} refused: {why}", status=200, body=got)
        return got

    async def health(self) -> Dict[str, Any]:
        """Is TransportPCE there, and does it have a portmapping to speak of?

        Deliberately a datastore read rather than a ping: a Karaf that has
        started but has not finished installing `odl-transportpce` answers TCP
        and then 404s every model path, which "reachable" would misreport.
        """
        try:
            await self.get("transportpce-portmapping:network")
            return {"reachable": True, "transportpce_installed": True,
                    **self.config.describe()}
        except TransportPceError as e:
            installed = e.status != 404
            return {"reachable": True, "transportpce_installed": installed,
                    "detail": str(e), **self.config.describe()}
        except TransportPceUnreachable as e:
            return {"reachable": False, "detail": str(e), **self.config.describe()}
