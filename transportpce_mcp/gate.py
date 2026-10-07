"""
Writes do not happen because an MCP call asked for one.

A read tool answers. A write tool *requests* — it validates the payload, asks
the policy store whether this action needs a human, checks the rate limit,
appends to the approval queue, and returns the queue id. The RESTCONF POST
happens later, in `apply_approved`, driven by an operator decision recorded
through the SDN controller's existing approval endpoints.

The deliberate absence here is a parameter that skips this. No `force`, no
`approved=True`, no `dry_run=False`. Any such argument is one the model fills in
itself, which makes the gate a suggestion. The model does not get a vote on
whether a change is safe: `service-create` renders cross-connects on real ROADMs
and `service-delete` tears down live traffic. That the southbound hop belongs to
somebody else's controller does not make it a lighter action.

The stores come from whichever tier owns them. Vendored into a system that
already has a policy store, rate limiter, approval queue and audit log, this
binds to those; standalone it falls back to `stores.py`, with the same semantics
rather than a lighter version. An unknown action is hard-gated and not
overridable rather than defaulting to permitted.
"""
import logging
import os
import sys
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("transportpce.gate")

# A host system's own stores, when this package is vendored into one, are looked
# for beside it. Imported lazily so a standalone checkout starts without them.
_HOST_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Risk tiers for the three southbound actions.
# Nothing here is low: each one changes a live optical path.
DEFAULT_TPCE_POLICIES: Dict[str, Dict[str, Any]] = {
    "tpce_service_create": {
        "risk_tier": "high", "require_human_approval": True,
        "max_changes_per_hour": 4,
    },
    "tpce_service_delete": {
        # Deleting a service drops traffic that is currently carried. Rarer than
        # creating one, and less recoverable.
        "risk_tier": "high", "require_human_approval": True,
        "max_changes_per_hour": 2,
    },
    "tpce_device_connect": {
        # Mounting a device is not traffic-affecting, but it puts a box under a
        # controller's authority, and two controllers owning one device is the
        # failure `dispatcher.managed_elsewhere` exists to prevent.
        "risk_tier": "high", "require_human_approval": True,
        "max_changes_per_hour": 6,
    },
}

KIND_PENDING = "pending_approval"


class GateUnavailable(Exception):
    """No approval queue could be reached, so nothing is queued.

    Not raised for a missing host controller tier — `_load` falls back
    to this package's own stores. It remains the signal for a queue that exists
    but cannot be written to, where refusing is the only safe answer.
    """


class WriteGate:
    """Policy + rate limit + approval queue + audit, for southbound requests."""

    def __init__(self, policy_store: Any = None, rate_limiter: Any = None,
                 queue: Any = None, audit: Any = None, subject: str = "transportpce"):
        # `subject` is what a policy store keys its rules by. TransportPCE is
        # the thing being changed as far as this server is concerned; the
        # individual ROADMs sit behind it and are never addressed from here.
        self.subject = subject
        self._policy = policy_store
        self._rate = rate_limiter
        self._queue = queue
        self._audit = audit
        self._loaded = not any(x is None for x in
                               (policy_store, rate_limiter, queue, audit))
        self.backed_by = "injected" if self._loaded else ""

    # ----- wiring -----

    def _load(self) -> None:
        """Bind to the controller tier's stores, once, on first write request."""
        if self._loaded:
            return
        if _HOST_ROOT not in sys.path:
            sys.path.insert(0, _HOST_ROOT)
        try:
            from sdn_controller.approval_queue import ApprovalQueue
            from sdn_controller.dispatcher import AuditLog
            from sdn_controller.policy import PolicyStore, RateLimiter
            self.backed_by = "host sdn_controller"
        except Exception as e:  # noqa: BLE001
            # Standalone. The local stores are the same semantics, not a lighter
            # version: still queued, still rate limited, still audited, still a
            # human decision. A fallback that auto-approved would remove the one
            # property this server's write half is built on.
            logger.info("no host controller tier is importable (%s); "
                        "using this package's own approval queue, rate limiter "
                        "and audit log", type(e).__name__)
            from .stores import (LocalApprovalQueue as ApprovalQueue,
                                 LocalAuditLog as AuditLog,
                                 LocalPolicyStore as PolicyStore,
                                 LocalRateLimiter as RateLimiter)
            self.backed_by = "transportpce_mcp.stores"
        self._policy = self._policy or PolicyStore()
        self._rate = self._rate or RateLimiter()
        self._queue = self._queue or ApprovalQueue()
        self._audit = self._audit or AuditLog()
        self._loaded = True

    def policy_for(self, action: str) -> Dict[str, Any]:
        """This action's rules: the store's if it has an opinion, else ours.

        An action nobody has classified is hard-gated and not overridable.
        Refusing to guess is the point: a new action gets classified
        deliberately, not by inheriting whatever the nearest rule happens to
        say.
        """
        self._load()
        configured = getattr(self._policy.get(self.subject), "actions", {}) or {}
        if action in configured:
            got = configured[action]
            return {"risk_tier": got.risk_tier,
                    "require_human_approval": got.require_human_approval,
                    "max_changes_per_hour": got.max_changes_per_hour,
                    "overridable": got.overridable, "source": "policy store"}
        if action in DEFAULT_TPCE_POLICIES:
            return {**DEFAULT_TPCE_POLICIES[action], "overridable": True,
                    "source": "transportpce defaults"}
        return {"risk_tier": "unknown", "require_human_approval": True,
                "max_changes_per_hour": 0, "overridable": False,
                "source": "unknown action — hard gated"}

    # ----- the only way a write is ever requested -----

    def request(self, action: str, summary: str, rpc: str, body: Dict[str, Any],
                target: str = "", method: str = "", path: str = "") -> Dict[str, Any]:
        """Queue a southbound request. Never performs it.

        Returns what the caller should tell the operator: that it is queued,
        under which id, and what has to happen next.
        """
        self._load()
        policy = self.policy_for(action)

        limit = int(policy.get("max_changes_per_hour", 0))
        allowance = self._rate.check(self.subject, action, limit)
        if not allowance["allowed"]:
            self._audit.write({
                "event": "tpce_write_rate_limited", "action": action,
                "target": target, "used": allowance["used"], "limit": limit,
            })
            return {
                "status": "refused",
                "reason": (f"rate limit reached for {action}: "
                           f"{allowance['used']}/{limit} in the last hour"),
                "policy": policy,
            }

        item = self._queue.add(
            kind=KIND_PENDING,
            device_id=self.subject,
            reason=summary,
            instruction={
                "action": action,
                "component": target,
                "controller": "transportpce",
                "rpc": rpc,
                "body": body,
                "method": method,
                "path": path,
            },
        )
        self._audit.write({
            "event": "tpce_write_queued", "action": action, "target": target,
            "approval_id": item["id"], "rpc": rpc,
            "risk_tier": policy.get("risk_tier"),
        })
        logger.info("queued %s for approval (%s): %s", action, item["id"], summary)
        return {
            "status": "queued_for_approval",
            "approval_id": item["id"],
            "action": action,
            "target": target,
            "policy": policy,
            "queue_backed_by": self.backed_by,
            "what_happens_next": (
                "Nothing was sent to TransportPCE. An operator approves or "
                "rejects this through the SDN controller's approval queue; only "
                "then is the RPC issued. This tool cannot approve its own "
                "request and takes no argument that would let it."),
        }

    # ----- and the only way one is ever performed -----

    def approved_items(self) -> list:
        """Queued TransportPCE writes an operator has approved."""
        self._load()
        out = []
        for item in self._queue._items.values():  # noqa: SLF001 - no public "all"
            instruction = item.get("instruction") or {}
            if (item.get("state") == "approved" and
                    instruction.get("controller") == "transportpce" and
                    not item.get("applied_at")):
                out.append(item)
        return out

    async def apply_approved(self, client: Any,
                             on_result: Optional[Callable] = None,
                             only: Optional[str] = None) -> Dict[str, Any]:
        """Issue the RPCs for approved items. Called by the runner, not by a tool.

        Deliberately not exposed over MCP. If a model could call this, the queue
        would be a delay rather than a gate.
        """
        self._load()
        applied, failed = [], []
        for item in self.approved_items():
            if only is not None and item["id"] != only:
                continue
            instruction = item["instruction"]
            try:
                # Two kinds of southbound write reach this queue. A service is
                # an RPC; mounting a device is a PUT to a data path. The queued
                # item says which, so the applier does not have to guess from
                # the action name.
                if instruction.get("method") == "PUT" and instruction.get("path"):
                    result = await client.put(instruction["path"], instruction["body"])
                elif instruction.get("rpc"):
                    result = await client.rpc(instruction["rpc"], instruction["body"])
                else:
                    raise ValueError(
                        "queued item names neither an rpc nor a PUT path; "
                        "refusing to guess how to apply it")
                item["applied_at"] = _now()
                item["result"] = "ok"
                applied.append(item["id"])
                self._rate.record(self.subject, instruction["action"])
                self._audit.write({
                    "event": "tpce_write_applied", "action": instruction["action"],
                    "target": instruction.get("component"),
                    "approval_id": item["id"],
                    "rpc": instruction.get("rpc") or instruction.get("path"),
                })
            except Exception as e:  # noqa: BLE001
                item["applied_at"] = _now()
                item["result"] = f"failed: {type(e).__name__}: {e}"
                failed.append({"id": item["id"], "error": str(e)})
                self._audit.write({
                    "event": "tpce_write_failed", "action": instruction["action"],
                    "target": instruction.get("component"),
                    "approval_id": item["id"], "error": str(e)[:300],
                })
            if on_result is not None:
                on_result(item)
        if applied or failed:
            self._queue._save()  # noqa: SLF001 - persisting the applied marker
        return {"applied": applied, "failed": failed}


    async def approve_and_apply(self, item_id: str, client: Any, by: str) -> Dict[str, Any]:
        """Approve one queued write and issue it now — that item only.

        For a host system that has had a person confirm this exact change; the
        caller has checked the operator's approver token. `by` is recorded.
        """
        self._load()
        item = self._queue.get(item_id)
        if item is None:
            return {"ok": False, "error": f"no queued request {item_id}"}
        if (item.get("instruction") or {}).get("controller") != "transportpce":
            return {"ok": False, "error": f"{item_id} is not a TransportPCE request"}
        if item.get("state") not in ("pending_approval", "pending"):
            return {"ok": False, "error": f"{item_id} is already {item.get('state')}"}
        self._queue.decide(item_id, True, by, "approved by a confirming host")
        result = await self.apply_approved(client, only=item_id)
        ok = item_id in result.get("applied", [])
        return {"ok": ok, "id": item_id, "state": "applied" if ok else "failed",
                "action": (item.get("instruction") or {}).get("action"),
                **({} if ok else {"error": (result.get("failed") or [{}])[0].get("error", "")})}


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
