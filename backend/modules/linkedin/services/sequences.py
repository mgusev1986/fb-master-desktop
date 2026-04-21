"""LinkedIn sequences (Agent Mode) — CRUD + утилиты для node-graph'а.

Узлы (node types):
  * "send_invitation"   — connection invitation с note (использует invitation_sender)
  * "wait_hours"        — отложить на N часов
  * "check_invite_accepted" — проверить, принято ли приглашение
  * "send_dm"           — DM 1st-degree (использует dm_sender)
  * "check_reply"       — проверить ответ в conversation
  * "stop"              — завершить выполнение (terminal)

Граф хранится в `nodes_json` (массив {id, type, params}) и `edges_json`
(массив {from, to, on?: "ok"|"fail"|"accepted"|"replied"|"timeout"}).
Старт — узел с `params.entry=True` или первый в списке.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from backend.modules.linkedin.models import LinkedInSequence, LinkedInSequenceRun


VALID_NODE_TYPES = (
    "send_invitation",
    "wait_hours",
    "check_invite_accepted",
    "send_dm",
    "check_reply",
    "stop",
)
VALID_EDGE_LABELS = ("ok", "fail", "accepted", "not_yet", "replied", "no_reply", "timeout")


def list_sequences(db: Session, organization_id: int) -> list[LinkedInSequence]:
    return (
        db.query(LinkedInSequence)
        .filter(
            LinkedInSequence.organization_id == organization_id,
            LinkedInSequence.is_archived.is_(False),
        )
        .order_by(LinkedInSequence.created_at.desc())
        .all()
    )


def get_sequence(db: Session, seq_id: int) -> LinkedInSequence | None:
    return db.get(LinkedInSequence, int(seq_id))


def validate_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[bool, str | None]:
    if not isinstance(nodes, list) or not nodes:
        return False, "nodes must be non-empty list"
    if not isinstance(edges, list):
        return False, "edges must be list"
    ids = set()
    for n in nodes:
        nid = n.get("id")
        ntype = n.get("type")
        if not nid or not isinstance(nid, str):
            return False, f"node missing id: {n!r}"
        if nid in ids:
            return False, f"duplicate node id: {nid}"
        if ntype not in VALID_NODE_TYPES:
            return False, f"unknown node type: {ntype}"
        ids.add(nid)
    for e in edges:
        if e.get("from") not in ids or e.get("to") not in ids:
            return False, f"edge references missing node: {e!r}"
        on = e.get("on")
        if on is not None and on not in VALID_EDGE_LABELS:
            return False, f"unknown edge label: {on}"
    return True, None


def create_sequence(
    db: Session, organization_id: int, *, name: str, nodes: list[dict[str, Any]], edges: list[dict[str, Any]],
) -> LinkedInSequence:
    ok, err = validate_graph(nodes, edges)
    if not ok:
        raise ValueError(err or "invalid graph")
    seq = LinkedInSequence(
        organization_id=organization_id,
        name=(name or "Untitled").strip()[:255],
        nodes_json=nodes,
        edges_json=edges,
    )
    db.add(seq)
    db.commit()
    return seq


def archive_sequence(db: Session, seq_id: int) -> bool:
    seq = get_sequence(db, seq_id)
    if seq is None:
        return False
    seq.is_archived = True
    db.commit()
    return True


def make_default_invite_then_dm_template(
    *,
    invitation_template_id: int | None = None,
    dm_template_id: int | None = None,
    wait_hours_after_invite: int = 48,
    wait_hours_before_dm: int = 24,
    wait_hours_for_reply: int = 72,
) -> dict[str, Any]:
    """Шаблон по умолчанию: invite → wait → check accepted → DM → wait → check reply → stop.

    Возвращает dict {nodes, edges} — готов для `create_sequence`.
    """
    nodes = [
        {"id": "n1", "type": "send_invitation", "params": {"template_id": invitation_template_id, "entry": True}},
        {"id": "n2", "type": "wait_hours", "params": {"hours": wait_hours_after_invite}},
        {"id": "n3", "type": "check_invite_accepted", "params": {}},
        {"id": "n4", "type": "wait_hours", "params": {"hours": wait_hours_before_dm}},
        {"id": "n5", "type": "send_dm", "params": {"template_id": dm_template_id}},
        {"id": "n6", "type": "wait_hours", "params": {"hours": wait_hours_for_reply}},
        {"id": "n7", "type": "check_reply", "params": {}},
        {"id": "stop_done", "type": "stop", "params": {"reason": "completed"}},
        {"id": "stop_no_accept", "type": "stop", "params": {"reason": "invitation_not_accepted"}},
    ]
    edges = [
        {"from": "n1", "to": "n2", "on": "ok"},
        {"from": "n1", "to": "stop_no_accept", "on": "fail"},
        {"from": "n2", "to": "n3"},
        {"from": "n3", "to": "n4", "on": "accepted"},
        {"from": "n3", "to": "stop_no_accept", "on": "not_yet"},
        {"from": "n4", "to": "n5"},
        {"from": "n5", "to": "n6", "on": "ok"},
        {"from": "n5", "to": "stop_done", "on": "fail"},
        {"from": "n6", "to": "n7"},
        {"from": "n7", "to": "stop_done"},
    ]
    return {"nodes": nodes, "edges": edges}


def parse_graph_from_json_text(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Парсит textarea с JSON-объектом {nodes, edges}."""
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return None
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None
    return nodes, edges


def start_run(
    db: Session,
    sequence_id: int,
    lead_id: int,
    account_id: int,
) -> LinkedInSequenceRun:
    seq = get_sequence(db, sequence_id)
    if seq is None:
        raise ValueError("sequence not found")
    nodes = seq.nodes_json or []
    if not nodes:
        raise ValueError("empty graph")
    entry = next((n for n in nodes if (n.get("params") or {}).get("entry")), nodes[0])
    run = LinkedInSequenceRun(
        sequence_id=sequence_id,
        lead_id=lead_id,
        account_id=account_id,
        current_node_id=str(entry["id"]),
        state="active",
        next_action_at=datetime.now(timezone.utc),
        state_json={},
    )
    db.add(run)
    db.commit()
    return run


def list_runs(db: Session, sequence_id: int) -> list[LinkedInSequenceRun]:
    return (
        db.query(LinkedInSequenceRun)
        .filter(LinkedInSequenceRun.sequence_id == sequence_id)
        .order_by(LinkedInSequenceRun.created_at.desc())
        .limit(200)
        .all()
    )


__all__ = [
    "VALID_EDGE_LABELS",
    "VALID_NODE_TYPES",
    "archive_sequence",
    "create_sequence",
    "get_sequence",
    "list_runs",
    "list_sequences",
    "make_default_invite_then_dm_template",
    "parse_graph_from_json_text",
    "start_run",
    "validate_graph",
]
