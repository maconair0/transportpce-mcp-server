"""
Trimming OpenROADM payloads to something a model can read.

An `openroadm-topology` read returns every node with its full termination-point
list, every supporting-node reference and every available/used wavelength
bitmap. On a small lab network that is already hundreds of kilobytes; on a real
one it is megabytes. Returning it by default would spend the whole context
window on one call and leave nothing to reason with.

So the default answers the question — counts, a breakdown by node type, a
sample of ids — and `raw=True` stays for when somebody genuinely needs the
payload. A tool that dumps a full topology into an agent's context has spent the
whole budget before the agent has reasoned about anything.
"""
import os
from typing import Any, Dict, List

# How many of each thing to name before switching to a count.
SAMPLE = 12

# How many nodes and links to describe in full. Higher than SAMPLE because a
# per-row listing is the point of asking for a topology, and a row is small —
# but still bounded, because an unbounded one is what this module prevents.
DETAIL = int(os.getenv("TPCE_DETAIL_ROWS", "64"))


def _nodes_of(network: Dict[str, Any]) -> List[Dict[str, Any]]:
    nodes = network.get("node") or network.get("ietf-network:node") or []
    return nodes if isinstance(nodes, list) else []


def _links_of(network: Dict[str, Any]) -> List[Dict[str, Any]]:
    links = (network.get("ietf-network-topology:link") or network.get("link") or [])
    return links if isinstance(links, list) else []


def _node_kind(node: Dict[str, Any]) -> str:
    """OpenROADM node type, wherever this release hangs it.

    The augmentation prefix has moved between releases
    (`org-openroadm-common-network:node-type` today), so the key is matched by
    suffix rather than by one spelling that will age.
    """
    for key, value in node.items():
        if key.endswith("node-type") and isinstance(value, str):
            return value
    return "unknown"


def _augmented(node: Dict[str, Any], suffix: str, default: str = "") -> str:
    """Read a value whose YANG augmentation prefix moves between releases.

    Same reasoning as `_node_kind`: `operational-state` arrives as
    `org-openroadm-common-network:operational-state` on one release and bare on
    another, so the key is matched by suffix rather than by one spelling.
    """
    for key, value in node.items():
        if key.endswith(suffix) and isinstance(value, (str, int)):
            return str(value)
    return default


def _node_detail(node: Dict[str, Any]) -> Dict[str, Any]:
    """One OpenROADM node, flattened to what an operator reads in a listing."""
    return {
        "id": str(node.get("node-id", "")),
        "type": _node_kind(node),
        "admin_state": _augmented(node, "administrative-state", "unknown"),
        "operational_state": _augmented(node, "operational-state", "unknown"),
        "termination_points": len(
            node.get("ietf-network-topology:termination-point") or []),
        "supporting": [str(s.get("node-ref", "")) for s
                       in (node.get("supporting-node") or [])
                       if isinstance(s, dict)],
    }


def _link_detail(link: Dict[str, Any]) -> Dict[str, Any]:
    """One OpenROADM link, as the pair it connects rather than its full tree.

    The termination points are kept because on a ROADM an inter-degree link and
    an add/drop link join the same two node ids, so dropping the tp would render
    two different fibres as one duplicated row.
    """
    src = link.get("source") or {}
    dst = link.get("destination") or {}
    return {
        "id": str(link.get("link-id", "")),
        "a": str(src.get("source-node", "")),
        "a_tp": str(src.get("source-tp", "")),
        "z": str(dst.get("dest-node", "")),
        "z_tp": str(dst.get("dest-tp", "")),
        "type": _augmented(link, "link-type", "unknown"),
        "admin_state": _augmented(link, "administrative-state", "unknown"),
        "operational_state": _augmented(link, "operational-state", "unknown"),
    }


def _unwrap_networks(payload: Any) -> List[Dict[str, Any]]:
    """Find the network list wherever RESTCONF wrapped it.

    ODL returns `{"ietf-network:networks": {"network": [...]}}` for a whole-tree
    read and `{"network": [{...}]}` for a keyed one, and some releases return the
    single network unwrapped. All three arrive here.
    """
    if isinstance(payload, list):
        return [n for n in payload if isinstance(n, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("ietf-network:networks", "networks"):
        if key in payload and isinstance(payload[key], dict):
            return _unwrap_networks(payload[key])
    for key in ("ietf-network:network", "network"):
        if key in payload:
            return _unwrap_networks(payload[key])
    # Already a single network object.
    return [payload] if ("node" in payload or "network-id" in payload) else []


def summarise_topology(payload: Any, network_name: str = "") -> Dict[str, Any]:
    """Counts, a breakdown by node type, and a sample — not the payload."""
    networks = _unwrap_networks(payload)
    if not networks:
        return {"network": network_name, "nodes": 0, "links": 0,
                "note": "no network data in the reply"}

    out: Dict[str, Any] = {"network": network_name or
                           str(networks[0].get("network-id", "")) or "unknown"}
    nodes, links = [], []
    for net in networks:
        nodes += _nodes_of(net)
        links += _links_of(net)

    by_kind: Dict[str, int] = {}
    for node in nodes:
        by_kind[_node_kind(node)] = by_kind.get(_node_kind(node), 0) + 1

    degraded = [str(n.get("node-id", "")) for n in nodes
                if str(n.get("org-openroadm-common-network:administrative-state",
                             n.get("administrative-state", "inService")))
                not in ("inService", "")]

    out.update({
        "nodes": len(nodes),
        "links": len(links),
        "links_note": ("OpenROADM links are unidirectional, so a bidirectional "
                       "fibre appears twice"),
        "node_types": by_kind,
        "termination_points": sum(
            len(n.get("ietf-network-topology:termination-point") or []) for n in nodes),
        "node_ids": [str(n.get("node-id", "")) for n in nodes[:SAMPLE]],
    })
    if len(nodes) > SAMPLE:
        out["node_ids_truncated"] = len(nodes) - SAMPLE
    if degraded:
        out["not_in_service"] = degraded[:SAMPLE]

    # Enough per node and per link to render a topology listing, rather than
    # only a count. Without this a caller can say "14 nodes, 32 links" and
    # nothing about what connects to what, which is the one thing an operator
    # looking at a topology is asking. Capped at DETAIL so the reason this
    # module exists is not undone — the full payload is still behind raw=True.
    out["node_detail"] = [_node_detail(n) for n in nodes[:DETAIL]]
    out["link_detail"] = [_link_detail(l) for l in links[:DETAIL]]
    if len(nodes) > DETAIL:
        out["node_detail_truncated"] = len(nodes) - DETAIL
    if len(links) > DETAIL:
        out["link_detail_truncated"] = len(links) - DETAIL
    out["raw_available"] = "call again with raw=true for the full payload"
    return out


def summarise_portmapping(payload: Any) -> Dict[str, Any]:
    """Per-node mapping counts, rather than every logical-connection-point."""
    # Two reply shapes, because there are two questions. Asking for every node
    # returns `{"transportpce-portmapping:network": {"nodes": [...]}}`; asking for
    # one returns `{"transportpce-portmapping:nodes": [ ... ]}` — a different key
    # at the top and no `network` wrapper. Unwrapping only the first meant a
    # per-node query reported "nodes: 0" for a node the same tool listed happily
    # when asked for all of them: not an error, an answer, and the wrong one.
    #
    # Matched by suffix rather than by spelling, as `_augmented` does for YANG
    # augmentations: the module prefix moves between releases and is not the part
    # that carries the meaning.
    root = payload
    nodes: Any = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.endswith(":nodes") or key == "nodes":
                nodes = value
                break
            if key.endswith(":network") or key == "network":
                root = value
                break
    if not nodes:
        if isinstance(root, dict):
            for key, value in root.items():
                if key.endswith("nodes") or key.endswith("node"):
                    nodes = value
                    break
        elif isinstance(root, list):
            nodes = root
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        nodes = []

    summary = []
    for node in nodes[:SAMPLE]:
        if not isinstance(node, dict):
            continue
        mappings = node.get("mapping") or []
        summary.append({
            "node-id": node.get("node-id", ""),
            "node-info": {k: v for k, v in (node.get("node-info") or {}).items()
                          if k in ("node-type", "node-vendor", "node-model",
                                   "openroadm-version", "node-clli")},
            "mappings": len(mappings) if isinstance(mappings, list) else 0,
        })
    out: Dict[str, Any] = {"nodes": len(nodes), "detail": summary}
    if len(nodes) > SAMPLE:
        out["truncated"] = len(nodes) - SAMPLE
    out["raw_available"] = "call again with raw=true for the full payload"
    return out


def summarise_path_computation(payload: Any) -> Dict[str, Any]:
    """The verdict and the hops, not every resource in the path description."""
    # RESTCONF returns the RPC output under the module-qualified key —
    # `transportpce-pce:output`, not `output`. Looking only for the bare name made
    # every reply unreadable, and the tool answered `ok: true` with "no output in
    # the reply" while TransportPCE had said, plainly,
    # `response-code 500, "No path found by PCE."`. A refusal reported as an
    # unreadable success is worse than either.
    out = None
    if isinstance(payload, dict):
        for key, value in payload.items():
            if (key == "output" or key.endswith(":output")) and isinstance(value, dict):
                out = value
                break
    if not isinstance(out, dict):
        return {"note": "the reply carried no RPC output", "raw_available": True}

    common = out.get("configuration-response-common") or {}
    summary: Dict[str, Any] = {
        "response-code": common.get("response-code"),
        "response-message": common.get("response-message"),
        "request-id": common.get("request-id"),
    }
    desc = out.get("response-parameters", {}).get("path-description") \
        if isinstance(out.get("response-parameters"), dict) else None
    if isinstance(desc, dict):
        for direction in ("aToZ-direction", "zToA-direction"):
            leg = desc.get(direction)
            if not isinstance(leg, dict):
                continue
            hops = leg.get(direction.split("-")[0]) or []
            summary[direction] = {
                "hops": len(hops) if isinstance(hops, list) else 0,
                "rate": leg.get("rate"),
                "modulation-format": leg.get("modulation-format"),
                "central-frequency": leg.get("central-frequency"),
                "width": leg.get("width"),
            }
    # The PCE signals a refusal inside a 200: `response-code` is its own status,
    # and 500 with "No path found" is an answer — just not the one asked for.
    # Reported as a success, a circuit that cannot be routed looked routable.
    code = str(summary.get("response-code") or "")
    summary["path_found"] = code.startswith("200")
    if not summary["path_found"]:
        summary["refused"] = (f"TransportPCE's PCE answered {code}: "
                              f"{summary.get('response-message') or 'no reason given'}")
    if "gnpy-response" in out:
        summary["gnpy_checked"] = True
    summary["raw_available"] = "call again with raw=true for the full path description"
    return summary


def summarise_services(payload: Any) -> Dict[str, Any]:
    """One line per service instead of every endpoint and topology reference."""
    root = payload
    if isinstance(payload, dict):
        for key in ("org-openroadm-service:service-list", "service-list"):
            if key in payload:
                root = payload[key]
                break
    services = root.get("services") if isinstance(root, dict) else root
    if not isinstance(services, list):
        services = []
    return {
        "services": len(services),
        "detail": [{
            "service-name": s.get("service-name"),
            "operational-state": s.get("operational-state"),
            "administrative-state": s.get("administrative-state"),
            "connection-type": s.get("connection-type"),
            "rate": (s.get("service-a-end") or {}).get("service-rate"),
        } for s in services[:SAMPLE] if isinstance(s, dict)],
        "truncated": max(0, len(services) - SAMPLE),
        "raw_available": "call again with raw=true for the full payload",
    }


def summarise_node_status(payload: Any, node_id: str = "") -> Dict[str, Any]:
    """Whether a mounted NETCONF node is actually connected."""
    nodes = payload.get("node") if isinstance(payload, dict) else None
    if isinstance(payload, dict) and "network-topology:node" in payload:
        nodes = payload["network-topology:node"]
    if isinstance(nodes, dict):
        nodes = [nodes]
    if not isinstance(nodes, list):
        nodes = [payload] if isinstance(payload, dict) else []

    out = []
    for node in nodes[:SAMPLE]:
        if not isinstance(node, dict):
            continue
        # The state of a mounted device lives inside `netconf-node`, not beside
        # `node-id`. Read from the top of the node it is simply absent, so every
        # device reports "unknown" while all of them are connected — which is
        # worse than an error, because it looks like an answer. Both shapes are
        # accepted: a flat one is what older controllers returned, and this has
        # to read whatever the deployment in front of it actually sends.
        inner = next((v for k, v in node.items()
                      if k.endswith("netconf-node") and isinstance(v, dict)), {})
        fields = {**node, **inner}
        status = next((v for k, v in fields.items()
                       if k.endswith("connection-status")), "unknown")
        caps = next((v for k, v in fields.items()
                     if k.endswith("available-capabilities")), {}) or {}
        listed = caps.get("available-capability") if isinstance(caps, dict) else []
        out.append({
            "node-id": node.get("node-id", node_id),
            "connection-status": status,
            "capabilities": len(listed) if isinstance(listed, list) else 0,
            "unavailable-capabilities": bool(
                next((v for k, v in fields.items()
                      if k.endswith("unavailable-capabilities")), None)),
        })
    return {"nodes": out, "raw_available": "call again with raw=true"}
