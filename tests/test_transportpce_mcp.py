"""
The TransportPCE MCP server, against mocked RESTCONF.

No TransportPCE was available when this was written, so everything here is built
from the published models and exercised against canned replies. What that buys:
the request bodies are checked against the YANG's own mandatory-leaf rules, and
the error paths are exercised against the shapes RESTCONF actually returns
(`ietf-restconf:errors`, and an RPC that fails inside an HTTP 200).

What it does not buy, and no unit test could: proof that a real TransportPCE
accepts these bodies. That is the smoke test in the README, for the session
where the controller exists.

The boundary under test is the read/write split. Reads answer; writes queue.
A write that could be issued by a tool call, or a parameter that skipped the
queue, would be the defect — so several tests here exist only to assert that
RESTCONF was never touched.
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from transportpce_mcp import schemas, summarise  # noqa: E402
from transportpce_mcp.config import RESTCONF_ROOTS, TransportPceConfig  # noqa: E402
from transportpce_mcp.gate import DEFAULT_TPCE_POLICIES, WriteGate  # noqa: E402
from transportpce_mcp.restconf import (RestconfClient, TransportPceError,  # noqa: E402
                                      TransportPceUnreachable, rpc_failed)
from transportpce_mcp.server import READ_TOOLS, WRITE_TOOLS, register_tools  # noqa: E402

A_END = {"service-format": "Ethernet", "service-rate": 100, "clli": "SITEA"}
Z_END = {"service-format": "Ethernet", "service-rate": 100, "clli": "SITEZ"}
A_FULL = {**A_END, "node-id": "ROADM-A", "tx-direction": "port-1",
          "rx-direction": "port-2"}
Z_FULL = {**Z_END, "node-id": "ROADM-Z", "tx-direction": "port-3",
          "rx-direction": "port-4"}


def run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self.text = text if text is not None else json.dumps(body or {})


class FakeHttp:
    """Records requests and replays canned replies, by (method, url-substring)."""

    def __init__(self, replies=None, error=None):
        self.replies = replies or {}
        self.error = error
        self.calls = []

    async def request(self, method, url, json=None, **kwargs):
        self.calls.append({"method": method, "url": url, "json": json,
                           "auth": kwargs.get("auth"),
                           "headers": kwargs.get("headers", {})})
        if self.error:
            raise self.error
        for fragment, reply in self.replies.items():
            if fragment in url:
                return reply
        return FakeResponse(404, {"ietf-restconf:errors": {"error": [
            {"error-tag": "data-missing", "error-message": "no such path"}]}})


def client_with(replies=None, error=None, **overrides):
    config = TransportPceConfig(base_url="http://tpce.test:8181",
                                username="admin", password="admin",
                                restconf_root="/rests", **overrides)
    http = FakeHttp(replies, error)
    return RestconfClient(config, client=http), http


class ConfigurationTests(unittest.TestCase):
    """Nothing about where TransportPCE lives may be baked in."""

    def env(self, **values):
        return mock.patch.dict(os.environ, values, clear=True)

    def test_defaults_describe_a_stock_karaf_not_a_fixed_host(self):
        with self.env():
            got = TransportPceConfig.from_env()
        self.assertEqual(got.base_url, "http://127.0.0.1:8181")
        self.assertEqual(got.username, "admin")

    def test_every_part_is_overridable(self):
        with self.env(TPCE_BASE_URL="https://odl.example:8443",
                      TPCE_USERNAME="svc", TPCE_PASSWORD="s3cret",
                      TPCE_VERIFY_TLS="false"):
            got = TransportPceConfig.from_env()
        self.assertEqual(got.base_url, "https://odl.example:8443")
        self.assertEqual(got.username, "svc")
        self.assertFalse(got.verify_tls)

    def test_host_and_port_compose_a_url(self):
        with self.env(TPCE_HOST="10.0.0.9", TPCE_PORT="8182"):
            self.assertEqual(TransportPceConfig.from_env().base_url,
                             "http://10.0.0.9:8182")

    def test_both_restconf_roots_are_selectable(self):
        """ODL served /restconf before RFC 8040 and /rests after. TransportPCE's
        own test harness still switches between them, so a server that hardcodes
        one is wrong against half the releases."""
        with self.env(TPCE_RESTCONF_VERSION="rfc8040"):
            self.assertEqual(TransportPceConfig.from_env().restconf_root, "/rests")
        with self.env(TPCE_RESTCONF_VERSION="draft02"):
            self.assertEqual(TransportPceConfig.from_env().restconf_root, "/restconf")
        self.assertEqual(set(RESTCONF_ROOTS), {"rfc8040", "draft02"})

    def test_an_unknown_restconf_version_is_refused_not_guessed(self):
        with self.env(TPCE_RESTCONF_VERSION="rfc9999"):
            with self.assertRaises(ValueError):
                TransportPceConfig.from_env()

    def test_odls_own_variable_names_are_honoured(self):
        with self.env(USE_ODL_ALT_RESTCONF_PORT="8282"):
            self.assertIn("8282", TransportPceConfig.from_env().base_url)

    def test_describe_never_leaks_the_password(self):
        with self.env(TPCE_PASSWORD="s3cret"):
            got = TransportPceConfig.from_env().describe()
        self.assertNotIn("s3cret", json.dumps(got))
        self.assertTrue(got["password_set"])

    def test_no_username_means_send_no_auth(self):
        from transportpce_mcp.config import auth_of
        self.assertIsNone(auth_of(TransportPceConfig(username="")))
        self.assertEqual(auth_of(TransportPceConfig(username="a", password="b")),
                         ("a", "b"))


class RestconfTests(unittest.TestCase):
    def test_a_read_goes_to_the_data_root_with_basic_auth(self):
        client, http = client_with({"portmapping": FakeResponse(200, {"ok": 1})})
        run(client.get("transportpce-portmapping:network"))
        call = http.calls[0]
        self.assertEqual(call["url"],
                         "http://tpce.test:8181/rests/data/transportpce-portmapping:network")
        self.assertEqual(call["auth"], ("admin", "admin"))

    def test_an_rpc_goes_to_the_operations_root(self):
        client, http = client_with({"operations": FakeResponse(200, {"output": {}})})
        run(client.rpc("transportpce-pce:path-computation-request", {"input": {}}))
        self.assertIn("/rests/operations/transportpce-pce:path-computation-request",
                      http.calls[0]["url"])

    def test_restconf_error_documents_are_read_not_just_the_status(self):
        """"HTTP 409" alone throws away the part that says why."""
        body = {"ietf-restconf:errors": {"error": [
            {"error-tag": "missing-element",
             "error-message": "Mandatory leaf service-name is missing"}]}}
        client, _ = client_with({"data": FakeResponse(409, body)})
        with self.assertRaises(TransportPceError) as caught:
            run(client.get("anything"))
        self.assertIn("Mandatory leaf service-name", str(caught.exception))

    def test_a_401_says_which_variables_to_check(self):
        client, _ = client_with({"data": FakeResponse(401, {})})
        with self.assertRaises(TransportPceError) as caught:
            run(client.get("anything"))
        self.assertIn("TPCE_USERNAME", str(caught.exception))

    def test_unreachable_is_a_different_exception_from_refused(self):
        client, _ = client_with(error=OSError("connection refused"))
        with self.assertRaises(TransportPceUnreachable):
            run(client.get("anything"))

    def test_an_rpc_that_fails_inside_a_200_is_a_failure(self):
        """OpenROADM carries the verdict in the body. Reading the 200 as success
        is how a refused service-create gets reported as provisioned."""
        body = {"output": {"configuration-response-common": {
            "response-code": "500", "response-message": "no path available"}}}
        client, _ = client_with({"operations": FakeResponse(200, body)})
        with self.assertRaises(TransportPceError) as caught:
            run(client.rpc("transportpce-pce:path-computation-request", {}))
        self.assertIn("no path available", str(caught.exception))

    def test_a_successful_rpc_body_is_not_treated_as_a_failure(self):
        for code in ("200", "OK", "Successful"):
            self.assertIsNone(rpc_failed({"output": {
                "configuration-response-common": {"response-code": code}}}))

    def test_health_distinguishes_started_from_feature_installed(self):
        """A Karaf that is up but has not installed odl-transportpce answers TCP
        and 404s every model path. "reachable" would misreport that."""
        client, _ = client_with({"portmapping": FakeResponse(404, {})})
        got = run(client.health())
        self.assertTrue(got["reachable"])
        self.assertFalse(got["transportpce_installed"])

    def test_json_content_type_is_restconfs_own(self):
        client, http = client_with({"operations": FakeResponse(200, {"output": {}})})
        run(client.rpc("x:y", {"input": {}}))
        self.assertEqual(http.calls[0]["headers"]["Content-Type"],
                         "application/yang-data+json")


class PathComputationShapeTests(unittest.TestCase):
    """Checked against transportpce-pce@2024-02-05 and
    transportpce-common-service-path-types@2022-01-18."""

    def test_mandatory_leaves_are_present(self):
        body = schemas.path_computation_request("svc", A_END, Z_END)["input"]
        self.assertEqual(body["service-name"], "svc")
        self.assertIn("resource-reserve", body)
        self.assertEqual(body["service-handler-header"]["request-id"], "svc")

    def test_resource_reserve_defaults_to_false(self):
        """It is mandatory, so something is sent; True would hold resources in
        the controller's topology until cancelled, which an advisory
        computation must not do."""
        self.assertIs(
            schemas.path_computation_request("s", A_END, Z_END)["input"]["resource-reserve"],
            False)

    def test_a_missing_endpoint_is_named_not_passed_through(self):
        with self.assertRaises(schemas.ValidationError) as caught:
            schemas.path_computation_request("s", {}, Z_END)
        self.assertIn("service-a-end", str(caught.exception))

    def test_clli_is_mandatory(self):
        with self.assertRaises(schemas.ValidationError) as caught:
            schemas.path_computation_request(
                "s", {"service-format": "Ethernet", "service-rate": 100}, Z_END)
        self.assertIn("clli", str(caught.exception))

    def test_service_rate_is_mandatory_only_when_the_format_is_not_oms(self):
        """`when "../service-format!='OMS'"`. Demanding it always would reject a
        valid ROADM-line request."""
        schemas.path_computation_request(
            "s", {"service-format": "OMS", "clli": "A"},
            {"service-format": "OMS", "clli": "Z"})
        with self.assertRaises(schemas.ValidationError):
            schemas.path_computation_request(
                "s", {"service-format": "Ethernet", "clli": "A"}, Z_END)

    def test_a_rate_with_an_oms_format_is_rejected(self):
        with self.assertRaises(schemas.ValidationError):
            schemas.path_computation_request(
                "s", {"service-format": "OMS", "clli": "A", "service-rate": 100},
                {"service-format": "OMS", "clli": "Z"})

    def test_node_id_is_optional_for_computation(self):
        """The docs page lists node-id among an endpoint's required fields; the
        model does not mark it mandatory. The model wins."""
        schemas.path_computation_request("s", A_END, Z_END)

    def test_an_unknown_service_format_is_caught_here_not_by_restconf(self):
        with self.assertRaises(schemas.ValidationError) as caught:
            schemas.path_computation_request(
                "s", {**A_END, "service-format": "Etherent"}, Z_END)
        self.assertIn("Ethernet", str(caught.exception))

    def test_the_metric_enum_is_the_models(self):
        schemas.path_computation_request("s", A_END, Z_END,
                                         pce_routing_metric="hop-count")
        with self.assertRaises(schemas.ValidationError):
            schemas.path_computation_request("s", A_END, Z_END,
                                             pce_routing_metric="shortest")

    def test_a_bare_port_name_is_wrapped_as_the_container_it_is(self):
        """tx-direction is a container, not a leaf; a bare string would be a
        schema error at the far end."""
        body = schemas.path_computation_request(
            "s", {**A_END, "tx-direction": "port-1"}, Z_END)
        self.assertEqual(body["input"]["service-a-end"]["tx-direction"],
                         {"port": {"port-name": "port-1"}})

    def test_an_already_structured_direction_is_left_alone(self):
        full = {"port": {"port-name": "p", "port-device-name": "d"}}
        body = schemas.path_computation_request(
            "s", {**A_END, "tx-direction": full}, Z_END)
        self.assertEqual(body["input"]["service-a-end"]["tx-direction"], full)


class ServiceCreateShapeTests(unittest.TestCase):
    def test_provisioning_additionally_requires_ports_and_node(self):
        """The PCE can compute without them; the renderer configures real ports
        and cannot."""
        with self.assertRaises(schemas.ValidationError) as caught:
            schemas.service_create_request("svc", A_END, Z_END)
        message = str(caught.exception)
        for field in ("node-id", "tx-direction", "rx-direction"):
            self.assertIn(field, message)

    def test_a_complete_request_builds(self):
        body = schemas.service_create_request("svc", A_FULL, Z_FULL)["input"]
        self.assertEqual(body["sdnc-request-header"]["rpc-action"], "service-create")
        self.assertEqual(body["connection-type"], "service")
        self.assertEqual(body["service-a-end"]["node-id"], "ROADM-A")

    def test_the_connection_type_enum_is_checked(self):
        with self.assertRaises(schemas.ValidationError):
            schemas.service_create_request("s", A_FULL, Z_FULL,
                                           connection_type="whatever")

    def test_delete_needs_only_a_name(self):
        body = schemas.service_delete_request("svc")["input"]
        self.assertEqual(body["service-delete-req-info"]["service-name"], "svc")
        self.assertEqual(body["sdnc-request-header"]["rpc-action"], "service-delete")

    def test_a_netconf_mount_validates_its_port(self):
        schemas.netconf_node_body("n", "10.0.0.1", 830, "u", "p")
        with self.assertRaises(schemas.ValidationError):
            schemas.netconf_node_body("n", "10.0.0.1", 99999, "u", "p")

    def test_a_netconf_mount_nests_its_connection_parameters(self):
        """The flat shape is a 400; this is the one a live controller takes.

        Most documentation and most older examples show host, port, username
        and password as siblings of node-id. TransportPCE 13.0.0 answers that
        with a bare HTTP 400 and the nested shape with 201.
        """
        body = schemas.netconf_node_body("ROADM-A1", "10.0.0.1", 17841, "admin", "admin")
        node = body["node"][0]
        self.assertEqual(node["node-id"], "ROADM-A1")
        inner = node["netconf-node-topology:netconf-node"]
        self.assertEqual(inner["netconf-node-topology:host"], "10.0.0.1")
        self.assertEqual(inner["netconf-node-topology:port"], 17841)
        # credentials one level deeper again
        creds = inner["netconf-node-topology:login-password-unencrypted"]
        self.assertEqual(creds["netconf-node-topology:username"], "admin")
        self.assertEqual(creds["netconf-node-topology:password"], "admin")
        # and nothing left at the top to be read as the flat shape
        self.assertNotIn("netconf-node-topology:host", node)


class NodeStatusShapeTests(unittest.TestCase):
    """Where a mounted device's state actually lives.

    `connection-status` is nested inside `netconf-node-topology:netconf-node`,
    not beside `node-id`. Read from the top of the node it is simply absent, so
    every device reports "unknown" while all of them are connected — which is
    worse than an error, because it looks like an answer.
    """

    LIVE = {"node": [{
        "node-id": "ROADM-A1",
        "netconf-node-topology:netconf-node": {
            "host": "127.0.0.1",
            "port": 17841,
            "connection-status": "connected",
            "login-password-unencrypted": {"username": "admin", "password": "admin"},
            "available-capabilities": {"available-capability": [{"capability": "a"},
                                                                {"capability": "b"}]},
            "unavailable-capabilities": {"unavailable-capability": [{"capability": "c"}]},
        },
    }]}

    def test_it_reads_the_nested_status(self):
        got = summarise.summarise_node_status(self.LIVE)
        self.assertEqual(got["nodes"][0]["connection-status"], "connected")
        self.assertEqual(got["nodes"][0]["node-id"], "ROADM-A1")

    def test_it_counts_nested_capabilities(self):
        got = summarise.summarise_node_status(self.LIVE)
        self.assertEqual(got["nodes"][0]["capabilities"], 2)
        self.assertTrue(got["nodes"][0]["unavailable-capabilities"])

    def test_it_says_where_the_device_is_but_not_how_to_log_in(self):
        got = summarise.summarise_node_status(self.LIVE)["nodes"][0]
        self.assertEqual((got["host"], got["port"]), ("127.0.0.1", 17841))
        self.assertNotIn("admin", str(got))

    def test_a_flat_node_still_reads(self):
        # older controllers returned it flat; this has to read whatever the
        # deployment in front of it sends
        got = summarise.summarise_node_status(
            {"node": [{"node-id": "X", "connection-status": "connecting"}]})
        self.assertEqual(got["nodes"][0]["connection-status"], "connecting")

    def test_a_node_with_no_status_says_unknown_rather_than_guessing(self):
        got = summarise.summarise_node_status({"node": [{"node-id": "X"}]})
        self.assertEqual(got["nodes"][0]["connection-status"], "unknown")


class PortmappingListsEveryNode(unittest.TestCase):
    """The portmapping summary is the device list; a sample of it is a wrong one."""

    def test_sixteen_nodes_are_sixteen_entries(self):
        payload = {"transportpce-portmapping:network": {"nodes": [
            {"node-id": f"N{i}", "node-info": {"node-type": "rdm"},
             "mapping": [{"logical-connection-point": "x"}] * 40} for i in range(16)]}}
        got = summarise.summarise_portmapping(payload)
        self.assertEqual(got["nodes"], 16)
        self.assertEqual([n["node-id"] for n in got["detail"]], [f"N{i}" for i in range(16)])
        self.assertNotIn("truncated", got)


class SummaryTests(unittest.TestCase):
    """A full OpenROADM topology is megabytes; it must not be the default."""

    TOPOLOGY = {"ietf-network:networks": {"network": [{
        "network-id": "openroadm-topology",
        "node": [
            {"node-id": "ROADM-A-SRG1",
             "org-openroadm-common-network:node-type": "SRG",
             "ietf-network-topology:termination-point": [{"tp-id": "t1"},
                                                         {"tp-id": "t2"}]},
            {"node-id": "ROADM-A-DEG1",
             "org-openroadm-common-network:node-type": "DEGREE"},
            {"node-id": "XPDR-A1-XPDR1",
             "org-openroadm-common-network:node-type": "XPONDER",
             "org-openroadm-common-network:administrative-state": "outOfService"},
        ],
        "ietf-network-topology:link": [{"link-id": "l1"}, {"link-id": "l2"}],
    }]}}

    def test_counts_and_types_rather_than_the_payload(self):
        got = summarise.summarise_topology(self.TOPOLOGY, "openroadm-topology")
        self.assertEqual(got["nodes"], 3)
        self.assertEqual(got["links"], 2)
        self.assertEqual(got["node_types"],
                         {"SRG": 1, "DEGREE": 1, "XPONDER": 1})
        self.assertEqual(got["termination_points"], 2)

    def test_a_node_out_of_service_is_surfaced(self):
        got = summarise.summarise_topology(self.TOPOLOGY)
        self.assertEqual(got["not_in_service"], ["XPDR-A1-XPDR1"])

    def test_the_link_count_says_it_is_unidirectional(self):
        """OpenROADM links are one-way, so a bidirectional fibre appears twice —
        a trap shared by most controllers that model links one way."""
        self.assertIn("unidirectional",
                      summarise.summarise_topology(self.TOPOLOGY)["links_note"])

    def test_every_restconf_wrapping_is_unwrapped(self):
        inner = self.TOPOLOGY["ietf-network:networks"]["network"][0]
        for payload in (self.TOPOLOGY,
                        {"network": [inner]},
                        {"ietf-network:network": [inner]},
                        inner):
            self.assertEqual(summarise.summarise_topology(payload)["nodes"], 3,
                             repr(payload)[:60])

    def test_an_empty_reply_does_not_crash_or_lie(self):
        got = summarise.summarise_topology({})
        self.assertEqual(got["nodes"], 0)
        self.assertIn("note", got)

    def test_the_node_type_key_is_matched_by_suffix(self):
        """The augmentation prefix has moved between releases; matching one
        spelling would silently report every node as unknown."""
        got = summarise.summarise_topology({"node": [
            {"node-id": "n", "some-future-module:node-type": "SRG"}]})
        self.assertEqual(got["node_types"], {"SRG": 1})

    def test_path_computation_keeps_the_verdict_and_drops_the_resources(self):
        payload = {"output": {
            "configuration-response-common": {"response-code": "200",
                                              "response-message": "Path is calculated"},
            "response-parameters": {"path-description": {
                "aToZ-direction": {"aToZ": [{"id": "1"}, {"id": "2"}],
                                   "rate": 100, "modulation-format": "dp-qpsk",
                                   "central-frequency": 196.1},
                "zToA-direction": {"zToA": [{"id": "1"}], "rate": 100}}}}}
        got = summarise.summarise_path_computation(payload)
        self.assertEqual(got["response-message"], "Path is calculated")
        self.assertEqual(got["aToZ-direction"]["hops"], 2)
        self.assertEqual(got["aToZ-direction"]["modulation-format"], "dp-qpsk")

    def test_services_are_one_line_each(self):
        got = summarise.summarise_services({"org-openroadm-service:service-list": {
            "services": [{"service-name": "s1", "operational-state": "inService",
                          "service-a-end": {"service-rate": 100}}]}})
        self.assertEqual(got["services"], 1)
        self.assertEqual(got["detail"][0]["rate"], 100)

    def test_node_status_reports_the_connection_state(self):
        got = summarise.summarise_node_status({"network-topology:node": [
            {"node-id": "ROADM-A",
             "netconf-node-topology:connection-status": "connected",
             "netconf-node-topology:available-capabilities": {
                 "available-capability": [{"capability": "a"}, {"capability": "b"}]}}]})
        self.assertEqual(got["nodes"][0]["connection-status"], "connected")
        self.assertEqual(got["nodes"][0]["capabilities"], 2)

    def test_large_lists_are_truncated_with_a_count(self):
        payload = {"node": [{"node-id": f"n{i}"} for i in range(40)]}
        got = summarise.summarise_topology(payload)
        self.assertEqual(got["nodes"], 40)
        self.assertLessEqual(len(got["node_ids"]), summarise.SAMPLE)
        self.assertEqual(got["node_ids_truncated"], 40 - summarise.SAMPLE)


class FakeQueue:
    def __init__(self):
        self._items = {}
        self.saved = 0

    def add(self, kind, device_id, reason, instruction=None, **_):
        item_id = f"item-{len(self._items) + 1}"
        self._items[item_id] = {"id": item_id, "kind": kind, "device_id": device_id,
                                "reason": reason, "instruction": instruction,
                                "state": "pending"}
        return self._items[item_id]

    def _save(self):
        self.saved += 1


class FakeRate:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.recorded = []

    def check(self, device_id, action, limit):
        return {"allowed": self.allowed, "used": 0 if self.allowed else limit,
                "limit": limit}

    def record(self, device_id, action):
        self.recorded.append((device_id, action))


class FakeAudit:
    def __init__(self):
        self.entries = []

    def write(self, entry):
        self.entries.append(entry)
        return entry


def gate_with(allowed=True, actions=None):
    policy = mock.Mock()
    policy.get.return_value = mock.Mock(actions=actions or {})
    return WriteGate(policy_store=policy, rate_limiter=FakeRate(allowed),
                     queue=FakeQueue(), audit=FakeAudit())


class WriteGateTests(unittest.TestCase):
    """A write is requested, never performed, by a tool call."""

    def test_a_request_is_queued_and_nothing_is_sent(self):
        gate = gate_with()
        got = gate.request("tpce_service_create", "create svc",
                           "org-openroadm-service:service-create",
                           {"input": {}}, target="svc")
        self.assertEqual(got["status"], "queued_for_approval")
        self.assertEqual(got["approval_id"], "item-1")
        self.assertEqual(gate._queue._items["item-1"]["state"], "pending")

    def test_the_queued_item_records_which_controller_it_is_for(self):
        """The queue is shared with device writes; an applier has to be able to
        tell a TransportPCE RPC from a NETCONF leaf write."""
        gate = gate_with()
        gate.request("tpce_service_create", "x", "rpc", {}, target="svc")
        instruction = gate._queue._items["item-1"]["instruction"]
        self.assertEqual(instruction["controller"], "transportpce")
        self.assertEqual(instruction["rpc"], "rpc")

    def test_queuing_is_audited(self):
        gate = gate_with()
        gate.request("tpce_service_delete", "x", "rpc", {}, target="svc")
        self.assertEqual(gate._audit.entries[0]["event"], "tpce_write_queued")

    def test_the_rate_limit_refuses_before_queuing(self):
        gate = gate_with(allowed=False)
        got = gate.request("tpce_service_create", "x", "rpc", {}, target="svc")
        self.assertEqual(got["status"], "refused")
        self.assertIn("rate limit", got["reason"])
        self.assertEqual(gate._queue._items, {})

    def test_every_southbound_action_requires_approval_by_default(self):
        gate = gate_with()
        for action in DEFAULT_TPCE_POLICIES:
            self.assertTrue(gate.policy_for(action)["require_human_approval"], action)

    def test_no_southbound_action_is_classified_low_risk(self):
        gate = gate_with()
        for action, policy in DEFAULT_TPCE_POLICIES.items():
            self.assertEqual(policy["risk_tier"], "high", action)

    def test_deleting_is_gated_more_tightly_than_creating(self):
        """Deleting drops traffic that is currently carried."""
        self.assertLess(DEFAULT_TPCE_POLICIES["tpce_service_delete"]["max_changes_per_hour"],
                        DEFAULT_TPCE_POLICIES["tpce_service_create"]["max_changes_per_hour"])

    def test_an_unclassified_action_is_hard_gated_and_not_overridable(self):
        """A new action must be classified deliberately, not inherit whatever
        the nearest rule happens to say."""
        got = gate_with().policy_for("tpce_something_new")
        self.assertTrue(got["require_human_approval"])
        self.assertFalse(got["overridable"])
        self.assertEqual(got["max_changes_per_hour"], 0)

    def test_the_policy_store_overrides_the_defaults(self):
        override = mock.Mock(risk_tier="low", require_human_approval=False,
                             max_changes_per_hour=99, overridable=True)
        gate = gate_with(actions={"tpce_service_create": override})
        got = gate.policy_for("tpce_service_create")
        self.assertFalse(got["require_human_approval"])
        self.assertEqual(got["source"], "policy store")

    def test_pending_items_are_not_applied(self):
        gate = gate_with()
        gate.request("tpce_service_create", "x", "rpc", {}, target="svc")
        client = mock.Mock()
        got = run(gate.apply_approved(client))
        self.assertEqual(got, {"applied": [], "failed": []})
        client.rpc.assert_not_called()

    def test_an_approved_item_is_applied_once_and_audited(self):
        gate = gate_with()
        gate.request("tpce_service_create", "x", "rpc-name", {"input": {}},
                     target="svc")
        gate._queue._items["item-1"]["state"] = "approved"

        async def rpc(name, body):
            return {"output": {}}

        client = mock.Mock(rpc=mock.Mock(side_effect=rpc))
        got = run(gate.apply_approved(client))
        self.assertEqual(got["applied"], ["item-1"])
        self.assertEqual(client.rpc.call_args[0][0], "rpc-name")
        # and not again
        self.assertEqual(run(gate.apply_approved(client))["applied"], [])
        self.assertIn("tpce_write_applied",
                      [e["event"] for e in gate._audit.entries])

    def test_a_failed_application_is_recorded_not_swallowed(self):
        gate = gate_with()
        gate.request("tpce_service_create", "x", "rpc", {}, target="svc")
        gate._queue._items["item-1"]["state"] = "approved"

        async def boom(name, body):
            raise TransportPceError("no path available")

        got = run(gate.apply_approved(mock.Mock(rpc=mock.Mock(side_effect=boom))))
        self.assertEqual(got["applied"], [])
        self.assertIn("no path available", got["failed"][0]["error"])
        self.assertIn("tpce_write_failed", [e["event"] for e in gate._audit.entries])

    def test_only_this_controllers_items_are_applied(self):
        """The queue is shared. Draining somebody else's device write from here
        would issue it down the wrong southbound path."""
        gate = gate_with()
        gate._queue._items["other"] = {
            "id": "other", "state": "approved",
            "instruction": {"controller": "teraflow", "rpc": "x", "body": {}}}
        self.assertEqual(gate.approved_items(), [])


class ToolSurfaceTests(unittest.TestCase):
    """The split, and the shape of what a tool returns."""

    def surface(self, replies=None, error=None, gate=None):
        client, http = client_with(replies, error)
        tools = {}

        class Recorder:
            def tool(inner_self, *a, **k):
                def wrap(fn):
                    tools[fn.__name__] = fn
                    return fn
                return wrap

        register_tools(Recorder(), client, gate or gate_with())
        return tools, http

    def test_every_declared_tool_is_registered(self):
        tools, _ = self.surface()
        for name in READ_TOOLS + WRITE_TOOLS:
            self.assertIn(name, tools, name)

    def test_the_two_sets_do_not_overlap(self):
        self.assertEqual(set(READ_TOOLS) & set(WRITE_TOOLS), set())

    def test_every_tool_is_prefixed(self):
        """A bare `get_topology` would have to be namespaced by the
        client anyway, because `get_topology` does not say whose network."""
        for name in READ_TOOLS + WRITE_TOOLS:
            self.assertTrue(name.startswith("tpce_"), name)

    def test_every_reply_is_json(self):
        """A server returning `str(dict)` is not parseable; doing that
        forces every client to carry a parser fallback."""
        tools, _ = self.surface({"portmapping": FakeResponse(200, {"x": 1})})
        got = run(tools["tpce_get_portmapping"]())
        self.assertIsInstance(json.loads(got), dict)

    def test_a_failure_is_data_not_prose(self):
        tools, _ = self.surface(error=OSError("refused"))
        got = json.loads(run(tools["tpce_get_topology"]()))
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"], "unreachable")

    def test_topology_is_summarised_by_default_and_raw_on_request(self):
        payload = SummaryTests.TOPOLOGY
        tools, _ = self.surface({"ietf-network:networks": FakeResponse(200, payload)})
        summary = json.loads(run(tools["tpce_get_topology"]()))["data"]
        self.assertEqual(summary["nodes"], 3)
        self.assertNotIn("ietf-network:networks", summary)
        full = json.loads(run(tools["tpce_get_topology"](raw=True)))["data"]
        self.assertIn("ietf-network:networks", full)

    def test_an_unknown_network_is_refused_before_the_request(self):
        tools, http = self.surface()
        got = json.loads(run(tools["tpce_get_topology"](network="my-network")))
        self.assertFalse(got["ok"])
        self.assertEqual(got["error"], "invalid_request")
        self.assertEqual(http.calls, [])

    def test_node_status_reads_the_operational_datastore(self):
        """Connection status only exists in the non-config view."""
        tools, http = self.surface({"topology-netconf": FakeResponse(200, {})})
        run(tools["tpce_get_node_status"]("ROADM-A"))
        self.assertIn("content=nonconfig", http.calls[0]["url"])

    def test_no_services_yet_is_an_answer_not_an_error(self):
        tools, _ = self.surface({"service-list": FakeResponse(404, {})})
        got = json.loads(run(tools["tpce_list_services"]()))
        self.assertTrue(got["ok"])
        self.assertEqual(got["data"]["services"], 0)

    def test_compute_path_never_reserves(self):
        tools, http = self.surface(
            {"operations": FakeResponse(200, {"output": {}})})
        got = json.loads(run(tools["tpce_compute_path"]("svc", A_END, Z_END)))
        self.assertTrue(got["advisory"])
        self.assertIs(got["reserved"], False)
        self.assertIs(http.calls[0]["json"]["input"]["resource-reserve"], False)

    def test_compute_path_reports_a_bad_endpoint_without_calling_out(self):
        tools, http = self.surface()
        got = json.loads(run(tools["tpce_compute_path"]("svc", {}, Z_END)))
        self.assertEqual(got["error"], "invalid_request")
        self.assertIn("service-a-end", got["detail"])
        self.assertEqual(http.calls, [])

    def test_the_shapes_tool_needs_no_controller(self):
        tools, http = self.surface(error=OSError("nothing running"))
        got = json.loads(run(tools["tpce_describe_models"]()))
        self.assertTrue(got["ok"])
        self.assertIn("Ethernet", got["data"]["service_formats"])
        self.assertEqual(http.calls, [])


class WriteToolsNeverWriteTests(unittest.TestCase):
    """The constraint the whole design turns on."""

    def surface(self, gate):
        client, http = client_with({"operations": FakeResponse(200, {"output": {}}),
                                    "data": FakeResponse(200, {})})
        tools = {}

        class Recorder:
            def tool(inner_self, *a, **k):
                def wrap(fn):
                    tools[fn.__name__] = fn
                    return fn
                return wrap

        register_tools(Recorder(), client, gate)
        return tools, http

    def test_service_create_queues_and_sends_nothing(self):
        gate = gate_with()
        tools, http = self.surface(gate)
        got = json.loads(run(tools["tpce_service_create"]("svc", A_FULL, Z_FULL)))
        self.assertEqual(got["status"], "queued_for_approval")
        self.assertEqual(http.calls, [], "a tool call reached RESTCONF")

    def test_service_delete_queues_and_sends_nothing(self):
        gate = gate_with()
        tools, http = self.surface(gate)
        got = json.loads(run(tools["tpce_service_delete"]("svc")))
        self.assertEqual(got["status"], "queued_for_approval")
        self.assertEqual(http.calls, [])

    def test_device_connect_queues_and_sends_nothing(self):
        gate = gate_with()
        tools, http = self.surface(gate)
        got = json.loads(run(tools["tpce_device_connect"]("n", "10.0.0.1", 830)))
        self.assertEqual(got["status"], "queued_for_approval")
        # The verb lives in the queued instruction, which is what the applier
        # reads — see ApplierDispatchTests.
        self.assertEqual(gate._queue._items["item-1"]["instruction"]["method"], "PUT")
        self.assertEqual(http.calls, [])

    def test_no_write_tool_takes_an_argument_that_skips_the_queue(self):
        """A `force`/`approved`/`dry_run` parameter is one the model fills in
        itself, which makes the gate a suggestion rather than a gate."""
        import inspect
        tools, _ = self.surface(gate_with())
        forbidden = {"force", "approved", "approve", "dry_run", "confirm",
                     "skip_approval", "allow_write", "execute", "apply",
                     "immediate", "now"}
        for name in WRITE_TOOLS:
            params = set(inspect.signature(tools[name]).parameters)
            self.assertEqual(params & forbidden, set(),
                             f"{name} exposes an approval bypass")

    def test_the_applier_is_not_a_tool(self):
        """If a model could drain the queue, the queue would be a delay."""
        tools, _ = self.surface(gate_with())
        for name in tools:
            self.assertNotIn("apply", name)
            self.assertNotIn("drain", name)

    def test_an_invalid_write_is_refused_before_it_is_queued(self):
        gate = gate_with()
        tools, http = self.surface(gate)
        got = json.loads(run(tools["tpce_service_create"]("svc", A_END, Z_END)))
        self.assertEqual(got["error"], "invalid_request")
        self.assertEqual(gate._queue._items, {})
        self.assertEqual(http.calls, [])

    def test_a_rate_limited_write_says_so_and_queues_nothing(self):
        gate = gate_with(allowed=False)
        tools, http = self.surface(gate)
        got = json.loads(run(tools["tpce_service_create"]("svc", A_FULL, Z_FULL)))
        self.assertEqual(got["status"], "refused")
        self.assertEqual(http.calls, [])

    def test_the_reply_says_that_nothing_was_sent(self):
        """An operator reading the transcript should not have to infer it."""
        tools, _ = self.surface(gate_with())
        got = json.loads(run(tools["tpce_service_create"]("svc", A_FULL, Z_FULL)))
        self.assertIn("Nothing was sent", got["what_happens_next"])


# A read verb, at the start of the name or of the segment after the prefix.
# This mirrors the gate an MCP client is expected to apply: a client that mounts
# somebody else's tools cannot read a description to decide whether a tool
# mutates, so it matches the verb. These names are built to survive that.
_READ_VERBS = ("list", "get", "health", "verify", "compute", "describe", "show",
               "read")
_MUTATING_WORDS = frozenset({
    "create", "delete", "remove", "add", "set", "update", "upload", "clear",
    "apply", "submit", "withdraw", "request", "connect", "configure",
    "provision", "reserve", "activate", "deactivate", "modify", "execute",
})


def _reads_by_name(name: str) -> bool:
    if _MUTATING_WORDS.intersection(name.lower().split("_")):
        return False
    parts = name.split("_", 1)
    stem = parts[1] if len(parts) > 1 else parts[0]
    return name.startswith(_READ_VERBS) or stem.startswith(_READ_VERBS)


class NamesSurviveAClientGateTests(unittest.TestCase):
    """A read-only MCP client must be able to tell these apart by name alone.

    Any client mounting a third-party server has to decide what it will admit
    without trusting a tool's own description, which means matching the verb. So
    a read tool's name has to lead with a read verb — in the first or second
    segment, since only one prefix is usually stripped — and a write tool's name
    must not.
    """

    def test_every_read_tool_would_be_admitted(self):
        for name in READ_TOOLS:
            self.assertTrue(_reads_by_name(name), name)

    def test_every_write_tool_would_be_refused(self):
        """Defence in depth: this server queues rather than writes, and a client
        should refuse to mount the tool even if it did not."""
        for name in WRITE_TOOLS:
            self.assertFalse(_reads_by_name(name), name)

    def test_the_verb_is_in_the_first_or_second_segment(self):
        """`tpce_get_tapi_topology_details`, not `tpce_tapi_get_topology_details`
        — a client that strips one prefix segment would miss the verb in the
        third."""
        for name in READ_TOOLS:
            self.assertIn(name.split("_")[1], _READ_VERBS + ("describe",), name)


if __name__ == "__main__":
    unittest.main()


class ApplierDispatchTests(unittest.TestCase):
    """Two kinds of southbound write share one queue.

    A service is an RPC; mounting a device is a PUT to a data path. The queued
    item says which, so the applier does not infer it from the action name.
    """

    def queued(self, **extra):
        gate = gate_with()
        gate.request("tpce_device_connect", "mount n", rpc="",
                     body={"network-topology:node": []}, target="n",
                     **extra)
        gate._queue._items["item-1"]["state"] = "approved"
        return gate

    def test_a_put_item_is_applied_as_a_put(self):
        gate = self.queued(method="PUT", path="network-topology:.../node=n")

        async def put(path, body):
            return {}

        client = mock.Mock(put=mock.Mock(side_effect=put),
                           rpc=mock.Mock(side_effect=AssertionError("wrong verb")))
        got = run(gate.apply_approved(client))
        self.assertEqual(got["applied"], ["item-1"])
        self.assertEqual(client.put.call_args[0][0], "network-topology:.../node=n")

    def test_an_item_naming_neither_is_refused_rather_than_guessed(self):
        gate = self.queued()
        client = mock.Mock()
        got = run(gate.apply_approved(client))
        self.assertEqual(got["applied"], [])
        self.assertIn("neither an rpc nor a PUT path", got["failed"][0]["error"])
        client.rpc.assert_not_called()
        client.put.assert_not_called()

    def test_device_connect_records_the_verb_at_queue_time(self):
        """Recorded in the instruction, not only in the reply to the caller —
        the applier reads the queue, not the transcript."""
        client, http = client_with()
        gate = gate_with()
        tools = {}

        class Recorder:
            def tool(inner_self, *a, **k):
                def wrap(fn):
                    tools[fn.__name__] = fn
                    return fn
                return wrap

        register_tools(Recorder(), client, gate)
        run(tools["tpce_device_connect"]("ROADM-A", "10.0.0.1", 830, "u", "p"))
        instruction = gate._queue._items["item-1"]["instruction"]
        self.assertEqual(instruction["method"], "PUT")
        self.assertIn("node=ROADM-A", instruction["path"])
        self.assertEqual(http.calls, [])


class StandaloneStoresTests(unittest.TestCase):
    """Standalone, the gate falls back to this package's own stores.

    The point is that the fallback is the same semantics, not a lighter version.
    A standalone install that auto-approved, or skipped the rate limit, would
    remove the property the write half of this server is built on — so these
    tests check the properties, not the plumbing.
    """

    def setUp(self):
        import tempfile
        from transportpce_mcp import stores
        self.dir = tempfile.mkdtemp(prefix="tpce-stores-")
        self.queue = stores.LocalApprovalQueue(os.path.join(self.dir, "q.json"))
        self.audit = stores.LocalAuditLog(os.path.join(self.dir, "a.jsonl"))
        self.rate = stores.LocalRateLimiter()
        self.policy = stores.LocalPolicyStore(os.path.join(self.dir, "p.json"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def gate(self):
        return WriteGate(policy_store=self.policy, rate_limiter=self.rate,
                         queue=self.queue, audit=self.audit)

    def test_a_queued_item_starts_pending_not_approved(self):
        item = self.queue.add("pending_approval", "transportpce", "why",
                              {"action": "tpce_service_create"})
        self.assertEqual(item["state"], "pending")

    def test_the_queue_survives_a_restart(self):
        """An approval that evaporated on restart would push operators towards
        approving in bulk."""
        from transportpce_mcp import stores
        item = self.queue.add("pending_approval", "transportpce", "why", {})
        reopened = stores.LocalApprovalQueue(self.queue.path)
        self.assertEqual(len(reopened.pending()), 1)
        self.assertEqual(reopened.get(item["id"])["reason"], "why")

    def test_deciding_twice_does_not_apply_twice(self):
        item = self.queue.add("pending_approval", "transportpce", "why", {})
        self.queue.decide(item["id"], True)
        again = self.queue.decide(item["id"], True)
        self.assertEqual(again["state"], "approved")
        self.assertEqual(self.queue.counts().get("approved"), 1)

    def test_a_rejected_item_is_never_applied(self):
        gate = self.gate()
        got = gate.request("tpce_service_create", "x", "rpc", {}, target="svc")
        self.queue.decide(got["approval_id"], False)
        client = mock.Mock()
        self.assertEqual(run(gate.apply_approved(client))["applied"], [])
        client.rpc.assert_not_called()

    def test_the_local_policy_store_defaults_to_requiring_approval(self):
        """Empty overrides means every action falls through to the gate's
        defaults, all of which need a human."""
        gate = self.gate()
        for action in DEFAULT_TPCE_POLICIES:
            self.assertTrue(gate.policy_for(action)["require_human_approval"], action)

    def test_an_override_has_to_be_written_deliberately(self):
        """The server never writes its own policy file."""
        with open(self.policy.path, "w") as f:
            json.dump({"subjects": {"transportpce": {"tpce_service_create": {
                "require_human_approval": False, "max_changes_per_hour": 9}}}}, f)
        from transportpce_mcp import stores
        reloaded = stores.LocalPolicyStore(self.policy.path)
        gate = WriteGate(policy_store=reloaded, rate_limiter=self.rate,
                         queue=self.queue, audit=self.audit)
        got = gate.policy_for("tpce_service_create")
        self.assertFalse(got["require_human_approval"])
        self.assertEqual(got["source"], "policy store")

    def test_the_rate_limiter_counts_per_action(self):
        self.assertTrue(self.rate.check("transportpce", "a", 1)["allowed"])
        self.rate.record("transportpce", "a")
        self.assertFalse(self.rate.check("transportpce", "a", 1)["allowed"])
        self.assertTrue(self.rate.check("transportpce", "b", 1)["allowed"])

    def test_the_audit_log_is_append_only_jsonl(self):
        self.audit.write({"event": "one"})
        self.audit.write({"event": "two"})
        got = self.audit.tail()
        self.assertEqual([e["event"] for e in got], ["one", "two"])
        self.assertTrue(all("at" in e for e in got))

    def test_a_corrupt_queue_does_not_read_as_nothing_pending(self):
        """Starting empty is survivable; reporting "nothing queued" for a file
        nobody can read is not, so it is logged as an error."""
        from transportpce_mcp import stores
        with open(self.queue.path, "w") as f:
            f.write("{not json")
        with self.assertLogs("transportpce.stores", level="ERROR") as logged:
            stores.LocalApprovalQueue(self.queue.path)
        self.assertIn("do not approve", "".join(logged.output))

    def test_the_reply_says_which_queue_took_it(self):
        gate = self.gate()
        got = gate.request("tpce_service_create", "x", "rpc", {}, target="svc")
        self.assertEqual(got["queue_backed_by"], "injected")
