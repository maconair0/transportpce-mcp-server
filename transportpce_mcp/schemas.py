"""
Request shapes, taken from the YANG rather than from prose.

Every constraint here was read out of the models themselves:

  transportpce-pce@2024-02-05.yang
      rpc path-computation-request: `service-name` and `resource-reserve` are
      `mandatory true`; it `uses service-handler-header`, whose `request-id` is
      also mandatory; `service-a-end`/`service-z-end` use `service-endpoint-sp`.
  transportpce-common-service-path-types@2022-01-18.yang
      grouping service-endpoint-sp: `service-format` and `clli` are mandatory;
      `service-rate` is mandatory `when "../service-format!='OMS'"`; `node-id`,
      `tx-direction` and `rx-direction` are *not* mandatory.
      typedef pce-metric: hop-count, propagation-delay, TE-metric, IGP-metric.
  org-openroadm-service-format.yang (OpenROADM MSA)
      typedef service-format: Ethernet, OTU, OC, STM, OMS, ODU, OTM, other, flexo.

Two of those are worth stating out loud because the documentation says
otherwise. `node-id` is optional for path computation — the docs page lists it
among an endpoint's required fields, and the model does not. And `service-rate`
is conditionally mandatory, not absolutely: an `OMS` (ROADM-line) service
legitimately has none, so demanding it always would reject a valid request.

The point of validating here rather than passing JSON through is that RESTCONF
fails unhelpfully. A missing mandatory leaf comes back as a schema-node error
naming an internal path, which tells a caller nothing about which argument they
omitted.
"""
from typing import Any, Dict, List, Optional, Tuple

# From org-openroadm-service-format.yang. `flexo-x`/`flexo-xe` derive from
# `flexo` and are accepted so a caller is not blocked on our enum being stale.
SERVICE_FORMATS = ("Ethernet", "OTU", "OC", "STM", "OMS", "ODU", "OTM", "other",
                   "flexo", "flexo-x", "flexo-xe")

# From transportpce-common-service-path-types, typedef pce-metric.
PCE_METRICS = ("hop-count", "propagation-delay", "TE-metric", "IGP-metric")

# The networks TransportPCE publishes under ietf-network:networks.
NETWORKS = ("openroadm-topology", "otn-topology", "openroadm-network",
            "clli-network")

# org-openroadm-service:service-create's connection-type.
CONNECTION_TYPES = ("service", "infrastructure", "roadm-line")


class ValidationError(ValueError):
    """A request that TransportPCE would have rejected less helpfully."""


def _endpoint_errors(end: Any, label: str) -> List[str]:
    """Check one service-a-end / service-z-end against service-endpoint-sp."""
    if not isinstance(end, dict) or not end:
        return [f"{label} is required and must be an object with at least "
                f"service-format and clli"]
    problems: List[str] = []

    fmt = end.get("service-format")
    if not fmt:
        problems.append(f"{label}.service-format is mandatory "
                        f"(one of: {', '.join(SERVICE_FORMATS)})")
    elif fmt not in SERVICE_FORMATS:
        problems.append(f"{label}.service-format {fmt!r} is not an OpenROADM "
                        f"service-format; expected one of: "
                        f"{', '.join(SERVICE_FORMATS)}")

    if not end.get("clli"):
        problems.append(f"{label}.clli is mandatory (the site identifier)")

    # `when "../service-format!='OMS'"` — conditionally mandatory, so an OMS
    # endpoint without a rate is correct rather than incomplete.
    rate = end.get("service-rate")
    if fmt != "OMS":
        if rate is None:
            problems.append(f"{label}.service-rate is mandatory unless "
                            f"service-format is OMS (rate in Gbps, e.g. 100)")
        elif not isinstance(rate, int) or isinstance(rate, bool) or rate < 0:
            problems.append(f"{label}.service-rate must be a non-negative "
                            f"integer number of Gbps, not {rate!r}")
    elif rate is not None:
        problems.append(f"{label}.service-rate does not apply when "
                        f"service-format is OMS")
    return problems


def _direction(value: Any) -> Any:
    """Normalise tx-direction / rx-direction to what the model expects.

    They are containers, not leaves, so a bare port string has to be wrapped.
    A caller who already passed the full structure is left alone.
    """
    if value is None or isinstance(value, (dict, list)):
        return value
    return {"port": {"port-name": str(value)}}


def path_computation_request(
    service_name: str,
    service_a_end: Dict[str, Any],
    service_z_end: Dict[str, Any],
    request_id: str = "",
    resource_reserve: bool = False,
    pce_routing_metric: str = "",
    customer_name: str = "",
    hard_constraints: Optional[Dict[str, Any]] = None,
    soft_constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build and validate a transportpce-pce:path-computation-request body.

    `resource_reserve` defaults to False deliberately. It is mandatory in the
    model, so something must be sent, and True tells the PCE to hold the
    computed resources until cancelled — a lasting effect on the controller's
    topology, which is not what an advisory computation should do by default.
    """
    problems: List[str] = []
    if not service_name:
        problems.append("service-name is mandatory")
    if pce_routing_metric and pce_routing_metric not in PCE_METRICS:
        problems.append(f"pce-routing-metric {pce_routing_metric!r} is not one "
                        f"of: {', '.join(PCE_METRICS)}")
    if not isinstance(resource_reserve, bool):
        problems.append("resource-reserve must be true or false")
    problems += _endpoint_errors(service_a_end, "service-a-end")
    problems += _endpoint_errors(service_z_end, "service-z-end")
    if problems:
        raise ValidationError("; ".join(problems))

    def endpoint(end: Dict[str, Any]) -> Dict[str, Any]:
        built = {k: v for k, v in end.items()
                 if k not in ("tx-direction", "rx-direction") and v is not None}
        for side in ("tx-direction", "rx-direction"):
            if end.get(side) is not None:
                built[side] = _direction(end[side])
        return built

    body: Dict[str, Any] = {
        "input": {
            "service-name": service_name,
            "resource-reserve": bool(resource_reserve),
            "service-handler-header": {"request-id": request_id or service_name},
            "service-a-end": endpoint(service_a_end),
            "service-z-end": endpoint(service_z_end),
        }
    }
    if pce_routing_metric:
        body["input"]["pce-routing-metric"] = pce_routing_metric
    if customer_name:
        body["input"]["customer-name"] = customer_name
    if hard_constraints:
        body["input"]["hard-constraints"] = hard_constraints
    if soft_constraints:
        body["input"]["soft-constraints"] = soft_constraints
    return body


def service_create_request(
    service_name: str,
    service_a_end: Dict[str, Any],
    service_z_end: Dict[str, Any],
    connection_type: str = "service",
    request_id: str = "",
    customer: str = "",
    due_date: str = "",
) -> Dict[str, Any]:
    """Build and validate an org-openroadm-service:service-create body.

    The endpoint checks are the same shape as path computation's, plus the
    directions: service-create renders a real cross-connect, and without
    tx-direction/rx-direction the renderer has no port to configure. The PCE
    can compute without them; the renderer cannot act without them.
    """
    problems: List[str] = []
    if not service_name:
        problems.append("service-name is mandatory")
    if connection_type not in CONNECTION_TYPES:
        problems.append(f"connection-type {connection_type!r} is not one of: "
                        f"{', '.join(CONNECTION_TYPES)}")
    for label, end in (("service-a-end", service_a_end),
                       ("service-z-end", service_z_end)):
        problems += _endpoint_errors(end, label)
        if isinstance(end, dict) and end:
            if not end.get("node-id"):
                problems.append(f"{label}.node-id is required to provision "
                                f"(the PCE can compute without it; the "
                                f"renderer cannot configure without it)")
            for side in ("tx-direction", "rx-direction"):
                if not end.get(side):
                    problems.append(f"{label}.{side} is required to provision")
    if problems:
        raise ValidationError("; ".join(problems))

    def endpoint(end: Dict[str, Any]) -> Dict[str, Any]:
        built = {k: v for k, v in end.items()
                 if k not in ("tx-direction", "rx-direction") and v is not None}
        built["tx-direction"] = _direction(end["tx-direction"])
        built["rx-direction"] = _direction(end["rx-direction"])
        return built

    body: Dict[str, Any] = {
        "input": {
            "sdnc-request-header": {
                "request-id": request_id or service_name,
                "rpc-action": "service-create",
                "request-system-id": "transportpce-mcp-server",
            },
            "service-name": service_name,
            "common-id": service_name,
            "connection-type": connection_type,
            "service-a-end": endpoint(service_a_end),
            "service-z-end": endpoint(service_z_end),
        }
    }
    if customer:
        body["input"]["customer"] = customer
    if due_date:
        body["input"]["due-date"] = due_date
    return body


def service_delete_request(service_name: str, request_id: str = "",
                           tail_retention: str = "no") -> Dict[str, Any]:
    if not service_name:
        raise ValidationError("service-name is mandatory")
    return {
        "input": {
            "sdnc-request-header": {
                "request-id": request_id or f"delete-{service_name}",
                "rpc-action": "service-delete",
                "request-system-id": "transportpce-mcp-server",
            },
            "service-delete-req-info": {
                "service-name": service_name,
                "tail-retention": tail_retention,
            },
        }
    }


def netconf_node_body(node_id: str, host: str, port: int, username: str,
                      password: str) -> Dict[str, Any]:
    """A network-topology NETCONF node, for mounting a device on TransportPCE."""
    problems = []
    if not node_id:
        problems.append("node-id is mandatory")
    if not host:
        problems.append("host is mandatory")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append(f"port must be 1-65535, not {port!r}")
    if problems:
        raise ValidationError("; ".join(problems))
    return {
        "network-topology:node": [{
            "node-id": node_id,
            "netconf-node-topology:host": host,
            "netconf-node-topology:port": port,
            "netconf-node-topology:username": username,
            "netconf-node-topology:password": password,
            "netconf-node-topology:tcp-only": False,
            "netconf-node-topology:keepalive-delay": 0,
        }]
    }


def validate_network(name: str) -> str:
    if name not in NETWORKS:
        raise ValidationError(f"{name!r} is not a TransportPCE network; known: "
                              f"{', '.join(NETWORKS)}")
    return name


# ----- the MCP input schemas, so a model is told the shape before it guesses -----

_ENDPOINT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": ("An OpenROADM service endpoint (service-endpoint-sp). "
                    "service-format and clli are mandatory; service-rate is "
                    "mandatory unless service-format is OMS."),
    "properties": {
        "service-format": {"type": "string", "enum": list(SERVICE_FORMATS)},
        "service-rate": {"type": "integer",
                         "description": "Rate in Gbps, e.g. 100 or 400"},
        "clli": {"type": "string", "description": "Site identifier"},
        "node-id": {"type": "string",
                    "description": "Optional for path computation; required to provision"},
        "tx-direction": {"description": "Port name, or the full container"},
        "rx-direction": {"description": "Port name, or the full container"},
    },
    "required": ["service-format", "clli"],
}


def endpoint_schema() -> Dict[str, Any]:
    return dict(_ENDPOINT_SCHEMA)


def describe_shapes() -> Dict[str, Any]:
    """What the models require, as data — for the introspection tool."""
    return {
        "service_formats": list(SERVICE_FORMATS),
        "pce_metrics": list(PCE_METRICS),
        "networks": list(NETWORKS),
        "connection_types": list(CONNECTION_TYPES),
        "path_computation_mandatory": ["service-name", "resource-reserve",
                                       "service-handler-header.request-id",
                                       "service-a-end.service-format",
                                       "service-a-end.clli",
                                       "service-z-end.service-format",
                                       "service-z-end.clli"],
        "service_create_additionally_requires": ["node-id", "tx-direction",
                                                 "rx-direction",
                                                 "connection-type"],
        "notes": [
            "service-rate is mandatory only when service-format is not OMS",
            "node-id is optional for path computation and required to provision",
        ],
        "sources": [
            "transportpce-pce@2024-02-05.yang",
            "transportpce-common-service-path-types@2022-01-18.yang",
            "org-openroadm-service-format.yang (OpenROADM MSA)",
        ],
    }
