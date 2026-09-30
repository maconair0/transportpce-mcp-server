"""
Run the TransportPCE MCP server.

SSE by default, since that is what most clients mount; `--transport stdio` for
those that prefer it. Where TransportPCE itself lives is entirely configuration
— see README.md.

    python run_transportpce_mcp.py
    python run_transportpce_mcp.py \\
        --tpce-url http://10.0.0.9:8181 --username admin --password secret

The operator half of the write gate lives here too, deliberately away from the
MCP surface: `--list-pending`, `--approve`, `--reject`, `--apply-now`, and
`--drain-interval` to issue approved writes on a timer. Nothing is issued unless
one of those is used, and no MCP tool can trigger it — see `gate.py`.
"""
import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int,
                        default=int(os.getenv("TPCE_MCP_PORT", "3004")),
                        help="SSE port (default 3004)")
    parser.add_argument("--host", default=os.getenv("TPCE_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--transport", choices=["sse", "stdio", "streamable-http"],
                        default="sse")
    parser.add_argument("--tpce-url", default="",
                        help="TransportPCE base URL, e.g. http://127.0.0.1:8181 "
                             "(default: $TPCE_BASE_URL, else TPCE_HOST/TPCE_PORT)")
    parser.add_argument("--restconf-root", default="",
                        help="/rests (RFC 8040, Chlorine onward) or /restconf (older)")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--drain-interval", type=int, default=0,
                        help="seconds between issuing operator-approved writes; "
                             "0 (default) never issues any")
    parser.add_argument("--check", action="store_true",
                        help="report whether TransportPCE is reachable, then exit")
    # Operator actions. Deliberately here and not MCP tools: approving is the
    # human half of the gate, and a tool that could approve would make the queue
    # a delay rather than a gate.
    parser.add_argument("--list-pending", action="store_true",
                        help="show queued writes awaiting a decision, then exit")
    parser.add_argument("--approve", metavar="ID", default="",
                        help="approve a queued write (it is issued on the next "
                             "drain, or by --apply-now)")
    parser.add_argument("--reject", metavar="ID", default="",
                        help="reject a queued write")
    parser.add_argument("--apply-now", action="store_true",
                        help="issue every approved write once, then exit")
    args = parser.parse_args(argv)

    # CLI beats environment; environment beats defaults.
    for flag, var in ((args.tpce_url, "TPCE_BASE_URL"),
                      (args.restconf_root, "TPCE_RESTCONF_ROOT"),
                      (args.username, "TPCE_USERNAME"),
                      (args.password, "TPCE_PASSWORD")):
        if flag:
            os.environ[var] = flag

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from transportpce_mcp.config import TransportPceConfig
    from transportpce_mcp.gate import WriteGate
    from transportpce_mcp.restconf import RestconfClient
    from transportpce_mcp.server import READ_TOOLS, WRITE_TOOLS, create_server

    try:
        config = TransportPceConfig.from_env()
    except ValueError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    client = RestconfClient(config)

    if args.check:
        got = asyncio.run(client.health())
        print(f"TransportPCE at {config.base_url}{config.restconf_root}: "
              f"{'reachable' if got.get('reachable') else 'NOT reachable'}")
        if got.get("reachable") and not got.get("transportpce_installed"):
            print("  answered, but no transportpce model path — is "
                  "odl-transportpce installed? (feature:install odl-transportpce)")
        if got.get("detail"):
            print(f"  {got['detail']}")
        return 0 if got.get("reachable") else 1

    gate = WriteGate()

    if args.list_pending or args.approve or args.reject or args.apply_now:
        return _operate(args, client, gate)

    server = create_server(config=config, client=client, gate=gate)
    server.settings.host = args.host
    server.settings.port = args.port

    print(f"TransportPCE MCP server: {config.base_url}{config.restconf_root} "
          f"(user={config.username or 'none'})", flush=True)
    print(f"  {len(READ_TOOLS)} read tools, {len(WRITE_TOOLS)} write tools "
          f"(queued for approval, never issued from a tool call)", flush=True)
    if args.transport == "sse":
        print(f"  SSE endpoint: http://{args.host}:{args.port}/sse", flush=True)
        print(f"  mount with:   TPCE_MCP_URL=http://{args.host}:{args.port}/sse",
              flush=True)
    if args.drain_interval:
        _start_drain(client, gate, args.drain_interval)
        print(f"  draining approved writes every {args.drain_interval}s", flush=True)
    else:
        print("  not draining: approved writes stay queued until this is run "
              "with --drain-interval", flush=True)

    server.run(transport=args.transport)
    return 0


def _operate(args, client, gate) -> int:
    """The operator half of the gate: look, decide, apply."""
    gate._load()  # noqa: SLF001 - binding the stores is what we are here for
    queue = gate._queue  # noqa: SLF001

    if args.list_pending:
        pending = [i for i in queue.pending()
                   if (i.get("instruction") or {}).get("controller") == "transportpce"]
        if not pending:
            print("nothing queued for TransportPCE")
            return 0
        print(f"{len(pending)} queued write(s) awaiting a decision "
              f"(queue: {gate.backed_by}):\n")
        for item in pending:
            instruction = item.get("instruction") or {}
            print(f"  {item['id']}")
            print(f"    action : {instruction.get('action')}")
            print(f"    target : {instruction.get('component')}")
            print(f"    reason : {item.get('reason')}")
            print(f"    issues : {instruction.get('rpc') or instruction.get('path')}")
            print(f"    queued : {item.get('queued_at')}\n")
        print("approve with: --approve <id>   reject with: --reject <id>")
        return 0

    for item_id, approve in ((args.approve, True), (args.reject, False)):
        if not item_id:
            continue
        item = queue.get(item_id)
        if item is None:
            print(f"no queued item {item_id}", file=sys.stderr)
            return 1
        if (item.get("instruction") or {}).get("controller") != "transportpce":
            # A host system may share this queue with its own device writes.
            # Deciding one of those from here would be acting outside this
            # server's remit.
            print(f"{item_id} is not a TransportPCE request "
                  f"({(item.get('instruction') or {}).get('controller')}); "
                  f"decide it where it was raised", file=sys.stderr)
            return 1
        decided = queue.decide(item_id, approve, decided_by="operator (cli)")
        print(f"{item_id}: {decided['state']}")
        if approve:
            print("it is issued on the next drain, or now with --apply-now")

    if args.apply_now:
        got = asyncio.run(gate.apply_approved(client))
        print(f"applied {len(got['applied'])}, failed {len(got['failed'])}")
        for failure in got["failed"]:
            print(f"  {failure['id']}: {failure['error']}", file=sys.stderr)
        return 1 if got["failed"] else 0
    return 0


def _start_drain(client, gate, interval: int) -> None:
    """Issue approved writes on a timer, in a thread of its own.

    Separate from the MCP event loop on purpose: a slow renderer must not stall
    tool calls, and a tool call must never be the thing that triggers a write.
    """
    import threading

    log = logging.getLogger("transportpce.drain")

    def loop():
        import time
        while True:
            time.sleep(interval)
            try:
                got = asyncio.run(gate.apply_approved(client))
                if got["applied"] or got["failed"]:
                    log.info("applied %d approved write(s), %d failed",
                             len(got["applied"]), len(got["failed"]))
            except Exception as e:  # noqa: BLE001 - a drain failure must not end the loop
                log.warning("drain failed: %s: %s", type(e).__name__, e)

    threading.Thread(target=loop, daemon=True, name="tpce-drain").start()


if __name__ == "__main__":
    sys.exit(main())
