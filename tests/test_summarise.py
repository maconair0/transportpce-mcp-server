"""
Topology detail in the summary, for a caller that has to render it.

`summarise_topology` answers "how big is this network" very well and answered
"what is in it" not at all: counts, a histogram and a sample of node ids. The
agent on the other end was printing every node and link for the other
controllers it federates and a single count line for this one — not a choice it
made, just all it was sent.

So the summary now carries a bounded per-node and per-link detail list. Bounded
is the point: an unbounded one is the thing this module exists to prevent, and
`raw=True` is still there for the full payload.
"""
import unittest

from transportpce_mcp import schemas, summarise




class TopologyDetailTests(unittest.TestCase):
    """A topology summary that is only counts cannot be rendered as a topology.

    The agent calling this was printing "14 nodes, 32 links" where it printed
    every node and link for the other controllers it talks to — not because it
    chose to, but because this is all it was sent.
    """

    def payload(self):
        return {"ietf-network:networks": {"network": [{
            "network-id": "openroadm-topology",
            "node": [
                {"node-id": "ROADM-A1-DEG1",
                 "org-openroadm-common-network:node-type": "DEGREE",
                 "org-openroadm-common-network:operational-state": "inService",
                 "ietf-network-topology:termination-point": [{}, {}],
                 "supporting-node": [{"node-ref": "ROADM-A1"}]},
                {"node-id": "XPDR-A1-XPDR1",
                 "org-openroadm-common-network:node-type": "XPONDER",
                 "org-openroadm-common-network:administrative-state": "outOfService"},
            ],
            "ietf-network-topology:link": [
                {"link-id": "L1",
                 "source": {"source-node": "ROADM-A1-DEG1",
                            "source-tp": "DEG1-TTP-TXRX"},
                 "destination": {"dest-node": "ROADM-C1-DEG2",
                                 "dest-tp": "DEG2-TTP-TXRX"},
                 "org-openroadm-common-network:link-type": "ROADM-TO-ROADM",
                 "org-openroadm-common-network:operational-state": "inService"},
            ]}]}}

    def test_each_node_carries_its_type_and_states(self):
        out = summarise.summarise_topology(self.payload(), "openroadm-topology")
        by_id = {n["id"]: n for n in out["node_detail"]}
        self.assertEqual("DEGREE", by_id["ROADM-A1-DEG1"]["type"])
        self.assertEqual("inService", by_id["ROADM-A1-DEG1"]["operational_state"])
        self.assertEqual("outOfService", by_id["XPDR-A1-XPDR1"]["admin_state"])
        self.assertEqual(2, by_id["ROADM-A1-DEG1"]["termination_points"])
        self.assertEqual(["ROADM-A1"], by_id["ROADM-A1-DEG1"]["supporting"])

    def test_a_link_names_both_ends_and_both_termination_points(self):
        """The tp is kept because two fibres can join the same pair of node ids.

        On a ROADM an inter-degree link and an add/drop link do exactly that, and
        a caller folding directions on the node ids alone would render them as
        one.
        """
        out = summarise.summarise_topology(self.payload(), "openroadm-topology")
        link = out["link_detail"][0]
        self.assertEqual("ROADM-A1-DEG1", link["a"])
        self.assertEqual("DEG1-TTP-TXRX", link["a_tp"])
        self.assertEqual("ROADM-C1-DEG2", link["z"])
        self.assertEqual("ROADM-TO-ROADM", link["type"])

    def test_an_augmented_state_is_found_whatever_the_release_prefixed_it_with(self):
        """The prefix has moved between OpenROADM releases; the suffix has not."""
        out = summarise.summarise_topology({"network": [{
            "network-id": "t",
            "node": [{"node-id": "N1", "node-type": "XPONDER",
                      "operational-state": "outOfService"}]}]}, "t")
        self.assertEqual("outOfService", out["node_detail"][0]["operational_state"])

    def test_the_detail_is_bounded(self):
        """The reason this module exists is not undone by adding detail to it."""
        nodes = [{"node-id": f"N{i}", "node-type": "XPONDER"} for i in range(200)]
        out = summarise.summarise_topology(
            {"network": [{"network-id": "t", "node": nodes}]}, "t")
        self.assertEqual(summarise.DETAIL, len(out["node_detail"]))
        self.assertEqual(200 - summarise.DETAIL, out["node_detail_truncated"])

    def test_an_empty_topology_still_reports_no_detail_rather_than_failing(self):
        out = summarise.summarise_topology({}, "openroadm-topology")
        self.assertEqual(0, out["nodes"])
        self.assertNotIn("node_detail", out)


class FourRoadmNetworkTests(unittest.TestCase):
    """Three bugs a four-ROADM network exposed, each a silently wrong answer.

    All three had the same shape: RESTCONF returns a module-qualified key
    (`transportpce-portmapping:nodes`, `transportpce-pce:output`) and the
    summariser looked for the bare name, so it found nothing and said so in a
    reply whose `ok` was still true. "Nothing" and "I could not read it" are
    different answers, and the second rendered as the first is how a refused path
    computation reads as an empty one.
    """

    def test_a_single_node_portmapping_reply_is_understood(self):
        # Asking for every node returns `…:network/nodes`; asking for one returns
        # `…:nodes` directly. Only the first was unwrapped, so a per-node query
        # reported "nodes: 0" for a node the same tool listed when asked for all.
        got = summarise.summarise_portmapping({
            "transportpce-portmapping:nodes": [
                {"node-id": "ROADM-D1", "mapping": [{}, {}],
                 "node-info": {"node-type": "rdm"}}]})
        self.assertEqual(got["nodes"], 1)
        self.assertEqual(got["detail"][0]["node-id"], "ROADM-D1")

    def test_the_collection_shape_still_works(self):
        got = summarise.summarise_portmapping({
            "transportpce-portmapping:network": {"nodes": [
                {"node-id": "ROADM-A1", "mapping": []},
                {"node-id": "ROADM-B1", "mapping": []}]}})
        self.assertEqual(got["nodes"], 2)

    def test_a_module_qualified_rpc_output_is_read(self):
        got = summarise.summarise_path_computation({
            "transportpce-pce:output": {"configuration-response-common": {
                "response-code": "200", "response-message": "Path is calculated by PCE",
                "request-id": "r1"}}})
        self.assertEqual(got["response-code"], "200")
        self.assertTrue(got["path_found"])

    def test_a_pce_refusal_inside_a_200_is_not_called_success(self):
        # TransportPCE answers HTTP 200 carrying `response-code 500, "No path
        # found by PCE."`. Reported as a success, an unroutable circuit looked
        # routable.
        got = summarise.summarise_path_computation({
            "transportpce-pce:output": {"configuration-response-common": {
                "response-code": "500", "response-message": "No path found by PCE."}}})
        self.assertFalse(got["path_found"])
        self.assertIn("No path found", got["refused"])

    def test_a_reply_with_no_output_says_that_rather_than_nothing(self):
        got = summarise.summarise_path_computation({"something-else": {}})
        self.assertIn("no RPC output", got["note"])


class PceMetricTests(unittest.TestCase):
    """TransportPCE dereferences the routing metric without a null check."""

    def test_a_metric_is_always_sent(self):
        # `PceGraph.chooseWeight` calls `getPceMetric().ordinal()`, so omitting
        # the field does not get a default — it raises NullPointerException and
        # the RPC comes back as `HTTP 500 path-computation-request failed`, which
        # reads as "this network cannot be routed".
        body = schemas.path_computation_request(
            service_name="x",
            service_a_end={"service-format": "Ethernet", "service-rate": 100,
                           "clli": "NodeA"},
            service_z_end={"service-format": "Ethernet", "service-rate": 100,
                           "clli": "NodeC"})
        self.assertEqual(body["input"]["pce-routing-metric"],
                         schemas.DEFAULT_PCE_METRIC)

    def test_an_explicit_metric_is_respected(self):
        body = schemas.path_computation_request(
            service_name="x",
            service_a_end={"service-format": "Ethernet", "service-rate": 100,
                           "clli": "NodeA"},
            service_z_end={"service-format": "Ethernet", "service-rate": 100,
                           "clli": "NodeC"},
            pce_routing_metric="propagation-delay")
        self.assertEqual(body["input"]["pce-routing-metric"], "propagation-delay")
