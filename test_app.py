import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


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
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        order = self.store.create_handover("reviewer1", claim["id"], "专人意送", "藏品研究员", "王氏家族代表")
        self.store.checkout_handover("staff", order["id"])
        self.store.sign_handover("staff", order["id"])
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签收完成，确认返还。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
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

    def _negotiating_claim(self, inventory="M-2010-1"):
        obj = self.store.create_object("staff", inventory, "瓷器", "日用器", "库房", "简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "后人", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        return obj, claim

    def test_handover_required_before_resolved_return(self):
        obj, claim = self._negotiating_claim()
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "尝试直接返还。")
        self.assertEqual(ctx.exception.code, "handover_required")
        self.assertIn("尚未登记返还交接单", ctx.exception.message)
        order = self.store.create_handover("reviewer1", claim["id"], "物流保价", "藏品研究员", "主张人代表")
        self.assertEqual(order["frozen_object_version"], 3)
        detail = self.store.get_handover("reviewer1", order["id"])
        self.assertEqual(detail["evidence_count"], 0)
        self.assertEqual(len(detail["evidence_digest"]), 64)
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "建单后仍未出库。")
        self.assertIn("待工作人员出库", ctx.exception.message)
        self.store.checkout_handover("staff", order["id"])
        listing = self.store.list_claim_handovers("reviewer1", claim["id"])
        self.assertIn("待接收人签收", listing["block_reason"])
        self.store.sign_handover("staff", order["id"])
        self.assertIsNone(self.store.list_claim_handovers("reviewer1", claim["id"])["block_reason"])
        result = self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签收完成，确认返还。")
        self.assertEqual(result["status"], "resolved_return")
        actions = [e["action"] for e in self.store.get_handover("staff", order["id"])["events"]]
        self.assertEqual(actions, ["create", "checkout", "sign"])

    def test_handover_invalidated_on_object_or_evidence_change(self):
        obj, claim = self._negotiating_claim()
        order = self.store.create_handover("reviewer1", claim["id"], "专人意送", "藏品研究员", "主张人代表")
        self.store.update_object("staff", obj["id"], {"public_summary": "交付前复核发现描述有误。"})
        detail = self.store.get_handover("reviewer1", order["id"])
        self.assertEqual(detail["status"], "invalidated")
        self.assertIn("藏品信息更新", detail["invalidation_reason"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.checkout_handover("staff", order["id"])
        self.assertEqual(ctx.exception.code, "invalid_handover_transition")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "单据失效后尝试返还。")
        self.assertIn("已失效", ctx.exception.message)
        order2 = self.store.create_handover("reviewer1", claim["id"], "专人意送", "藏品研究员", "主张人代表")
        self.store.upload_evidence("staff", obj["id"], "recheck.pdf", base64.b64encode(b"recheck").decode(), "internal")
        self.assertEqual(self.store.get_handover("reviewer1", order2["id"])["status"], "invalidated")
        order3 = self.store.create_handover("reviewer1", claim["id"], "专人意送", "藏品研究员", "主张人代表")
        self.store.checkout_handover("staff", order3["id"])
        self.store.sign_handover("staff", order3["id"])
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "重核后完成交接。")

    def test_handover_reject_returns_claim_to_negotiating(self):
        obj, claim = self._negotiating_claim()
        order = self.store.create_handover("reviewer1", claim["id"], "专人意送", "藏品研究员", "主张人代表")
        self.store.checkout_handover("staff", order["id"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.reject_handover("staff", order["id"], "  ")
        self.assertEqual(ctx.exception.code, "reject_reason_required")
        result = self.store.reject_handover("staff", order["id"], "包装不符合运输要求")
        self.assertEqual(result["claim_status"], "negotiating")
        view = self.store.get_object("reviewer1", obj["id"])
        self.assertEqual(view["claims"][0]["status"], "negotiating")
        self.assertIn("被拒收", view["claims"][0]["reviews"][-1]["note"])
        self.assertIn("已被拒收", self.store.list_claim_handovers("reviewer1", claim["id"])["block_reason"])
        order2 = self.store.create_handover("reviewer1", claim["id"], "物流保价", "藏品研究员", "主张人代表")
        self.assertEqual(order2["status"], "pending")

    def test_handover_roles_and_guards(self):
        obj = self.store.create_object("staff", "M-2011-9", "玉器", "饰品", "库房", "简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "后人", "返还藏品")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_handover("reviewer1", claim["id"], "专人意送", "藏品研究员", "主张人代表")
        self.assertEqual(ctx.exception.code, "invalid_claim_stage")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_handover("staff", claim["id"], "专人意送", "藏品研究员", "主张人代表")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_handover("reviewer1", claim["id"], "", "藏品研究员", "主张人代表")
        self.assertEqual(ctx.exception.code, "invalid_handover")
        order = self.store.create_handover("reviewer1", claim["id"], "专人意送", "藏品研究员", "主张人代表")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_handover("reviewer1", claim["id"], "物流保价", "藏品研究员", "主张人代表")
        self.assertEqual(ctx.exception.code, "handover_active")
        with self.assertRaises(BusinessError) as ctx:
            self.store.checkout_handover("reviewer1", order["id"])
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.sign_handover("staff", order["id"])
        self.assertEqual(ctx.exception.code, "invalid_handover_transition")
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_handover("claimant1", order["id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
