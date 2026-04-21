"""Reddit Agent Mode (Sequences).

MVP: линейная цепочка шагов. Поддерживаются типы:
  - wait            : задержка
  - draft_dm        : создать RedditMessageDraft (требует approval)
  - send_dm         : отправить approved draft
  - draft_comment   : создать RedditCommentDraft (требует approval)
  - publish_comment : опубликовать approved comment
  - check_reply     : проверить, есть ли reply (placeholder)

Runtime: manual advance — пользователь нажимает «Run next step» на
конкретном SequenceRun, что продвигает run на следующий шаг. Это самый
безопасный default — никакого unattended execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from backend.modules.reddit.models import (
    RedditLead,
    RedditSequence,
    RedditSequenceRun,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


NODE_KINDS = (
    "wait",
    "draft_dm",
    "send_dm",
    "draft_comment",
    "publish_comment",
    "check_reply",
    "note",
)

RUN_STATES = (
    "scheduled",
    "waiting",
    "awaiting_approval",
    "completed",
    "failed",
    "cancelled",
)


@dataclass(frozen=True)
class NodeSpec:
    id: str
    kind: str
    params: dict
    requires_approval: bool = False
    description: str = ""


def parse_nodes(raw: Any) -> list[NodeSpec]:
    if not isinstance(raw, list):
        return []
    out: list[NodeSpec] = []
    for i, node in enumerate(raw):
        if not isinstance(node, dict):
            continue
        out.append(NodeSpec(
            id=str(node.get("id") or f"n{i}"),
            kind=str(node.get("kind") or "note"),
            params=dict(node.get("params") or {}),
            requires_approval=bool(node.get("requires_approval", False)),
            description=str(node.get("description") or ""),
        ))
    return out


# ── CRUD sequences ────────────────────────────────────────────────────


def list_sequences(db: "Session", organization_id: int | None) -> list[RedditSequence]:
    return (
        db.query(RedditSequence)
        .filter(RedditSequence.organization_id == organization_id)
        .order_by(RedditSequence.id.desc())
        .all()
    )


def get_sequence(db: "Session", sequence_id: int) -> RedditSequence | None:
    return db.get(RedditSequence, int(sequence_id))


def save_sequence(
    db: "Session",
    organization_id: int | None,
    *,
    sequence_id: int | None = None,
    name: str = "",
    status: str = "draft",
    nodes: list[dict] | None = None,
    timezone_name: str = "Europe/Madrid",
) -> RedditSequence:
    if sequence_id:
        row = db.get(RedditSequence, int(sequence_id))
        if row is None:
            raise ValueError("sequence not found")
    else:
        row = RedditSequence(organization_id=organization_id)
        db.add(row)

    row.name = (name or "").strip()[:200] or "Sequence"
    row.status = status if status in ("draft", "active", "paused", "archived") else "draft"
    row.nodes_json = nodes or []
    row.timezone = timezone_name or "Europe/Madrid"
    # safety caps: жёстко по умолчанию
    row.safety_caps_json = row.safety_caps_json or {
        "per_day_actions": 20,
        "per_hour_actions": 5,
    }
    # auto-mark approval gates: draft_dm / draft_comment → requires_approval=true
    nodes_list = row.nodes_json or []
    for node in nodes_list:
        if node.get("kind") in ("draft_dm", "draft_comment"):
            node["requires_approval"] = True
    row.nodes_json = nodes_list
    db.commit()
    return row


def delete_sequence(db: "Session", sequence_id: int) -> None:
    row = db.get(RedditSequence, int(sequence_id))
    if row is None:
        return
    db.delete(row)
    db.commit()


# ── Runs ───────────────────────────────────────────────────────────────


def enroll_leads(
    db: "Session",
    sequence: RedditSequence,
    lead_ids: list[int],
    *,
    account_id: int | None = None,
) -> int:
    leads = (
        db.query(RedditLead)
        .filter(
            RedditLead.organization_id == sequence.organization_id,
            RedditLead.id.in_(lead_ids),
        )
        .all()
    )
    if not leads:
        return 0
    existing_lead_ids = {
        r[0] for r in db.query(RedditSequenceRun.lead_id).filter(
            RedditSequenceRun.sequence_id == sequence.id
        ).all()
    }
    added = 0
    nodes = parse_nodes(sequence.nodes_json)
    first_node = nodes[0].id if nodes else None
    for lead in leads:
        if lead.id in existing_lead_ids:
            continue
        run = RedditSequenceRun(
            sequence_id=sequence.id,
            lead_id=lead.id,
            account_id=account_id,
            current_node_id=first_node,
            state="scheduled" if first_node else "completed",
            run_state_json={"history": []},
        )
        db.add(run)
        added += 1
    db.commit()
    return added


def list_runs(
    db: "Session",
    sequence_id: int,
    state: str | None = None,
) -> list[RedditSequenceRun]:
    q = db.query(RedditSequenceRun).filter(RedditSequenceRun.sequence_id == int(sequence_id))
    if state:
        q = q.filter(RedditSequenceRun.state == state)
    return q.order_by(RedditSequenceRun.id.desc()).all()


def cancel_run(db: "Session", run_id: int) -> None:
    run = db.get(RedditSequenceRun, int(run_id))
    if run is None:
        return
    run.state = "cancelled"
    run.completed_at = datetime.now(timezone.utc)
    db.commit()


def _find_next_node(nodes: list[NodeSpec], current_id: str | None) -> NodeSpec | None:
    if not nodes:
        return None
    if current_id is None:
        return nodes[0]
    for i, n in enumerate(nodes):
        if n.id == current_id and i + 1 < len(nodes):
            return nodes[i + 1]
    return None


def advance_run(db: "Session", run_id: int) -> dict[str, Any]:
    """Выполнить текущий node и передвинуться дальше.

    Возвращает {'ok', 'state', 'message'}.
    Жёсткое правило: шаги send_dm/publish_comment требуют, чтобы соответствующий
    draft уже был в state=approved — если нет, run переходит в awaiting_approval.
    """
    run = db.get(RedditSequenceRun, int(run_id))
    if run is None:
        return {"ok": False, "message": "run not found"}
    if run.state in ("completed", "failed", "cancelled"):
        return {"ok": True, "message": "run already terminal", "state": run.state}

    sequence = db.get(RedditSequence, int(run.sequence_id))
    if sequence is None:
        run.state = "failed"
        run.last_error = "sequence missing"
        db.commit()
        return {"ok": False, "message": "sequence missing"}

    nodes = parse_nodes(sequence.nodes_json)
    current = next((n for n in nodes if n.id == run.current_node_id), None)
    if current is None and nodes:
        current = nodes[0]
    if current is None:
        run.state = "completed"
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"ok": True, "message": "no nodes", "state": "completed"}

    result = _execute_node(db, sequence, run, current)
    # history
    hist = (run.run_state_json or {}).get("history") or []
    hist.append({
        "node_id": current.id,
        "kind": current.kind,
        "at": datetime.now(timezone.utc).isoformat(),
        "result": result,
    })
    run_state = dict(run.run_state_json or {})
    run_state["history"] = hist[-100:]
    run.run_state_json = run_state

    if not result.get("ok"):
        if result.get("pause"):
            run.state = result.get("state") or "waiting"
            db.commit()
            return {"ok": True, "message": result.get("message"), "state": run.state}
        run.state = "failed"
        run.last_error = result.get("message") or "step failed"
        db.commit()
        return {"ok": False, "message": run.last_error, "state": "failed"}

    # продвинуться
    nxt = _find_next_node(nodes, current.id)
    if nxt is None:
        run.state = "completed"
        run.completed_at = datetime.now(timezone.utc)
        run.current_node_id = current.id
        db.commit()
        return {"ok": True, "message": "done", "state": "completed"}
    run.current_node_id = nxt.id
    run.state = "scheduled"
    db.commit()
    return {"ok": True, "message": f"advanced to {nxt.id}", "state": run.state}


def _execute_node(
    db: "Session",
    sequence: RedditSequence,
    run: RedditSequenceRun,
    node: NodeSpec,
) -> dict[str, Any]:
    if node.kind == "wait":
        hours = int(node.params.get("hours") or 1)
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        # помечаем waiting + возвращаем pause
        run_state = dict(run.run_state_json or {})
        run_state["wait_until"] = until.isoformat()
        run.run_state_json = run_state
        return {"ok": False, "pause": True, "state": "waiting", "message": f"wait until {until.isoformat()}"}

    if node.kind == "draft_dm":
        from backend.modules.reddit.services import conversations as conv_svc
        from backend.modules.reddit.services import templates as templates_svc

        template_id = int(node.params.get("template_id") or 0)
        tpl = db.get(templates_svc.RedditTemplate, template_id) if template_id else None
        if tpl is None:
            return {"ok": False, "message": "template missing"}
        lead = db.get(RedditLead, int(run.lead_id)) if run.lead_id else None
        if lead is None or not lead.username:
            return {"ok": False, "message": "lead missing"}
        from backend.modules.reddit.models import RedditAccount

        account = db.get(RedditAccount, int(run.account_id)) if run.account_id else None
        if account is None:
            account = (
                db.query(RedditAccount)
                .filter(
                    RedditAccount.organization_id == sequence.organization_id,
                    RedditAccount.status == "connected",
                )
                .first()
            )
            if account is None:
                return {"ok": False, "message": "no account"}
            run.account_id = account.id

        rendered = templates_svc.render_body(tpl.body, {
            "username": lead.username,
            "topic": (lead.topic_tags_json or [""])[0] if (lead.topic_tags_json or []) else "",
        })
        conv = conv_svc.get_or_create_conversation(
            db, sequence.organization_id, account, lead.username,
            subject=f"Hi, u/{lead.username}"[:100],
        )
        db.flush()
        draft = conv_svc.create_draft(db, conv, rendered)
        # awaiting_approval: пока draft не в approved/sent, не продвигаемся
        run_state = dict(run.run_state_json or {})
        run_state["pending_draft_id"] = draft.id
        run.run_state_json = run_state
        return {"ok": False, "pause": True, "state": "awaiting_approval", "message": f"draft {draft.id} awaiting approval"}

    if node.kind == "send_dm":
        from backend.modules.reddit.services import conversations as conv_svc

        draft_id = (run.run_state_json or {}).get("pending_draft_id")
        if not draft_id:
            return {"ok": False, "message": "no pending draft to send"}
        from backend.modules.reddit.models import RedditMessageDraft

        draft = db.get(RedditMessageDraft, int(draft_id))
        if draft is None:
            return {"ok": False, "message": "draft missing"}
        if draft.review_state == "pending_review":
            return {"ok": False, "pause": True, "state": "awaiting_approval", "message": "draft still pending_review"}
        if draft.review_state == "rejected":
            return {"ok": False, "message": "draft rejected"}
        if draft.review_state == "sent":
            return {"ok": True, "message": "already sent"}
        result = conv_svc.send_draft(db, draft.id)
        if not result.get("ok"):
            return {"ok": False, "message": "send failed: " + (result.get("reason_code") or "")}
        # clear pending
        rs = dict(run.run_state_json or {})
        rs.pop("pending_draft_id", None)
        run.run_state_json = rs
        return {"ok": True, "message": "sent"}

    if node.kind == "draft_comment":
        from backend.modules.reddit.services import comments as comments_svc

        lead = db.get(RedditLead, int(run.lead_id)) if run.lead_id else None
        target = node.params.get("target_ref") or (lead.source_ref if lead else "")
        if not target:
            return {"ok": False, "message": "no target permalink"}
        body = str(node.params.get("body") or "Thanks for the post!")
        from backend.modules.reddit.models import RedditAccount

        account = db.get(RedditAccount, int(run.account_id)) if run.account_id else None
        if account is None:
            account = (
                db.query(RedditAccount)
                .filter(
                    RedditAccount.organization_id == sequence.organization_id,
                    RedditAccount.status == "connected",
                )
                .first()
            )
            if account is None:
                return {"ok": False, "message": "no account"}
            run.account_id = account.id
        draft = comments_svc.create_draft(
            db, sequence.organization_id, account.id, target, body,
        )
        rs = dict(run.run_state_json or {})
        rs["pending_comment_draft_id"] = draft.id
        run.run_state_json = rs
        return {"ok": False, "pause": True, "state": "awaiting_approval", "message": f"comment draft {draft.id} awaiting approval"}

    if node.kind == "publish_comment":
        from backend.modules.reddit.services import comments as comments_svc

        cd_id = (run.run_state_json or {}).get("pending_comment_draft_id")
        if not cd_id:
            return {"ok": False, "message": "no pending comment draft"}
        draft = comments_svc.get_draft(db, int(cd_id))
        if draft is None:
            return {"ok": False, "message": "comment draft missing"}
        if draft.review_state == "pending_review":
            return {"ok": False, "pause": True, "state": "awaiting_approval", "message": "draft still pending"}
        if draft.review_state in ("rejected", "failed"):
            return {"ok": False, "message": "draft rejected/failed"}
        if draft.review_state == "published":
            return {"ok": True, "message": "already published"}
        result = comments_svc.publish_draft(db, int(cd_id))
        if not result.get("ok"):
            return {"ok": False, "message": "publish failed: " + (result.get("reason_code") or "")}
        rs = dict(run.run_state_json or {})
        rs.pop("pending_comment_draft_id", None)
        run.run_state_json = rs
        return {"ok": True, "message": "published"}

    if node.kind == "check_reply":
        # Placeholder: реальный poll inbox будет интегрирован с /message/inbox
        return {"ok": True, "message": "check_reply placeholder"}

    if node.kind == "note":
        return {"ok": True, "message": node.description or "note"}

    return {"ok": False, "message": f"unknown node kind: {node.kind}"}


__all__ = [
    "NODE_KINDS",
    "NodeSpec",
    "RUN_STATES",
    "advance_run",
    "cancel_run",
    "delete_sequence",
    "enroll_leads",
    "get_sequence",
    "list_runs",
    "list_sequences",
    "parse_nodes",
    "save_sequence",
]
