# TransportPCE MCP server

[![M8ven Score](https://m8ven.ai/badge/mcp/maconair0/transportpce-mcp-server)](https://m8ven.ai/mcp/maconair0/transportpce-mcp-server?s=readme)

An MCP server for [OpenDaylight TransportPCE](https://docs.opendaylight.org/projects/transportpce/en/latest/),
the OpenROADM optical controller.

It lets an AI agent read an optical network — topology, port mappings, device
status, provisioned services — and compute paths through it, while keeping
provisioning behind a human approval gate.

## Why the write tools do not write

The three write tools do not reach the controller. They validate the request,
write it to an approval queue, and return an `approval_id`. A separate operator
step approves it, and a drain loop issues it.

This is not a limitation to be removed later. An agent that can read a network
and an agent that can reconfigure one are different things to put in front of a
production controller, and the gap between them should be a person. The queue is
also the audit trail: every request is recorded with its arguments, whether it was
approved, and what the controller answered.

`--list-pending` shows the queue. `--drain-interval` runs the loop that issues
approved requests.

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

**Path computation is a read.** `tpce_compute_path` forces
`resource-reserve: false`. The leaf is mandatory so something must be sent, and
`true` tells the PCE to hold the computed resources until cancelled — a lasting
change to the controller. Computing a path is a question; reserving one is an
action.

**Replies are JSON, and failures are data.** Every tool returns a JSON object
with `ok`. A controller that refuses comes back as `{"ok": false, ...}` with the
HTTP status and the RESTCONF error, not as an exception — an agent needs to read
the refusal, not catch it.

**Topology reads are summarised.** A full `openroadm-topology` is large enough to
spend an agent's whole context on one call, so reads return counts, a breakdown by
node type, and a bounded per-node and per-link listing. `raw=true` returns the
full payload.

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
stock OpenDaylight Karaf because that is what an untouched install answers on.

| variable | default | notes |
|---|---|---|
| `TPCE_BASE_URL` | — | e.g. `http://127.0.0.1:8181`; overrides host/port/scheme |
| `TPCE_HOST` / `TPCE_PORT` / `TPCE_SCHEME` | `127.0.0.1` / `8181` / `http` | used when `TPCE_BASE_URL` is unset |
| `TPCE_USERNAME` / `TPCE_PASSWORD` | `admin` / `admin` | ODL's stock basic auth — **change these**. An empty username sends no auth at all, for an instance behind a gateway that authenticates for you |
| `TPCE_RESTCONF_VERSION` | `rfc8040` | `rfc8040` → `/rests`, `draft02` → `/restconf` |
| `TPCE_RESTCONF_ROOT` | — | set the root path directly, overriding the above |
| `TPCE_TIMEOUT` / `TPCE_LONG_TIMEOUT` | `60` / `180` | reads vs path computation and rendering |
| `TPCE_VERIFY_TLS` | `true` | set `false` only for a self-signed Karaf certificate |
| `TPCE_DETAIL_ROWS` | `64` | nodes and links described in full per topology read |
| `TPCE_MCP_HOST` / `TPCE_MCP_PORT` | `127.0.0.1` / `3004` | this server's own endpoint |
| `TPCE_STATE_DIR` | `transportpce_mcp/state/` | approval queue, audit log, policy overrides |

`USE_ODL_ALT_RESTCONF_PORT` and `USE_ODL_RESTCONF_VERSION` are honoured too, since
those are the names TransportPCE's own test harness uses.

The RESTCONF root is configurable rather than assumed because OpenDaylight served
it at `/restconf` before the RFC 8040 rewrite and `/rests` after. TransportPCE's
own tests still switch between the two, so a server that hardcodes either is wrong
against half the releases in the field.

## Running

```bash
# is the controller there at all?
python run_transportpce_mcp.py --check

# serve over SSE (the default)
python run_transportpce_mcp.py --tpce-url http://127.0.0.1:8181

# or over stdio
python run_transportpce_mcp.py --transport stdio
```

`--check` separates three states that otherwise all look like failure:
unreachable; reachable but `odl-transportpce` not installed (Karaf answers TCP and
then 404s every model path); and working.

## A network to test against

TransportPCE ships OpenROADM device simulators in its own source tree. Four
interconnected ROADMs with a transponder at each edge is enough to exercise every
read tool and an end-to-end path computation:

```
          ROADM-A1 ──── ROADM-B1
            :17841  \      :17842 \
XPDR-A1 ──────┘      \        │     ROADM-D1 (:17847)
 :17840               ROADM-C1 ────────┘
XPDR-C1 ───────────────  :17843
 :17844
```

Adjacencies: A1–B1, A1–C1, B1–C1, B1–D1, C1–D1. From `transportpce/tests`:

```bash
honeynode/2.2.1/honeynode-simulator/honeycomb-tpce \
  17847 sample_configs/openroadm/2.2.1/oper-ROADMD.xml
```

Then mount each one with `tpce_device_connect` (or a `PUT` to
`network-topology`). TransportPCE builds the port mapping and infers the ROADM
line links from each device's own OTS interfaces — no topology configuration
needed beyond the mounts.

Three things that cost time if you do not know them:

- **Start the simulators one at a time, waiting for each.** `honeycomb-tpce`
  edits shared files under `config/` before starting the JVM, so simultaneous
  launches overwrite each other's device config and all but one die with an SSH
  bind error that never mentions the cause. Wait for
  `Netconf SSH endpoint started successfully` in the log before the next.
- **A device stuck in `connection-status: connecting`** after its simulator is up
  is in OpenDaylight's reconnect backoff. Deleting and re-creating the mount is
  much faster than waiting it out.
- **The PCE ignores a line link with no OMS attributes.** Write span data to each
  ROADM-to-ROADM link before computing a path, or a complete and healthy-looking
  topology returns `"No path found by PCE"` with no indication why:

  ```
  PUT …/ietf-network-topology:link=<id>/org-openroadm-network-topology:OMS-attributes/span
  {"span": {"auto-spanloss": "true", "spanloss-base": 11.4,
            "engineered-spanloss": 12.2,
            "link-concatenation": [{"SRLG-Id": 0, "fiber-type": "smf",
                                    "SRLG-length": 100000, "pmd": 0.5}]}}
  ```

## Request shapes

Requests are validated against the YANG rather than against prose, because
RESTCONF fails unhelpfully: a missing mandatory leaf comes back as a schema-node
error naming an internal path, which says nothing about which argument was left
out.

For `path-computation-request`, mandatory: `service-name`, `resource-reserve`,
`service-handler-header/request-id`, and on each endpoint `service-format` and
`clli`. `service-rate` is required unless the format is `OMS`.

Four places the models and the published examples disagree, where this server
follows the models:

- **`pce-routing-metric` is effectively mandatory.** It is optional in the YANG,
  but `PceGraph.chooseWeight` calls `getPceMetric().ordinal()` with no null check,
  so omitting it crashes the RPC and surfaces as
  `HTTP 500 path-computation-request failed`. This server always sends it,
  defaulting to `hop-count`. The field is `pce-routing-metric`, not `pce-metric`.
- **`tx-direction` and `rx-direction` are containers, not lists** in revision
  `2024-02-05`. Sending an array gets
  `"Found an unexpected array nested under tx-direction"`.
- **There is no `lgx` node under them** in that revision, though published
  examples include one.
- **Mount parameters nest under `netconf-node`.** A flat body is accepted and
  silently connects nothing.

Replies are read by matching keys on their suffix, not their full spelling:
RESTCONF qualifies them by module (`transportpce-pce:output`), the prefixes move
between releases, and a single-object query returns a different top-level key from
a collection query.

A PCE refusal arrives inside an `HTTP 200`, as
`response-code: 500, "No path found by PCE."` — so the summary carries
`path_found`, and quotes the controller when it is false.

## Tests

```bash
python -m unittest discover -s tests -t .
```

109 tests, no network or controller required. They cover the request bodies
against the models' mandatory-leaf rules, the RESTCONF error shapes, the
summarisers against each wrapping OpenDaylight uses, and the gate — including
tests whose only job is to assert a write tool left the HTTP call list empty.

## Limitations

- No notification support. `tapi-notification` and the Kafka/DMaaP connectors
  need external infrastructure to be useful.
- `service-create` exposes the common fields, not the full OpenROADM service
  model — no explicit `soft-constraints`, latency bounds or SRLG exclusions on the
  create path, though path computation accepts hard and soft constraints.
- The write path has been exercised against simulators only. Nothing here has
  configured real optical hardware.

## Contributing

Issues and pull requests welcome, particularly from anyone running this against
real equipment.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
