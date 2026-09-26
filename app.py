"""博物馆藏品来源与返还审查系统。

业务规则（状态机、角色、交付方式、阻塞判定）集中在 rules.py 维护；
本文件负责持久化、留痕与 HTTP 入口；页面入口在 web/index.html。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import rules

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','return_in_progress','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handovers(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    delivery_method TEXT NOT NULL,
                    handler_id TEXT NOT NULL REFERENCES users(id),
                    receiver_name TEXT NOT NULL,
                    receiver_user_id TEXT REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'pending_outbound'
                        CHECK(status IN ('pending_outbound','outbound','signed','rejected','invalid')),
                    frozen_object_version INTEGER NOT NULL,
                    evidence_summary TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    outbound_by TEXT REFERENCES users(id), outbound_at TEXT,
                    signed_name TEXT, signed_at TEXT,
                    reject_reason TEXT, invalid_reason TEXT,
                    created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS handover_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handover_id INTEGER NOT NULL REFERENCES handovers(id),
                    actor_id TEXT NOT NULL REFERENCES users(id),
                    action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    # -- 返还交接内部辅助 ---------------------------------------------------

    def _evidence_summary(self, conn, object_id):
        """建单时冻结的证据摘要：文件名 + SHA-256 清单及其整体摘要值。"""
        rows = conn.execute(
            "SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? ORDER BY id", (object_id,)
        ).fetchall()
        items = [dict(r) for r in rows]
        digest = hashlib.sha256(
            json.dumps(items, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return items, digest

    def _handover_event(self, conn, handover_id, actor, action, detail):
        conn.execute(
            "INSERT INTO handover_events(handover_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (handover_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _set_claim_status(self, conn, claim, new_status, actor, note):
        """交接动作驱动的主张流转：写审查留痕、推进藏品版本并保存快照。"""
        conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim["id"]))
        conn.execute(
            "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
            (claim["id"], actor, claim["status"], new_status, note, now()),
        )
        obj = self._object(conn, claim["object_id"])
        next_version = obj["version"] + 1
        conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
        self._snapshot(conn, claim["object_id"], actor)
        self._audit(conn, claim["object_id"], actor, "claim.transition",
                    {"claim_id": claim["id"], "from": claim["status"], "to": new_status})
        return next_version

    def _invalidate_active_handovers(self, conn, object_id, actor, reason_code, reason_text):
        """待交付期间藏品或证据变化：活动交接单失效，对应主张退回重核。

        由对象/证据变更在提升版本号之前调用，快照会记录失效后的状态。
        """
        rows = conn.execute(
            """SELECT h.*, c.status AS claim_status FROM handovers h
               JOIN claims c ON c.id=h.claim_id
               WHERE h.object_id=? AND h.status IN ('pending_outbound','outbound')""",
            (object_id,),
        ).fetchall()
        for handover in rows:
            conn.execute(
                "UPDATE handovers SET status='invalid',invalid_reason=?,updated_at=? WHERE id=?",
                (reason_text, now(), handover["id"]),
            )
            self._handover_event(conn, handover["id"], actor, "invalidate",
                                 {"reason_code": reason_code, "reason": reason_text})
            self._audit(conn, object_id, actor, "handover.invalidate",
                        {"handover_id": handover["id"], "reason_code": reason_code})
            claim = {"id": handover["claim_id"], "object_id": object_id, "status": handover["claim_status"]}
            self._set_claim_status(conn, claim, rules.INVALIDATE_TO, actor,
                                   f"交接单 #{handover['id']} 失效（{reason_text}），主张退回重核。")
        return len(rows)

    def _handover_row(self, conn, handover_id):
        row = conn.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
        if not row:
            raise BusinessError("返还交接单不存在", 404, "not_found")
        return row

    def _handover_payload(self, conn, handover, include_events=True):
        data = dict(handover)
        data["delivery_method_label"] = rules.DELIVERY_METHODS.get(handover["delivery_method"], handover["delivery_method"])
        data["evidence_summary"] = json.loads(handover["evidence_summary"])
        if include_events:
            data["events"] = [dict(e) for e in conn.execute(
                "SELECT id,actor_id,action,detail,created_at FROM handover_events WHERE handover_id=? ORDER BY id",
                (handover["id"],),
            ).fetchall()]
        return data

    def _stale_blocks(self, conn, handover):
        """活动单据的冻结依据校验：返回阻塞原因列表（空列表表示未漂移）。"""
        if handover["status"] not in rules.ACTIVE_STATUSES:
            return []
        obj = self._object(conn, handover["object_id"])
        _, digest = self._evidence_summary(conn, handover["object_id"])
        return rules.freeze_blocks(handover, obj["version"], digest)

    def _guard_fresh(self, conn, handover, actor, action):
        """动作前校验冻结依据；已漂移则当场失效并阻止本次操作。

        失效结果先提交再抛错，避免调用方回滚把失效记录一并撤销。
        """
        blocks = self._stale_blocks(conn, handover)
        if not blocks:
            return
        reason = "；".join(b["message"] for b in blocks)
        conn.execute(
            "UPDATE handovers SET status='invalid',invalid_reason=?,updated_at=? WHERE id=?",
            (reason, now(), handover["id"]),
        )
        self._handover_event(conn, handover["id"], actor, "invalidate",
                             {"reason_code": "stale_on_action", "action": action, "reason": reason})
        self._audit(conn, handover["object_id"], actor, "handover.invalidate",
                    {"handover_id": handover["id"], "reason_code": "stale_on_action"})
        claim = conn.execute("SELECT * FROM claims WHERE id=?", (handover["claim_id"],)).fetchone()
        self._set_claim_status(conn, dict(claim), rules.INVALIDATE_TO, actor,
                               f"交接单 #{handover['id']} 失效（{reason}），主张退回重核。")
        conn.commit()
        raise BusinessError(f"交接依据已变化，单据失效并退回重核：{reason}", 409, "handover_invalidated")

    def _receiver_allowed(self, handover, user):
        """接收人关联了系统账号时只能本人签收/拒收，否则由工作人员代为登记。"""
        if handover["receiver_user_id"]:
            return user["id"] == handover["receiver_user_id"]
        return user["role"] == "staff"


    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            self._object(conn, object_id)
            invalidated = self._invalidate_active_handovers(
                conn, object_id, user_id, "object_changed", "待交付期间藏品信息发生变化")
            row = self._object(conn, object_id)  # 失效处理可能已推进版本
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update",
                        {"version": new_version, "changes": clean, "invalidated_handovers": invalidated})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            invalidated = self._invalidate_active_handovers(
                conn, object_id, user_id, "object_changed", "待交付期间藏品流转记录发生变化")
            row = self._object(conn, object_id)  # 失效处理可能已推进版本
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add",
                        {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility,
                         "invalidated_handovers": invalidated})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            obj = self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            invalidated = self._invalidate_active_handovers(
                conn, object_id, user_id, "evidence_changed", "待交付期间证据发生新增或变更")
            obj = self._object(conn, object_id)  # 失效处理可能已推进版本
            new_version = obj["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "evidence.upload",
                        {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility,
                         "version": new_version, "invalidated_handovers": invalidated})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                allowed = rules.CLAIM_TRANSITIONS.get(claim["status"], set())
                if new_status not in allowed:
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                next_version = self._set_claim_status(conn, dict(claim), new_status, user_id, note.strip())
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    # -- 返还交接单 ----------------------------------------------------------

    def create_handover(self, user_id, claim_id, delivery_method, handler_id, receiver_name, receiver_user_id=None):
        """审查员在协商阶段建单：登记交付方式、经办人、接收人，并冻结依据。"""
        delivery_method = (delivery_method or "").strip()
        receiver_name = (receiver_name or "").strip()
        if delivery_method not in rules.DELIVERY_METHODS:
            raise BusinessError("交付方式必须是：" + "、".join(rules.DELIVERY_METHODS), 422, "invalid_delivery_method")
        if not receiver_name:
            raise BusinessError("接收人不能为空", 422, "invalid_receiver")
        with self.connect() as conn:
            self._user(conn, user_id, rules.HANDOVER_ACTION_ROLES["create"])
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                if conn.execute(
                    "SELECT 1 FROM handovers WHERE claim_id=? AND status IN ('pending_outbound','outbound')", (claim_id,)
                ).fetchone():
                    raise BusinessError("该主张已有进行中的交接单", 409, "handover_exists")
                if claim["status"] not in rules.CREATE_HANDOVER_FROM:
                    raise BusinessError("只有协商阶段的主张可以创建返还交接单", 409, "invalid_claim_status")
                handler = conn.execute("SELECT * FROM users WHERE id=?", ((handler_id or "").strip(),)).fetchone()
                if not handler or handler["role"] != "staff":
                    raise BusinessError("经办人必须是工作人员账号", 422, "invalid_handler")
                receiver_user_id = (receiver_user_id or "").strip() or None
                if receiver_user_id:
                    receiver = conn.execute("SELECT * FROM users WHERE id=?", (receiver_user_id,)).fetchone()
                    if not receiver or receiver["role"] != "claimant" or receiver_user_id != claim["claimant_id"]:
                        raise BusinessError("接收人账号必须是该主张的主张人", 422, "invalid_receiver_account")
                obj = self._object(conn, claim["object_id"])
                evidence_items, evidence_digest = self._evidence_summary(conn, claim["object_id"])
                cur = conn.execute(
                    """INSERT INTO handovers(claim_id,object_id,delivery_method,handler_id,receiver_name,receiver_user_id,
                                             status,frozen_object_version,evidence_summary,evidence_digest,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (claim_id, claim["object_id"], delivery_method, handler["id"], receiver_name, receiver_user_id,
                     rules.PENDING_OUTBOUND, obj["version"], json.dumps(evidence_items, ensure_ascii=False, sort_keys=True),
                     evidence_digest, user_id, now(), now()),
                )
                handover_id = cur.lastrowid
                # 建单动作自身推进版本并生成快照，冻结的是建单完成后的版本；
                # 之后任何藏品/证据变化都会让冻结版本落后于当前版本。
                frozen_version = self._set_claim_status(conn, dict(claim), rules.RETURN_IN_PROGRESS, user_id,
                                                        f"创建返还交接单 #{handover_id}，进入交付流程。")
                conn.execute("UPDATE handovers SET frozen_object_version=?,updated_at=? WHERE id=?",
                             (frozen_version, now(), handover_id))
                self._handover_event(conn, handover_id, user_id, "create", {
                    "delivery_method": delivery_method, "handler_id": handler["id"],
                    "receiver_name": receiver_name, "receiver_user_id": receiver_user_id,
                    "frozen_object_version": frozen_version, "evidence_digest": evidence_digest,
                })
                self._audit(conn, claim["object_id"], user_id, "handover.create",
                            {"handover_id": handover_id, "claim_id": claim_id})
                return {"id": handover_id, "claim_id": claim_id, "status": rules.PENDING_OUTBOUND,
                        "frozen_object_version": frozen_version, "evidence_digest": evidence_digest}
            except Exception:
                conn.rollback()
                raise

    def outbound_handover(self, user_id, handover_id):
        """登记的经办人出库。"""
        with self.connect() as conn:
            self._user(conn, user_id, rules.HANDOVER_ACTION_ROLES["outbound"])
            try:
                conn.execute("BEGIN IMMEDIATE")
                handover = self._handover_row(conn, handover_id)
                if handover["status"] != rules.PENDING_OUTBOUND:
                    raise BusinessError(f"当前状态 {handover['status']} 不能出库", 409, "invalid_handover_status")
                if user_id != handover["handler_id"]:
                    raise BusinessError("只有交接单登记的经办人可以出库", 403, "not_handler")
                self._guard_fresh(conn, handover, user_id, "outbound")
                conn.execute(
                    "UPDATE handovers SET status='outbound',outbound_by=?,outbound_at=?,updated_at=? WHERE id=?",
                    (user_id, now(), now(), handover_id),
                )
                self._handover_event(conn, handover_id, user_id, "outbound", {"handler_id": user_id})
                self._audit(conn, handover["object_id"], user_id, "handover.outbound", {"handover_id": handover_id})
                return {"id": handover_id, "status": rules.OUTBOUND}
            except Exception:
                conn.rollback()
                raise

    def sign_handover(self, user_id, handover_id):
        """接收人签收：主张才能置为已返还。"""
        with self.connect() as conn:
            user = self._user(conn, user_id, rules.HANDOVER_ACTION_ROLES["sign"])
            try:
                conn.execute("BEGIN IMMEDIATE")
                handover = self._handover_row(conn, handover_id)
                if handover["status"] != rules.OUTBOUND:
                    raise BusinessError(f"当前状态 {handover['status']} 不能签收", 409, "invalid_handover_status")
                if not self._receiver_allowed(handover, user):
                    raise BusinessError("只有交接单登记的接收人可以签收", 403, "not_receiver")
                self._guard_fresh(conn, handover, user_id, "sign")
                conn.execute(
                    "UPDATE handovers SET status='signed',signed_name=?,signed_at=?,updated_at=? WHERE id=?",
                    (handover["receiver_name"], now(), now(), handover_id),
                )
                self._handover_event(conn, handover_id, user_id, "sign",
                                     {"signed_name": handover["receiver_name"], "signed_by_user": user_id})
                self._audit(conn, handover["object_id"], user_id, "handover.sign", {"handover_id": handover_id})
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (handover["claim_id"],)).fetchone()
                self._set_claim_status(conn, dict(claim), rules.RESOLVED_RETURN, user_id,
                                       f"交接单 #{handover_id} 已签收，主张完成返还。")
                return {"id": handover_id, "status": rules.SIGNED, "claim_status": rules.RESOLVED_RETURN}
            except Exception:
                conn.rollback()
                raise

    def reject_handover(self, user_id, handover_id, reason):
        """接收人拒收：填写原因，主张回到协商。"""
        reason = (reason or "").strip()
        if len(reason) < 2:
            raise BusinessError("拒收必须填写原因", 422, "reject_reason_required")
        with self.connect() as conn:
            user = self._user(conn, user_id, rules.HANDOVER_ACTION_ROLES["reject"])
            try:
                conn.execute("BEGIN IMMEDIATE")
                handover = self._handover_row(conn, handover_id)
                if handover["status"] not in rules.ACTIVE_STATUSES:
                    raise BusinessError(f"当前状态 {handover['status']} 不能拒收", 409, "invalid_handover_status")
                if not self._receiver_allowed(handover, user):
                    raise BusinessError("只有交接单登记的接收人可以拒收", 403, "not_receiver")
                self._guard_fresh(conn, handover, user_id, "reject")
                conn.execute(
                    "UPDATE handovers SET status='rejected',reject_reason=?,updated_at=? WHERE id=?",
                    (reason, now(), handover_id),
                )
                self._handover_event(conn, handover_id, user_id, "reject", {"reason": reason})
                self._audit(conn, handover["object_id"], user_id, "handover.reject",
                            {"handover_id": handover_id, "reason": reason})
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (handover["claim_id"],)).fetchone()
                self._set_claim_status(conn, dict(claim), rules.REJECT_DELIVERY_TO, user_id,
                                       f"交接单 #{handover_id} 被拒收（{reason}），主张回到协商。")
                return {"id": handover_id, "status": rules.DELIVERY_REJECTED, "claim_status": rules.REJECT_DELIVERY_TO}
            except Exception:
                conn.rollback()
                raise

    def _visible_handover(self, conn, user, handover):
        if user["role"] in ("staff", "reviewer"):
            return
        if user["role"] == "claimant":
            claim = conn.execute("SELECT claimant_id FROM claims WHERE id=?", (handover["claim_id"],)).fetchone()
            if claim and claim["claimant_id"] == user["id"]:
                return
        raise BusinessError("当前角色无权查看该交接单", 403, "forbidden")

    def get_handover(self, user_id, handover_id):
        """交接单详情：冻结依据、留痕时间线与当前阻塞原因。"""
        with self.connect() as conn:
            user = self._user(conn, user_id)
            handover = self._handover_row(conn, handover_id)
            self._visible_handover(conn, user, handover)
            payload = self._handover_payload(conn, handover)
            claim = conn.execute("SELECT id,status FROM claims WHERE id=?", (handover["claim_id"],)).fetchone()
            payload["claim_status"] = claim["status"]
            blocks = []
            if handover["status"] in rules.ACTIVE_STATUSES:
                if handover["status"] == rules.PENDING_OUTBOUND:
                    blocks.append({"code": "awaiting_outbound", "message": "等待经办人出库，签收尚未开放"})
                blocks.extend(self._stale_blocks(conn, handover))
            elif handover["status"] == rules.DELIVERY_REJECTED:
                blocks.append({"code": "delivery_rejected",
                               "message": f"接收人拒收：{handover['reject_reason']}，主张已回到协商"})
            elif handover["status"] == rules.INVALID:
                blocks.append({"code": "invalidated",
                               "message": f"单据已失效：{handover['invalid_reason']}，需重核后重新建单"})
            payload["blocks"] = blocks
            return payload

    def list_claim_handovers(self, user_id, claim_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if not claim:
                raise BusinessError("权利主张不存在", 404, "not_found")
            if user["role"] == "claimant" and claim["claimant_id"] != user_id:
                raise BusinessError("当前角色无权查看该主张的交接单", 403, "forbidden")
            if user["role"] == "public":
                raise BusinessError("当前角色无权查看交接单", 403, "forbidden")
            rows = conn.execute("SELECT * FROM handovers WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
            return [self._handover_payload(conn, row, include_events=False) for row in rows]

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()],
                                      "handovers": [self._handover_payload(conn, h, include_events=False) for h in conn.execute(
                                          "SELECT * FROM handovers WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
        if len(parts) >= 3 and parts[:2] == ["api", "claims"]:
            claim_id = int(parts[2])
            if len(parts) == 4 and parts[3] == "transition" and method == "POST":
                d = self._body(); return self._send(200, store.transition_claim(user, claim_id, d.get("status", ""), d.get("note", "")))
            if len(parts) == 4 and parts[3] == "handovers" and method == "GET":
                return self._send(200, {"items": store.list_claim_handovers(user, claim_id)})
            if len(parts) == 4 and parts[3] == "handovers" and method == "POST":
                d = self._body()
                return self._send(201, store.create_handover(user, claim_id, d.get("delivery_method", ""),
                                                             d.get("handler_id", ""), d.get("receiver_name", ""),
                                                             d.get("receiver_user_id")))
        if len(parts) >= 3 and parts[:2] == ["api", "handovers"]:
            handover_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_handover(user, handover_id))
            if len(parts) == 4 and parts[3] == "outbound" and method == "POST":
                return self._send(200, store.outbound_handover(user, handover_id))
            if len(parts) == 4 and parts[3] == "sign" and method == "POST":
                return self._send(200, store.sign_handover(user, handover_id))
            if len(parts) == 4 and parts[3] == "reject" and method == "POST":
                return self._send(200, store.reject_handover(user, handover_id, self._body().get("reason", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc: self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
