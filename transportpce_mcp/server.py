"""
An MCP server for the OpenDaylight TransportPCE optical SDN controller.

**Naming and response shape.** Every tool is prefixed `tpce_` and returns JSON.
Both choices are for the client's benefit:

  - *Prefixed names.* A bare `get_topology` says nothing about whose network it
    describes, so a client mounting several controllers has to namespace it
    anyway. Doing it at the source spares the client guessing.
  - *JSON, not Python reprs.* Some MCP servers return `str(python_dict)`, which
    is not parseable without a fallback. `json.loads` is enough here.

**Read/write split.** Reads answer. Writes queue. `tpce_service_create`,
`tpce_service_delete` and `tpce_device_connect` never reach RESTCONF from a tool
call: they validate, consult policy, and append to an approval queue. See
`gate.py` for why there is no argument that skips it.

Path computation sits on the read side, with `resource-reserve` forced to false.
It is advisory: the PCE computes a path without claiming anything. Asking for a
reservation would be a write, so this server does not offer it.

Built against the documented RESTCONF surface and the published YANG; no
TransportPCE instance was available when it was written. Everything about where
that instance is lives in `config.py`, from the environment.
"""
import json
import logging
from typing import Any, Dict, Optional

from . import schemas, summarise
from .config import TransportPceConfig
from .gate import GateUnavailable, WriteGate
from .restconf import RestconfClient, TransportPceError, TransportPceUnreachable

logger = logging.getLogger("transportpce.mcp")

PORTMAPPING = "transportpce-portmapping:network"
NETWORKS_PATH = "ietf-network:networks/network={network}"
NETCONF_NODE = ("network-topology:network-topology/topology=topology-netconf/"
                "node={node_id}")
SERVICE_LIST = "org-openroadm-service:service-list"

RPC_PATH_COMPUTATION = "transportpce-pce:path-computation-request"
RPC_SERVICE_CREATE = "org-openroadm-service:service-create"
RPC_SERVICE_DELETE = "org-openroadm-service:service-delete"
RPC_TAPI_TOPOLOGY = "tapi-topology:get-topology-details"
RPC_TAPI_CONNECTIVITY = "tapi-connectivity:get-connectivity-service-list"


def _reply(payload: Any) -> str:
    """The one response shape: JSON text, like TFS's `format_response`."""
    return json.dumps(payload, indent=2, default=str)


def _failure(what: str, exc: Exception) -> str:
    """An error a caller can branch on rather than parse out of prose.

    A server that returns `f"Error ...: {e}"` as a *successful* result makes a
    controller that is down read as one that answered. A tool here reports
    `{"ok": false, ...}`, so the failure is in the data and not in the prose.
    """
    kind = ("unreachable" if isinstance(exc, TransportPceUnreachable)
            else "refused" if isinstance(exc, TransportPceError)
            else "invalid_request" if isinstance(exc, schemas.ValidationError)
            else type(exc).__name__)
    out: Dict[str, Any] = {"ok": False, "error": kind, "operation": what,
                           "detail": str(exc)}
    status = getattr(exc, "status", 0)
    if status:
        out["http_status"] = status
    return _reply(out)


def create_server(config: Optional[TransportPceConfig] = None,
                  client: Optional[RestconfClient] = None,
                  gate: Optional[WriteGate] = None):
    """Build the FastMCP server. Nothing is contacted until a tool is called."""
    from mcp.server.fastmcp import FastMCP

    config = config or TransportPceConfig.from_env()
    client = client or RestconfClient(config)
    gate = gate or WriteGate()
    mcp = FastMCP("OpenDaylight TransportPCE", log_level="ERROR")
    register_tools(mcp, client, gate)
    return mcp


def register_tools(mcp, client: RestconfClient, gate: WriteGate) -> None:
    """Every tool, in one place, so the read/write split is visible at a glance."""

    # ----------------------------------------------------------------- reads

    @mcp.tool()
    async def tpce_health_check() -> str:
        """Is TransportPCE reachable, and is odl-transportpce actually installed?

        A Karaf that has started but not finished installing the feature answers
        TCP and then 404s every model path, so this reads a model path.
        """
        try:
            return _reply({"ok": True, **await client.health()})
        except Exception as e:  # noqa: BLE001
            return _failure("health_check", e)

    @mcp.tool()
    async def tpce_get_topology(network: str = "openroadm-topology",
                               raw: bool = False) -> str:
        """Summarise a TransportPCE topology: node and link counts, node types, sample ids.

        network: openroadm-topology (default), otn-topology, openroadm-network,
        clli-network. Pass raw=true for the full OpenROADM payload, which is
        large — a real topology runs to megabytes, so the summary is the default.
        """
        try:
            name = schemas.validate_network(network)
            got = await client.get(NETWORKS_PATH.format(network=name))
            return _reply({"ok": True, "network": name,
                           "data": got if raw else summarise.summarise_topology(got, name)})
        except Exception as e:  # noqa: BLE001
            return _failure("get_topology", e)

    @mcp.tool()
    async def tpce_get_portmapping(node_id: str = "", raw: bool = False) -> str:
        """Read TransportPCE's port mapping — its abstraction of each device's ports.

        node_id narrows it to one node. Pass raw=true for every
        logical-connection-point rather than per-node counts.
        """
        try:
            path = PORTMAPPING + (f"/nodes={node_id}" if node_id else "")
            got = await client.get(path)
            return _reply({"ok": True, "node_id": node_id or "all",
                           "data": got if raw else summarise.summarise_portmapping(got)})
        except Exception as e:  # noqa: BLE001
            return _failure("get_portmapping", e)

    @mcp.tool()
    async def tpce_get_node_status(node_id: str, raw: bool = False) -> str:
        """Whether a mounted NETCONF device is connected, and what it advertises.

        Reads the operational datastore (content=nonconfig), which is where the
        connection status lives — the config view only says what was asked for.
        """
        try:
            got = await client.get(NETCONF_NODE.format(node_id=node_id),
                                   params="content=nonconfig")
            return _reply({"ok": True, "node_id": node_id,
                           "data": got if raw
                           else summarise.summarise_node_status(got, node_id)})
        except Exception as e:  # noqa: BLE001
            return _failure("get_node_status", e)

    @mcp.tool()
    async def tpce_list_services(raw: bool = False) -> str:
        """Services TransportPCE currently has provisioned, with their states."""
        try:
            got = await client.get(SERVICE_LIST)
            return _reply({"ok": True,
                           "data": got if raw else summarise.summarise_services(got)})
        except TransportPceError as e:
            if e.status == 404:
                # Nothing provisioned yet is an answer, not a fault.
                return _reply({"ok": True, "data": {"services": 0, "detail": []}})
            return _failure("list_services", e)
        except Exception as e:  # noqa: BLE001
            return _failure("list_services", e)

    @mcp.tool()
    async def tpce_get_service(service_name: str, raw: bool = False) -> str:
        """One provisioned service in full, by name."""
        try:
            got = await client.get(f"{SERVICE_LIST}/services={service_name}")
            return _reply({"ok": True, "service_name": service_name,
                           "data": got if raw else summarise.summarise_services(
                               {"services": (got.get("services") if isinstance(got, dict)
                                             else None) or []})})
        except Exception as e:  # noqa: BLE001
            return _failure("get_service", e)

    @mcp.tool()
    async def tpce_compute_path(service_name: str, service_a_end: Dict[str, Any],
                                service_z_end: Dict[str, Any],
                                pce_routing_metric: str = "",
                                raw: bool = False) -> str:
        """Ask the PCE for a path. Advisory: computes, reserves nothing.

        Each endpoint needs at least service-format (Ethernet, OTU, OC, STM, OMS,
        ODU, OTM, flexo, other) and clli, plus service-rate in Gbps unless the
        format is OMS. node-id is optional here and required to provision.
        pce_routing_metric: hop-count, propagation-delay, TE-metric, IGP-metric.

        resource-reserve is forced to false. Reserving holds resources in the
        controller's topology until cancelled, which is a change to it — and this
        tool is on the read side of the split.
        """
        try:
            body = schemas.path_computation_request(
                service_name=service_name, service_a_end=service_a_end,
                service_z_end=service_z_end,
                pce_routing_metric=pce_routing_metric,
                resource_reserve=False)
            got = await client.rpc(RPC_PATH_COMPUTATION, body)
            return _reply({"ok": True, "advisory": True, "reserved": False,
                           "data": got if raw
                           else summarise.summarise_path_computation(got)})
        except Exception as e:  # noqa: BLE001
            return _failure("compute_path", e)

    @mcp.tool()
    async def tpce_describe_models() -> str:
        """What the YANG models require, so a caller need not guess.

        Which fields are mandatory, which enums are legal, and the two places the
        documentation and the models disagree.
        """
        return _reply({"ok": True, "data": schemas.describe_shapes()})

    @mcp.tool()
    async def tpce_get_tapi_topology_details(topology_id: str = "",
                                            raw: bool = False) -> str:
        """TAPI's abstracted view, for callers that speak TAPI rather than OpenROADM.

        An RPC by protocol but a read by effect. Needs the tapi feature installed;
        a 404 here means odl-transportpce-tapi is not loaded.
        """
        try:
            body = {"input": {"topology-id-or-name": topology_id}} if topology_id else {"input": {}}
            got = await client.rpc(RPC_TAPI_TOPOLOGY, body)
            payload = got
            if not raw and isinstance(got, dict):
                topo = (got.get("output") or {}).get("topology") or {}
                payload = {"uuid": topo.get("uuid"),
                           "nodes": len(topo.get("node") or []),
                           "links": len(topo.get("link") or []),
                           "raw_available": "call again with raw=true"}
            return _reply({"ok": True, "data": payload})
        except Exception as e:  # noqa: BLE001
            return _failure("tapi_get_topology_details", e)

    @mcp.tool()
    async def tpce_list_tapi_connectivity_services(raw: bool = False) -> str:
        """TAPI connectivity services currently known to TransportPCE."""
        try:
            got = await client.rpc(RPC_TAPI_CONNECTIVITY, {"input": {}})
            payload = got
            if not raw and isinstance(got, dict):
                services = (got.get("output") or {}).get("service") or []
                payload = {"services": len(services),
                           "uuids": [s.get("uuid") for s in services[:12]
                                     if isinstance(s, dict)],
                           "raw_available": "call again with raw=true"}
            return _reply({"ok": True, "data": payload})
        except Exception as e:  # noqa: BLE001
            return _failure("tapi_list_connectivity_services", e)

    # ---------------------------------------------------------------- writes
    #
    # These queue. None of them reaches RESTCONF; `gate.apply_approved` does
    # that, after an operator decides, and is not reachable from any tool.

    @mcp.tool()
    async def tpce_service_create(service_name: str,
                                  service_a_end: Dict[str, Any],
                                  service_z_end: Dict[str, Any],
                                  connection_type: str = "service") -> str:
        """Request an OpenROADM service. Queues for operator approval; provisions nothing.

        Both endpoints need service-format, clli, service-rate (unless OMS),
        node-id, tx-direction and rx-direction — the renderer configures real
        ports, so the directions are not optional here even though the PCE can
        compute without them.

        There is no argument that skips the approval queue. Creating a service
        renders cross-connects on live ROADMs, and this tier does not decide that
        such a change is safe.
        """
        try:
            body = schemas.service_create_request(
                service_name=service_name, service_a_end=service_a_end,
                service_z_end=service_z_end, connection_type=connection_type)
            return _reply({"ok": True, **gate.request(
                action="tpce_service_create",
                summary=(f"create OpenROADM service {service_name} "
                         f"({service_a_end.get('node-id')} -> "
                         f"{service_z_end.get('node-id')}, "
                         f"{service_a_end.get('service-rate')}G)"),
                rpc=RPC_SERVICE_CREATE, body=body, target=service_name)})
        except GateUnavailable as e:
            return _failure("service_create", e)
        except Exception as e:  # noqa: BLE001
            return _failure("service_create", e)

    @mcp.tool()
    async def tpce_service_delete(service_name: str) -> str:
        """Request deletion of an OpenROADM service. Queues for approval; deletes nothing.

        Deleting a service drops traffic it currently carries, so this is gated
        more tightly than creating one.
        """
        try:
            body = schemas.service_delete_request(service_name)
            return _reply({"ok": True, **gate.request(
                action="tpce_service_delete",
                summary=f"delete OpenROADM service {service_name}",
                rpc=RPC_SERVICE_DELETE, body=body, target=service_name)})
        except GateUnavailable as e:
            return _failure("service_delete", e)
        except Exception as e:  # noqa: BLE001
            return _failure("service_delete", e)

    @mcp.tool()
    async def tpce_device_connect(node_id: str, host: str, port: int = 830,
                                  username: str = "", password: str = "") -> str:
        """Request that TransportPCE mount a NETCONF device. Queues for approval.

        Not traffic-affecting, but it puts a device under a controller's
        authority — and one device owned by two controllers is exactly the
        conflict `dispatcher.managed_elsewhere` exists to catch. So it is gated
        too, and the queued item records which device and which controller.
        """
        try:
            body = schemas.netconf_node_body(node_id, host, port, username, password)
            # A PUT to a data path, not an RPC. Recorded in the queued
            # instruction itself so the applier knows how to issue it without
            # inferring it from the action name.
            queued = gate.request(
                action="tpce_device_connect",
                summary=f"mount {node_id} ({host}:{port}) on TransportPCE",
                rpc="", body=body, target=node_id,
                method="PUT", path=NETCONF_NODE.format(node_id=node_id))
            return _reply({"ok": True, **queued})
        except GateUnavailable as e:
            return _failure("device_connect", e)
        except Exception as e:  # noqa: BLE001
            return _failure("device_connect", e)


# The tool names, split as the gate splits them. Kept as data so a test can
# assert the split rather than trusting the decorators to stay where they are.
READ_TOOLS = (
    "tpce_health_check", "tpce_get_topology", "tpce_get_portmapping",
    "tpce_get_node_status", "tpce_list_services", "tpce_get_service",
    "tpce_compute_path", "tpce_describe_models",
    "tpce_get_tapi_topology_details", "tpce_list_tapi_connectivity_services",
)
WRITE_TOOLS = (
    "tpce_service_create", "tpce_service_delete", "tpce_device_connect",
)
