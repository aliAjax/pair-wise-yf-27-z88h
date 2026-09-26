import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


def make_claim_ready(store, inventory="M-1999-7"):
    """造一个进入协商阶段的主张，返回 (obj, claim)。"""
    obj = store.create_object("staff", inventory, "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
    claim = store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
    store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
    store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
    return obj, claim


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 4)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        # 审查员不能直接置为已返还，必须走交接单。
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        handover = self.store.create_handover("reviewer1", claim["id"], "on_site", "staff", "王先生", "claimant1")
        self.store.outbound_handover("staff", handover["id"])
        result = self.store.sign_handover("claimant1", handover["id"])
        self.assertEqual(result["claim_status"], "resolved_return")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertEqual(claimant_view["claims"][0]["handovers"][0]["status"], "signed")
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.obj, self.claim = make_claim_ready(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def claim_status(self):
        return self.store.get_object("reviewer1", self.obj["id"])["claims"][0]["status"]

    def test_create_freezes_version_and_evidence_summary(self):
        self.store.upload_evidence("staff", self.obj["id"], "a.pdf", base64.b64encode(b"a").decode(), "internal")
        handover = self.store.create_handover("reviewer1", self.claim["id"], "courier", "staff", "王先生", "claimant1")
        obj = self.store.get_object("staff", self.obj["id"])
        self.assertEqual(handover["frozen_object_version"], obj["version"])  # 冻结建单完成后的版本
        self.assertEqual(self.claim_status(), "return_in_progress")
        detail = self.store.get_handover("staff", handover["id"])
        self.assertEqual(len(detail["evidence_summary"]), 1)
        self.assertEqual(detail["evidence_summary"][0]["filename"], "a.pdf")
        self.assertEqual(detail["delivery_method_label"], "馆方运送")
        self.assertEqual(detail["blocks"][0]["code"], "awaiting_outbound")
        self.assertEqual([e["action"] for e in detail["events"]], ["create"])
        # 同一主张不能重复建单；非协商阶段不能建单。
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_handover("reviewer1", self.claim["id"], "courier", "staff", "王先生")
        self.assertEqual(ctx.exception.code, "handover_exists")
        obj2 = self.store.create_object("staff", "M-2002-1", "书画", "纸质", "馆藏", "简介。")
        claim2 = self.store.create_claim("claimant1", obj2["id"], "李氏家族", "返还书画")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_handover("reviewer1", claim2["id"], "courier", "staff", "王先生")
        self.assertEqual(ctx.exception.code, "invalid_claim_status")

    def test_outbound_sign_and_role_guards(self):
        handover = self.store.create_handover("reviewer1", self.claim["id"], "on_site", "staff", "王先生", "claimant1")
        with self.assertRaises(BusinessError) as ctx:  # 未出库不能签收
            self.store.sign_handover("claimant1", handover["id"])
        self.assertEqual(ctx.exception.code, "invalid_handover_status")
        with self.assertRaises(BusinessError) as ctx:  # 审查员不能出库
            self.store.outbound_handover("reviewer1", handover["id"])
        self.assertEqual(ctx.exception.status, 403)
        self.store.outbound_handover("staff", handover["id"])
        with self.assertRaises(BusinessError) as ctx:  # 关联了接收人账号，工作人员不能代签
            self.store.sign_handover("staff", handover["id"])
        self.assertEqual(ctx.exception.code, "not_receiver")
        result = self.store.sign_handover("claimant1", handover["id"])
        self.assertEqual(result["claim_status"], "resolved_return")
        self.assertEqual(self.claim_status(), "resolved_return")
        detail = self.store.get_handover("reviewer1", handover["id"])
        self.assertEqual(detail["blocks"], [])
        self.assertEqual([e["action"] for e in detail["events"]], ["create", "outbound", "sign"])
        self.assertEqual(detail["signed_name"], "王先生")

    def test_unlinked_receiver_signed_by_staff(self):
        handover = self.store.create_handover("reviewer1", self.claim["id"], "third_party", "staff", "王先生")
        self.store.outbound_handover("staff", handover["id"])
        with self.assertRaises(BusinessError) as ctx:  # 非接收人角色不能代签
            self.store.sign_handover("claimant1", handover["id"])
        self.assertEqual(ctx.exception.code, "not_receiver")
        result = self.store.sign_handover("staff", handover["id"])
        self.assertEqual(result["claim_status"], "resolved_return")

    def test_object_change_invalidates_pending_handover(self):
        handover = self.store.create_handover("reviewer1", self.claim["id"], "on_site", "staff", "王先生", "claimant1")
        self.store.update_object("staff", self.obj["id"], {"public_summary": "交付前补充说明。"})
        detail = self.store.get_handover("staff", handover["id"])
        self.assertEqual(detail["status"], "invalid")
        self.assertIn("藏品信息发生变化", detail["invalid_reason"])
        self.assertEqual(detail["blocks"][0]["code"], "invalidated")
        self.assertEqual(self.claim_status(), "under_review")  # 退回重核
        with self.assertRaises(BusinessError) as ctx:  # 失效单据不能继续出库
            self.store.outbound_handover("staff", handover["id"])
        self.assertEqual(ctx.exception.code, "invalid_handover_status")
        # 重核后可以重新建单走完全程。
        self.store.transition_claim("reviewer1", self.claim["id"], "negotiating", "重核通过，重新协商交付。")
        handover2 = self.store.create_handover("reviewer1", self.claim["id"], "on_site", "staff", "王先生", "claimant1")
        self.store.outbound_handover("staff", handover2["id"])
        self.store.sign_handover("claimant1", handover2["id"])
        self.assertEqual(self.claim_status(), "resolved_return")

    def test_evidence_change_invalidates_outbound_handover(self):
        handover = self.store.create_handover("reviewer1", self.claim["id"], "on_site", "staff", "王先生", "claimant1")
        self.store.outbound_handover("staff", handover["id"])
        self.store.upload_evidence("staff", self.obj["id"], "new.pdf", base64.b64encode(b"new").decode(), "internal")
        detail = self.store.get_handover("staff", handover["id"])
        self.assertEqual(detail["status"], "invalid")
        self.assertIn("证据", detail["invalid_reason"])
        self.assertEqual(self.claim_status(), "under_review")

    def test_reject_returns_claim_to_negotiating(self):
        handover = self.store.create_handover("reviewer1", self.claim["id"], "on_site", "staff", "王先生", "claimant1")
        self.store.outbound_handover("staff", handover["id"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.reject_handover("claimant1", handover["id"], "")
        self.assertEqual(ctx.exception.code, "reject_reason_required")
        result = self.store.reject_handover("claimant1", handover["id"], "包装破损，暂缓接收")
        self.assertEqual(result["claim_status"], "negotiating")
        self.assertEqual(self.claim_status(), "negotiating")
        detail = self.store.get_handover("staff", handover["id"])
        self.assertEqual(detail["status"], "rejected")
        self.assertEqual(detail["reject_reason"], "包装破损，暂缓接收")
        self.assertEqual(detail["blocks"][0]["code"], "delivery_rejected")
        # 回到协商后可以再次建单。
        again = self.store.create_handover("reviewer1", self.claim["id"], "courier", "staff", "王先生", "claimant1")
        self.assertEqual(again["status"], "pending_outbound")

    def test_stale_guard_on_action_persists_invalidation(self):
        handover = self.store.create_handover("reviewer1", self.claim["id"], "on_site", "staff", "王先生", "claimant1")
        # 绕过存储层直接改版本号，模拟未挂钩子的变更路径。
        import sqlite3
        conn = sqlite3.connect(self.store.db_path)
        conn.execute("UPDATE objects SET version=version+1 WHERE id=?", (self.obj["id"],))
        conn.commit()
        conn.close()
        with self.assertRaises(BusinessError) as ctx:
            self.store.outbound_handover("staff", handover["id"])
        self.assertEqual(ctx.exception.code, "handover_invalidated")
        # 失效结果不被回滚：单据失效、主张退回重核。
        detail = self.store.get_handover("staff", handover["id"])
        self.assertEqual(detail["status"], "invalid")
        self.assertEqual(self.claim_status(), "under_review")

    def test_handover_visibility(self):
        handover = self.store.create_handover("reviewer1", self.claim["id"], "on_site", "staff", "王先生", "claimant1")
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_handover("public", handover["id"])
        self.assertEqual(ctx.exception.status, 403)
        items = self.store.list_claim_handovers("claimant1", self.claim["id"])
        self.assertEqual(len(items), 1)
        self.assertNotIn("events", items[0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_claim_handovers("public", self.claim["id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
