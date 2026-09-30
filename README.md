# transportpce-mcp-server

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[OpenDaylight TransportPCE](https://docs.opendaylight.org/projects/transportpce/en/latest/),
the open-source optical SDN controller built on the OpenROADM models.

It lets an AI agent read an optical network — topology, port mappings, device
status, provisioned services — and compute paths through it, while keeping
provisioning behind a human approval gate.

> **Status: not yet run against a real controller.** Every request shape is
> derived from TransportPCE's own YANG models and exercised against mocked
> RESTCONF (92 tests). What that proves is that the bodies match the models and
> the error paths are handled. What it does not prove is that a live TransportPCE
> accepts them. The [smoke test](#smoke-test) is the first real evidence, and
> nobody has run it. Treat the read tools as probably-right and the write tools
> as untested.

## Why the write tools do not write

An agent reading a network is useful. An agent provisioning one is a different
proposition: `service-create` renders cross-connects on live ROADMs and
`service-delete` drops traffic that is currently carried.

So the three write tools do not perform anything. They validate the request,
consult policy, check a rate limit, append to an approval queue, and return a
queue id. A human approves or rejects out of band; only then is the RPC issued.

```
tpce_service_create(...)  ->  { "status": "queued_for_approval",
                                "approval_id": "a4f1...",
                                "what_happens_next": "Nothing was sent ..." }

$ python run_transportpce_mcp.py --list-pending      # a human looks
$ python run_transportpce_mcp.py --approve a4f1...   # a human decides
$ python run_transportpce_mcp.py --apply-now         # now it is issued
```

There is deliberately **no** `force`, `approved`, `confirm` or `dry_run`
parameter on any write tool, and no tool that drains the queue. An argument like
that is one the model fills in itself, which turns a gate into a suggestion —
and a queue an agent can drain is a delay, not a gate. A test enumerates the
write tools' signatures against a list of such names so one cannot be added by
accident later.

This is not a claim that the design is unbypassable. Anyone running the server
can edit the queue file or set a policy override. The point is that the *model*
cannot, through the interface it is given.

## Tools

Ten reads, three writes.

| read tool | answers |
|---|---|
| `tpce_health_check` | is it reachable, and is `odl-transportpce` installed |
| `tpce_get_topology` | `openroadm-topology`, `otn-topology`, `openroadm-network`, `clli-network` |
| `tpce_get_portmapping` | TransportPCE's abstraction of each device's ports |
| `tpce_get_node_status` | whether a mounted NETCONF device is connected |
| `tpce_list_services` | provisioned services and their states |
| `tpce_get_service` | one service in full |
| `tpce_compute_path` | a PCE path computation — **advisory, reserves nothing** |
| `tpce_describe_models` | which fields the YANG makes mandatory, and the legal enums |
| `tpce_get_tapi_topology_details` | TAPI's abstracted topology |
| `tpce_list_tapi_connectivity_services` | TAPI connectivity services |

| write tool | queues |
|---|---|
| `tpce_service_create` | `org-openroadm-service:service-create` |
| `tpce_service_delete` | `org-openroadm-service:service-delete` |
| `tpce_device_connect` | a NETCONF mount (`PUT` to `network-topology`) |

### Path computation is a read

`tpce_compute_path` forces `resource-reserve: false`. The model marks that leaf
mandatory so something must be sent, and `true` tells the PCE to hold the
computed resources until cancelled — a lasting change to the controller's state.
Computing a path is a question; reserving one is an action. Reserving is not
offered at all.

### Responses are JSON, and failures are data

Every tool returns a JSON document. A failure is `{"ok": false, "error": ...}`
with `error` one of `unreachable`, `refused` or `invalid_request` — not an error
string returned as a successful result. That distinction matters more than it
looks: a controller that is *down* must not read to the model as one that
*answered*, and a caller should be able to branch on a field rather than
pattern-match prose.

RESTCONF makes this harder than it sounds, because OpenROADM RPCs report failure
*inside* an HTTP 200:

```json
{"output": {"configuration-response-common": {
    "response-code": "500", "response-message": "no path available"}}}
```

Reading the 200 as success is how a refused `service-create` gets reported as
provisioned. This server checks the body.

### Topology reads are summarised by default

A real `openroadm-topology` payload runs to megabytes — every node with its full
termination-point list and wavelength bitmaps. Returned whole, one call would
consume an agent's entire context. So reads return counts, a breakdown by node
type, and a sample of ids, with `raw=true` as the escape hatch when the full
payload is genuinely wanted.

```json
{"ok": true, "network": "openroadm-topology",
 "data": {"nodes": 27, "links": 48,
          "links_note": "OpenROADM links are unidirectional, so a bidirectional fibre appears twice",
          "node_types": {"ROADM": 4, "SRG": 8, "DEGREE": 12, "XPONDER": 3},
          "termination_points": 214,
          "node_ids": ["ROADM-A-SRG1", "..."],
          "not_in_service": ["XPDR-C1-XPDR1"]}}
```

That `links_note` is there because the count invites a wrong conclusion:
OpenROADM models a link as one direction, so 48 links may be 24 fibres.

## Install

```bash
git clone https://github.com/maconair0/transportpce-mcp-server.git
cd transportpce-mcp-server
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+. Two dependencies: `mcp` and `httpx`.

## Configuration

Nothing about where TransportPCE lives is hardcoded. The defaults describe a
stock OpenDaylight Karaf because that is what an untouched install answers on —
not because this expects to find one there.

| variable | default | notes |
|---|---|---|
| `TPCE_BASE_URL` | — | e.g. `http://127.0.0.1:8181`; overrides host/port/scheme |
| `TPCE_HOST` / `TPCE_PORT` / `TPCE_SCHEME` | `127.0.0.1` / `8181` / `http` | used when `TPCE_BASE_URL` is unset |
| `TPCE_USERNAME` / `TPCE_PASSWORD` | `admin` / `admin` | ODL's stock basic auth — **change these**. An empty username sends no auth at all, for an instance behind a gateway that authenticates for you |
| `TPCE_RESTCONF_VERSION` | `rfc8040` | `rfc8040` → `/rests`, `draft02` → `/restconf` |
| `TPCE_RESTCONF_ROOT` | — | set the root path directly, overriding the above |
| `TPCE_TIMEOUT` / `TPCE_LONG_TIMEOUT` | `60` / `180` | reads vs path computation and rendering |
| `TPCE_VERIFY_TLS` | `true` | set `false` only for a self-signed Karaf certificate |
| `TPCE_MCP_HOST` / `TPCE_MCP_PORT` | `127.0.0.1` / `3004` | this server's own endpoint |
| `TPCE_STATE_DIR` | `transportpce_mcp/state/` | approval queue, audit log, policy overrides |

`USE_ODL_ALT_RESTCONF_PORT` and `USE_ODL_RESTCONF_VERSION` are honoured too,
since those are the names TransportPCE's own test harness uses.

**Why the RESTCONF root is configurable rather than assumed.** OpenDaylight
served RESTCONF at `/restconf` before the RFC 8040 rewrite and at `/rests`
afterwards. TransportPCE's own tests still switch between the two, and the
OpenDaylight documentation pages disagree depending on which release they were
written for. A server that hardcodes either is wrong against half the releases in
the field.

## Running

```bash
# is the controller there at all?
python run_transportpce_mcp.py --check

# serve over SSE (the default)
python run_transportpce_mcp.py --tpce-url http://127.0.0.1:8181

# or over stdio, for a client that prefers it
python run_transportpce_mcp.py --transport stdio
```

`--check` separates three states that otherwise all look like failure:
unreachable; reachable but `odl-transportpce` not installed (Karaf answers TCP
and then 404s every model path); and working.

### As an MCP client sees it

For a stdio client such as Claude Desktop:

```json
{
  "mcpServers": {
    "transportpce": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/run_transportpce_mcp.py", "--transport", "stdio"],
      "env": {
        "TPCE_BASE_URL": "http://127.0.0.1:8181",
        "TPCE_USERNAME": "admin",
        "TPCE_PASSWORD": "admin"
      }
    }
  }
}
```

Over SSE, point the client at `http://127.0.0.1:3004/sse`.

### Tool names are built for a read-only client

A client that mounts a third-party MCP server cannot trust a tool's own
description to say whether it mutates — a description arrives from another
process. In practice such clients match the *verb* in the name. These names are
built to survive that:

- every read tool leads with a read verb (`get`, `list`, `describe`, `compute`,
  `health`), in the first or second segment, since a client typically strips only
  one prefix;
- no read tool's name contains a mutating word anywhere — which is why it is
  `tpce_describe_models` and not `tpce_get_request_shapes` ("request" reads as a
  verb), and `tpce_get_tapi_topology_details` rather than
  `tpce_tapi_get_topology_details` (the verb would be in the third segment);
- every write tool's name contains an obvious mutating word, so a read-only
  client refuses it even before this server's own gate applies.

Tests assert all three properties.

## Installing TransportPCE to test against

**There is no Docker image or Compose file for TransportPCE itself.** The
Dockerfiles in its repository (`tests/Xtesting/DockerSims/`, `tests/inventory/`)
build *device simulators* — honeynode ROADM and transponder sims — and the
Xtesting harness. They do not package the controller. It is built from source.

### Karaf distribution

```bash
git clone https://gerrit.opendaylight.org/gerrit/transportpce
cd transportpce
mvn clean install -DskipTests          # JDK 17+, Maven 3.8+
./karaf/target/assembly/bin/karaf
```

At the `opendaylight-user@root>` prompt:

```
feature:install odl-transportpce
```

Add `odl-transportpce-tapi` for the two TAPI tools; without it they return 404,
which this server reports as `refused` rather than as the controller being
absent.

RESTCONF then answers on **8181** with **admin/admin** — taken from the project's
own `tests/transportpce_tests/common/test_utils.py`, which sets
`ODL_LOGIN = 'admin'`, `ODL_PWD = 'admin'`, `RESTCONF_PORT = 8181`.

### lighty.io

TransportPCE also runs under [lighty.io](https://lighty.io), a plain JVM
application rather than a Karaf container — faster to start and lighter to carry.
Its banner (`lighty.io and RESTCONF-NETCONF started`) is what the project's own
harness waits on. Same RESTCONF surface, so nothing here changes; only
`TPCE_BASE_URL` may differ.

### Device simulators

With no real ROADMs the topology stays empty. `tests/Xtesting/DockerSims/build_sims.sh`
in the TransportPCE repository builds honeynode simulators it can mount over
NETCONF — the intended way to get a non-trivial topology on a laptop.

## Smoke test

Once TransportPCE is running. In order; each step tells you something the next
one assumes.

**1. Is it there, and is the feature installed?**

```bash
python run_transportpce_mcp.py --check
```

**2. Read the topology.**

```bash
python - <<'PY'
import asyncio, json
from transportpce_mcp.restconf import RestconfClient
from transportpce_mcp.summarise import summarise_topology
got = asyncio.run(RestconfClient().get(
    "ietf-network:networks/network=openroadm-topology"))
print(json.dumps(summarise_topology(got, "openroadm-topology"), indent=2))
PY
```

Expect node and link counts. **Zero nodes is a correct answer** on a controller
with nothing mounted — it means the path worked, not that the test failed.

**3. Compute a path between two nodes step 2 actually reported.**

```bash
python - <<'PY'
import asyncio, json
from transportpce_mcp.restconf import RestconfClient
from transportpce_mcp.schemas import path_computation_request
from transportpce_mcp.summarise import summarise_path_computation
body = path_computation_request(
    service_name="smoke-test-1",
    service_a_end={"service-format": "Ethernet", "service-rate": 100,
                   "clli": "NodeA", "node-id": "XPDR-A1-XPDR1"},
    service_z_end={"service-format": "Ethernet", "service-rate": 100,
                   "clli": "NodeC", "node-id": "XPDR-C1-XPDR1"})
got = asyncio.run(RestconfClient().rpc(
    "transportpce-pce:path-computation-request", body))
print(json.dumps(summarise_path_computation(got), indent=2))
PY
```

Substitute node ids and `clli` values from step 2 — the ones above are from
TransportPCE's own functional tests and exist only with the same simulators
loaded. Expect `response-code: 200` with `Path is calculated`, or a clean refusal
naming what the PCE could not satisfy. A `TransportPceError` means the RPC ran
and said no, which still proves the connection; `TransportPceUnreachable` means
it never got there.

**4. Confirm a write is queued and not executed.**

Call `tpce_service_create` through your MCP client. The reply should say
`queued_for_approval` with an `approval_id`. Then check `tpce_list_services` —
the service must not exist. That is the whole point of step 4.

```bash
python run_transportpce_mcp.py --list-pending
```

## Request shapes

Validation is against the YANG, not against prose, because RESTCONF fails
unhelpfully: a missing mandatory leaf comes back as a schema-node error naming an
internal path, which tells you nothing about which argument you left out.

For `path-computation-request`, mandatory: `service-name`, `resource-reserve`,
`service-handler-header/request-id`, and on each endpoint `service-format` and
`clli`.

**Two places the models and the documentation disagree**, where this follows the
models:

- **`node-id` is not mandatory for path computation.** The documentation lists it
  among an endpoint's required fields; `service-endpoint-sp` does not mark it
  `mandatory true`. It *is* required to provision, because the renderer has to
  configure a real port — so `service-create` demands it and `compute_path` does
  not.
- **`service-rate` is conditionally mandatory**, under
  `when "../service-format != 'OMS'"`. An OMS (ROADM-line) service legitimately
  has no rate, so requiring it always would reject a valid request.

`tpce_describe_models` reports both at runtime, so an agent can ask rather than
guess.

Legal `service-format` values, from OpenROADM's `org-openroadm-service-format.yang`:
`Ethernet`, `OTU`, `OC`, `STM`, `OMS`, `ODU`, `OTM`, `other`, `flexo`.
`pce-routing-metric`: `hop-count`, `propagation-delay`, `TE-metric`, `IGP-metric`.

## Tests

```bash
python -m unittest discover -s tests -t .
```

92 tests, no network and no controller required. They cover the request bodies
against the models' own mandatory-leaf rules, the RESTCONF error shapes
(`ietf-restconf:errors`, and an RPC failing inside an HTTP 200), the summarisers
against every wrapping ODL uses, and the gate — including several tests whose
only job is to assert that a write tool left the HTTP call list empty.

## Design notes

**The gate uses whatever stores it finds.** This server was written inside a
larger system whose SDN-controller tier already owns policy, approvals, rate
limits and an audit log, and it uses those when they are importable. Standalone,
it falls back to its own JSON-backed equivalents in `TPCE_STATE_DIR`. The
fallback is the same semantics, not a lighter version — still queued, still rate
limited, still audited, still a human decision. A fallback that auto-approved
would remove the property the write half is built on.

**Nothing is classified low-risk.** All three write actions default to
`require_human_approval: true`. `service-delete` is capped lower than
`service-create` (2/hour against 4) because deleting drops traffic that is
currently carried and is less recoverable. An action nobody has classified is
hard-gated and not overridable, rather than defaulting to permitted.

**Approval is not idempotent-by-accident.** An item can be decided once;
re-approving an applied item does not provision it again. The queue persists, so
a restart does not lose a pending decision — an approval queue that evaporated
would push operators towards approving in bulk.

## Limitations

- Never run against a real TransportPCE. See the status note at the top.
- No notification support. `tapi-notification` and the Kafka/DMaaP connectors
  need external infrastructure to be useful.
- `service-create` is exposed with the common fields, not the full OpenROADM
  service model — no explicit `soft-constraints`, latency bounds or SRLG
  exclusions on the create path, though path computation accepts hard and soft
  constraints.
- Read tools summarise with a fixed sample size (12). Large topologies report
  accurate counts with a truncated sample; `raw=true` gets everything.

## Contributing

Issues and pull requests welcome, particularly from anyone with a running
TransportPCE who can confirm or correct the request shapes. If a body here is
rejected by a real controller, that is the most useful bug report this project
can receive — please include the `ietf-restconf:errors` document.

## Licence

Apache-2.0. See [LICENSE](LICENSE).

TransportPCE and OpenDaylight are projects of the OpenDaylight Foundation;
OpenROADM models are published by the OpenROADM MSA. This is an independent
client and is not affiliated with either.

## Sources

Confirmed against, rather than recalled:

- `transportpce-pce@2024-02-05.yang` — the `path-computation-request` RPC
- `transportpce-common-service-path-types@2022-01-18.yang` — `service-endpoint-sp`,
  `service-handler-header`, `pce-metric`
- `org-openroadm-service-format.yang` (OpenROADM MSA) — the `service-format` enum
- `tests/transportpce_tests/common/test_utils.py` — port, credentials, RESTCONF
  roots, Karaf path
- [TransportPCE developer guide](https://docs.opendaylight.org/projects/transportpce/en/latest/developer-guide.html)
  · [user guide](https://docs.opendaylight.org/projects/transportpce/en/latest/user-guide.html)
