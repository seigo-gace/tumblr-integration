import unittest
import tempfile
from pathlib import Path
from tumblrkit.core import connect, create_campaign, event_counts, issue_referral
from tumblrkit.integration import CampaignHandoff, CoreLinkStore

class Links:
    def __init__(self): self.events = set()
    def create(self, campaign_id, source_post_id): return f"{campaign_id}:{source_post_id}"
    def mark(self, event_id, event_type, link_id):
        if event_id in self.events: return False
        self.events.add(event_id)
        return True

class Gate:
    def verify_member(self, group_id, telegram_user_id): return telegram_user_id == "verified-user"

class Publisher:
    def __init__(self): self.calls = 0; self.ok = True
    def publish(self, destination, text, operation_key):
        self.calls += 1
        return {"message_id": "42"}
    def readback(self, destination, message_id): return self.ok

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.publisher = Publisher()
        self.flow = CampaignHandoff(Links(), Gate(), self.publisher)
    def test_referral_link(self):
        self.assertEqual("c1:p1", self.flow.referral_entry("c1", "p1").reference)
    def test_click_idempotence(self):
        self.assertEqual("recorded", self.flow.track_entry("e1", "l1").status)
        self.assertEqual("already_recorded", self.flow.track_entry("e1", "l1").status)
    def test_click_is_not_membership(self):
        self.flow.track_entry("e1", "l1")
        self.assertEqual("not_verified", self.flow.check_membership("g1", "other").status)
    def test_gate_verification(self):
        self.assertEqual("member_verified", self.flow.check_membership("g1", "verified-user").status)
    def test_publication_requires_confirmation(self):
        self.assertEqual("confirmation_required", self.flow.publish_announcement("g1", "msg", "k").status)
        self.assertEqual(0, self.publisher.calls)
    def test_publish_readback(self):
        self.assertEqual("published", self.flow.publish_announcement("g1", "msg", "k", confirmed=True).status)
    def test_unverified_publish(self):
        self.publisher.ok = False
        self.assertEqual("unverified", self.flow.publish_announcement("g1", "msg", "k", confirmed=True).status)
    def test_gate_readback_must_be_exact_true_not_truthy(self):
        class UntrustedGate:
            def __init__(self, answer): self.answer=answer
            def verify_member(self, group_id, user_id): return self.answer
        for wrong in (1, "true", {"verified":True}, None, False):
            with self.subTest(answer=wrong):
                flow=CampaignHandoff(Links(), UntrustedGate(wrong), self.publisher)
                result=flow.check_membership("g1","verified-user")
                self.assertEqual(result.status,"not_verified")
        flow=CampaignHandoff(Links(), UntrustedGate(True), self.publisher)
        self.assertEqual(flow.check_membership("g1","verified-user").status,"member_verified")

    def test_malformed_mpb_receipt_is_unverified_not_published(self):
        class MalformedPublisher(Publisher):
            def __init__(self, response):
                super().__init__()
                self.response=response
            def publish(self, destination, text, operation_key):
                self.calls+=1
                return self.response
        for response in (None, [], {"message_id":42}, {"message_id":""}):
            with self.subTest(response=response):
                publisher=MalformedPublisher(response)
                flow=CampaignHandoff(Links(), Gate(), publisher)
                result=flow.publish_announcement("g1","message","key",confirmed=True)
                self.assertEqual(result.status,"unverified")
                self.assertEqual(result.next_actions,("reconcile_delivery",))
                self.assertEqual(publisher.calls,1)

    def test_persistent_campaign_link_and_click_survive_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path=str(Path(temp)/"campaign.sqlite3")
            db=connect(db_path)
            create_campaign(db,"campaign","real source",{2:"tier"})
            first=CoreLinkStore(db)
            link=first.create("campaign","post-123")
            self.assertEqual(link,first.create("campaign","post-123"))
            self.assertTrue(link and ":" not in link)
            self.assertTrue(first.mark("click-1","entry",link))
            self.assertFalse(first.mark("click-1","entry",link))
            db.close()
            db=connect(db_path)
            try:
                restored=CoreLinkStore(db)
                self.assertEqual(restored.create("campaign","post-123"),link)
                self.assertFalse(restored.mark("click-1","entry",link))
                other=restored.create("campaign","post-456")
                self.assertNotEqual(link,other)
                with self.assertRaisesRegex(ValueError,"campaign_event_id_conflict"):
                    restored.mark("click-1","entry",other)
                self.assertEqual(event_counts(db),[(link,"entry",1)])
                with self.assertRaisesRegex(ValueError,"campaign_link_missing"):
                    restored.mark("click-2","entry","unknown-link")
                self.assertEqual(event_counts(db),[(link,"entry",1)])
            finally:
                db.close()

    def test_inactive_campaign_cannot_create_new_entry_link(self):
        db=connect()
        try:
            create_campaign(db,"campaign","demo",{})
            links=CoreLinkStore(db)
            previous=links.create("campaign","post-1")
            with db:
                db.execute("UPDATE campaigns SET active=0 WHERE id='campaign'")
            with self.assertRaisesRegex(ValueError,"campaign inactive"):
                links.create("campaign","post-2")
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM campaign_links").fetchone()[0],1)
            self.assertTrue(previous)
        finally:
            db.close()

    def test_verified_member_requires_bound_group_and_counts_tier_once(self):
        class ConfirmingGate:
            def __init__(self): self.calls=[]; self.answer=True
            def verify_member(self,group_id,user_id):
                self.calls.append((group_id,user_id))
                return self.answer
        db=connect()
        try:
            create_campaign(db,"c1","safe", {2:"two-referrals"})
            code=issue_referral(db,"c1","alice")
            gate=ConfirmingGate()
            no_binding=CampaignHandoff(Links(),gate,self.publisher)
            with self.assertRaisesRegex(PermissionError,"campaign_group_binding_required"):
                no_binding.attribute_membership(db,"c1","allowed","bob",code)
            self.assertEqual(gate.calls,[])
            flow=CampaignHandoff(Links(),gate,self.publisher,
                                  campaign_group=lambda campaign: "allowed" if campaign=="c1" else None)
            with self.assertRaisesRegex(PermissionError,"campaign_group_mismatch"):
                flow.attribute_membership(db,"c1","other","bob",code)
            self.assertEqual(gate.calls,[])
            gate.answer=1
            self.assertEqual(flow.attribute_membership(db,"c1","allowed","bob",code).status,
                             "not_verified")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memberships").fetchone()[0],0)
            gate.answer=True
            first=flow.attribute_membership(db,"c1","allowed","bob",code)
            self.assertEqual(first.status,"member_verified")
            self.assertEqual(first.new_rewards,())
            again=flow.attribute_membership(db,"c1","allowed","bob",code)
            self.assertEqual(again.new_rewards,())
            second=flow.attribute_membership(db,"c1","allowed","charlie",code)
            self.assertEqual(second.new_rewards,("two-referrals",))
            self.assertEqual(flow.attribute_membership(
                db,"c1","allowed","charlie",code).new_rewards,())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memberships").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM grants").fetchone()[0],1)
        finally:
            db.close()

    def test_self_referral_is_rejected_even_after_gate_succeeds(self):
        db=connect()
        try:
            create_campaign(db,"c1","safe",{1:"tier"})
            code=issue_referral(db,"c1","alice")
            flow=CampaignHandoff(Links(),Gate(),self.publisher,
                                 campaign_group=lambda _: "g1")
            with self.assertRaisesRegex(ValueError,"self referral"):
                # Fake GATE marks 'verified-user' as verified, and that user
                # must never refer themselves under an identical account ID.
                db.execute("UPDATE referrals SET referrer_id='verified-user' WHERE code=?",
                           (code,))
                flow.attribute_membership(db,"c1","g1","verified-user",code)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM grants").fetchone()[0],0)
        finally:
            db.close()

    def test_missing_event_id_rejected(self):
        with self.assertRaises(ValueError): self.flow.track_entry("", "l1")

if __name__ == "__main__": unittest.main()
