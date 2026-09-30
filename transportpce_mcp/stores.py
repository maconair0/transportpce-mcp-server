# Copyright 2026 the transportpce-mcp-server authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""
A standalone approval queue, rate limiter and audit log.

This server was written inside a larger system whose SDN controller tier already
owns policy, approvals, rate limits and the audit log, and it uses those when
they are importable. Run on its own, it needs equivalents — otherwise every
write tool would refuse, and the write half of the server would be decoration.

These are deliberately the same shape and the same semantics as the originals,
not a lighter version of them. The gate is the reason this server can be pointed
at a production controller at all; a fallback that approved things automatically,
or skipped the rate limit, would quietly remove the property the design is built
on. Approval is still a human action, taken out of band — see `--list-pending`
and `--approve` on the runner.

JSON-backed so a queued request survives a restart. An approval that evaporated
when the process died would push operators towards approving in bulk.
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("transportpce.stores")

STATE_DIR = os.getenv("TPCE_STATE_DIR",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalApprovalQueue:
    """Queued southbound requests, pending a human decision."""

    def __init__(self, path: str = ""):
        self.path = path or os.path.join(STATE_DIR, "approval_queue.json")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self._items: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                self._items = json.load(f)
            logger.info("loaded %d queued item(s) from %s", len(self._items), self.path)
        except Exception as e:  # noqa: BLE001
            # A corrupt queue must not take the server down, but it must not be
            # silently treated as "nothing pending" either.
            logger.error("could not read %s (%s); starting empty. The old file is "
                         "left in place — do not approve anything until you have "
                         "looked at it.", self.path, e)

    def _save(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._items, f, indent=2, default=str)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            logger.error("could not persist the approval queue: %s", e)

    def add(self, kind: str, device_id: str, reason: str,
            instruction: Optional[Dict[str, Any]] = None, **_: Any) -> Dict[str, Any]:
        item_id = str(uuid.uuid4())
        item = {
            "id": item_id,
            "kind": kind,
            "device_id": device_id,
            "action": (instruction or {}).get("action"),
            "component": (instruction or {}).get("component"),
            "reason": reason,
            "instruction": instruction,
            "state": "pending",
            "queued_at": _now(),
            "decided_at": None,
            "decided_by": None,
        }
        with self._lock:
            self._items[item_id] = item
            self._save()
        logger.info("queued %s (%s): %s", item["action"], item_id, reason[:90])
        return item

    def pending(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = [i for i in self._items.values() if i["state"] == "pending"]
        return [i for i in items if not kind or i["kind"] == kind]

    def get(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self._items.get(item_id)

    def decide(self, item_id: str, approve: bool, decided_by: str = "operator",
               note: str = "") -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                return None
            if item["state"] != "pending":
                # Deciding twice is refused rather than applied twice. An
                # already-applied item re-approved would provision again.
                logger.warning("%s is already %s; leaving it alone",
                               item_id, item["state"])
                return item
            item["state"] = "approved" if approve else "rejected"
            item["decided_at"] = _now()
            item["decided_by"] = decided_by
            item["decision_note"] = note
            self._save()
        logger.info("%s %s by %s", item_id, item["state"], decided_by)
        return item

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for item in self._items.values():
            out[item["state"]] = out.get(item["state"], 0) + 1
        return out


class LocalRateLimiter:
    """Caps southbound writes per subject+action per hour.

    Not persisted, deliberately: the window is an hour, and a restart that
    forgot it would cap less than intended rather than more. Persisting a
    counter that could be stale in the other direction is worse.
    """

    def __init__(self):
        self._events: Dict[tuple, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, device_id: str, action: str, limit: int) -> Dict[str, Any]:
        now = time.time()
        key = (device_id, action)
        with self._lock:
            recent = [t for t in self._events.get(key, []) if now - t < 3600]
            self._events[key] = recent
        return {"allowed": len(recent) < limit, "used": len(recent), "limit": limit}

    def record(self, device_id: str, action: str) -> None:
        with self._lock:
            self._events.setdefault((device_id, action), []).append(time.time())


class LocalAuditLog:
    """Append-only JSONL. Every queue, refusal, application and failure."""

    def __init__(self, path: str = ""):
        self.path = path or os.path.join(STATE_DIR, "audit.jsonl")
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        record = {"at": _now(), **entry}
        try:
            with self._lock:
                with open(self.path, "a") as f:
                    f.write(json.dumps(record, default=str) + "\n")
        except Exception as e:  # noqa: BLE001 - a write must not be lost to a log failure
            logger.error("could not append to the audit log: %s", e)
        return record

    def tail(self, limit: int = 50) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path) as f:
                lines = f.readlines()[-limit:]
            return [json.loads(line) for line in lines if line.strip()]
        except Exception as e:  # noqa: BLE001
            logger.error("could not read the audit log: %s", e)
            return []


class LocalPolicyStore:
    """Per-subject action policy, so overrides are possible without the parent tier.

    Empty by default, which means every action falls through to the gate's own
    defaults — all of which require approval. An override is a deliberate edit to
    the JSON file, never something this server writes for itself.
    """

    def __init__(self, path: str = ""):
        self.path = path or os.path.join(STATE_DIR, "policy.json")
        self._subjects: Dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                raw = json.load(f)
            self._subjects = raw.get("subjects", {})
            logger.info("loaded policy overrides for %d subject(s)",
                        len(self._subjects))
        except Exception as e:  # noqa: BLE001
            logger.error("could not read %s (%s); using defaults, which require "
                         "approval for everything", self.path, e)

    def get(self, subject: str) -> Any:
        """Mimics the parent tier's `PolicyStore.get(...).actions`."""
        actions = {}
        for action, doc in (self._subjects.get(subject) or {}).items():
            if not isinstance(doc, dict):
                continue
            actions[action] = _Policy(
                risk_tier=str(doc.get("risk_tier", "high")),
                require_human_approval=bool(doc.get("require_human_approval", True)),
                max_changes_per_hour=int(doc.get("max_changes_per_hour", 0)),
                overridable=bool(doc.get("overridable", True)),
            )
        return _Subject(actions)


class _Policy:
    __slots__ = ("risk_tier", "require_human_approval", "max_changes_per_hour",
                 "overridable")

    def __init__(self, risk_tier, require_human_approval, max_changes_per_hour,
                 overridable):
        self.risk_tier = risk_tier
        self.require_human_approval = require_human_approval
        self.max_changes_per_hour = max_changes_per_hour
        self.overridable = overridable


class _Subject:
    __slots__ = ("actions",)

    def __init__(self, actions):
        self.actions = actions
