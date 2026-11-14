import unittest
import tempfile
from pathlib import Path
from tumblrkit.core import connect, create_campaign, event_counts, issue_referral
from tumblrkit.integration import CampaignHandoff, CoreLinkStore, TelegramActionAdapter
from tumblrkit.bridge import Bridge, ActionRequest

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
    def authorize(self, destination, operation_key): return True
    def publish(self, destination, text, operation_key):
        self.calls += 1
        return {"message_id": "42"}
    def readback(self, destination, message_id): return self.ok

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.publisher = Publisher()
        self.ledger_dir=tempfile.TemporaryDirectory()
        self.addCleanup(self.ledger_dir.cleanup)
        self.db=connect(str(Path(self.ledger_dir.name)/"receipts.sqlite3"))
        self.addCleanup(self.db.close)
        self.flow = CampaignHandoff(Links(), Gate(), self.publisher,delivery_db=self.db)
    def test_telegram_bridge_uses_existing_handoff_without_new_provider(self):
        bridge=Bridge({"telegram": TelegramActionAdapter(
            self.flow,member_id="verified-user")})
        membership=bridge.dispatch(ActionRequest(
            "gate-1","telegram.check_membership","g1"))
        self.assertEqual(membership.state,"completed")
        self.assertEqual(membership.detail["status"],"member_verified")
        self.assertEqual(self.publisher.calls,0)
        requested=ActionRequest("mpb-1","telegram.publish_announcement","g1",
            payload={"text":"Notice"},confirmed=True)
        self.assertEqual(bridge.dispatch(requested).state,"completed")
        self.assertEqual(self.publisher.calls,1)
        self.assertEqual(bridge.dispatch(requested).state,"completed")
        self.assertEqual(self.publisher.calls,1)
        no_confirm=bridge.dispatch(ActionRequest("mpb-2",
            "telegram.publish_announcement","g1",
            payload={"text":"Notice"}))
        self.assertEqual(no_confirm.state,"confirmation_required")
        self.assertEqual(self.publisher.calls,1)

    def test_telegram_bridge_unconnected_and_invalid_input(self):
        bridge=Bridge({"telegram":TelegramActionAdapter(self.flow)})
        missing=bridge.dispatch(ActionRequest(
            "gate-missing","telegram.check_membership","g1"))
        self.assertEqual(missing.state,"unverified")
        self.assertEqual(self.publisher.calls,0)
        invalid=bridge.dispatch(ActionRequest(
            "invalid","telegram.publish_announcement","g1",
            payload={},confirmed=True))
        self.assertEqual(invalid.state,"failed")
        self.assertEqual(self.publisher.calls,0)

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

    def test_gate_timeout_never_credits_membership_or_rewards(self):
        class UnavailableGate:
            calls=0
            def verify_member(self, group_id, user_id):
                self.calls += 1
                raise TimeoutError("GATE readback unavailable")
        gate=UnavailableGate()
        db=connect()
        try:
            create_campaign(db,"gate-timeout","safe",{1:"reward"})
            referral=issue_referral(db,"gate-timeout","alice")
            flow=CampaignHandoff(Links(),gate,self.publisher,
                                 campaign_group=lambda _: "g1")
            self.assertEqual(flow.check_membership("g1","bob").status,"not_verified")
            result=flow.attribute_membership(db,"gate-timeout","g1","bob",referral)
            self.assertEqual(result.status,"not_verified")
            self.assertEqual(result.new_rewards,())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM memberships").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM grants").fetchone()[0],0)
            self.assertEqual(gate.calls,2)
        finally:
            db.close()

    def test_malformed_mpb_receipt_is_unverified_not_published(self):
        class MalformedPublisher(Publisher):
            def __init__(self, response):
                super().__init__()
                self.response=response
            def publish(self, destination, text, operation_key):
                self.calls+=1
                return self.response
        for index, response in enumerate((None, [], {"message_id":42}, {"message_id":""})):
            with self.subTest(response=response):
                publisher=MalformedPublisher(response)
                ledger=connect(str(Path(self.ledger_dir.name)/f"malformed-{index}.sqlite3"))
                flow=CampaignHandoff(Links(), Gate(), publisher,delivery_db=ledger)
                self.addCleanup(flow.delivery_db.close)
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

    def test_mpb_owner_authorization_is_required_before_any_send(self):
        class NoAuthorization:
            calls=0
            def publish(self,destination,text,operation_key):
                self.calls+=1
                return {"message_id":"123"}
            def readback(self,destination,message_id): return True
        missing=NoAuthorization()
        denied=CampaignHandoff(Links(),Gate(),missing,delivery_db=self.db)
        self.assertEqual(denied.publish_announcement(
            "unsafe-destination","text","op-1",confirmed=True).status,"not_connected")
        self.assertEqual(missing.calls,0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM announcement_receipts").fetchone()[0],0)
        class UntrustedPublisher(Publisher):
            def __init__(self,result):
                super().__init__()
                self.result=result
            def authorize(self,destination,operation_key): return self.result
        for result in (1,"true",None,False):
            with self.subTest(result=result):
                publisher=UntrustedPublisher(result)
                flow=CampaignHandoff(Links(),Gate(),publisher,delivery_db=self.db)
                self.assertEqual(flow.publish_announcement(
                    "unsafe-destination","text","op-1",confirmed=True).status,"not_connected")
                self.assertEqual(publisher.calls,0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM announcement_receipts").fetchone()[0],0)

    def test_mpb_authorization_error_fails_closed_without_reservation(self):
        class UnavailableOwner(Publisher):
            def authorize(self,destination,operation_key):
                raise TimeoutError("authorization unavailable")
        publisher=UnavailableOwner()
        flow=CampaignHandoff(Links(),Gate(),publisher,delivery_db=self.db)
        self.assertEqual(flow.publish_announcement(
            "group","text","auth-failure",confirmed=True).status,"not_connected")
        self.assertEqual(flow.reconcile_announcement(
            "group","text","auth-failure").status,"not_connected")
        self.assertEqual(publisher.calls,0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM announcement_receipts "
            "WHERE operation_key='auth-failure'").fetchone()[0],0)

    def test_memory_ledger_blocks_publish_and_readback_without_owner_calls(self):
        class CountingPublisher(Publisher):
            def __init__(self):
                super().__init__()
                self.auth_calls=0
                self.lookups=0
            def authorize(self, destination, operation_key):
                self.auth_calls+=1
                return True
            def find_by_operation_key(self, destination, operation_key):
                self.lookups+=1
                return {"message_id":"42"}
        db=connect()
        self.addCleanup(db.close)
        publisher=CountingPublisher()
        flow=CampaignHandoff(Links(),Gate(),publisher,delivery_db=db)
        self.assertEqual(flow.publish_announcement(
            "group","text","volatile",confirmed=True).status,"not_connected")
        self.assertEqual(flow.reconcile_announcement(
            "group","text","volatile").status,"not_connected")
        self.assertEqual(publisher.calls,0)
        self.assertEqual(publisher.auth_calls,0)
        self.assertEqual(publisher.lookups,0)
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM announcement_receipts").fetchone()[0],0)

    def test_announcement_requires_persistent_ledger_before_mpb(self):
        flow=CampaignHandoff(Links(), Gate(), self.publisher)
        self.assertEqual(flow.publish_announcement(
            "group","text","key",confirmed=True).status,"not_connected")
        self.assertEqual(self.publisher.calls,0)

    def test_announcement_replay_never_republishes_or_mutates_payload(self):
        done=self.flow.publish_announcement("group","text","stable",confirmed=True)
        self.assertEqual(done.status,"published")
        self.assertEqual(self.publisher.calls,1)
        again=self.flow.publish_announcement("group","text","stable",confirmed=True)
        self.assertEqual(again.status,"published")
        self.assertEqual(self.publisher.calls,1)
        for destination,text in (("different","text"),("group","different")):
            with self.assertRaisesRegex(ValueError,"announcement_operation_conflict"):
                self.flow.publish_announcement(destination,text,"stable",confirmed=True)
        self.assertEqual(self.publisher.calls,1)
        self.assertEqual(self.db.execute(
            "SELECT state FROM announcement_receipts WHERE operation_key='stable'"
        ).fetchone()[0],"verified")

    def test_unknown_mpb_result_remains_reserved_and_not_reposted(self):
        class UnknownPublisher(Publisher):
            def publish(self,destination,text,operation_key):
                self.calls+=1
                return None
        publisher=UnknownPublisher()
        flow=CampaignHandoff(Links(),Gate(),publisher,delivery_db=self.db)
        first=flow.publish_announcement("group","text","unknown",confirmed=True)
        self.assertEqual(first.status,"unverified")
        second=flow.publish_announcement("group","text","unknown",confirmed=True)
        self.assertEqual(second.status,"unverified")
        self.assertEqual(publisher.calls,1)
        self.assertEqual(self.db.execute(
            "SELECT state FROM announcement_receipts WHERE operation_key='unknown'"
        ).fetchone()[0],"reserved")

    def test_sent_receipt_reconciles_by_readback_without_second_publish(self):
        self.publisher.ok=False
        first=self.flow.publish_announcement("group","text","sent",confirmed=True)
        self.assertEqual(first.status,"unverified")
        self.assertEqual(first.reference,"42")
        self.assertEqual(self.db.execute(
            "SELECT state FROM announcement_receipts WHERE operation_key='sent'"
        ).fetchone()[0],"sent")
        self.publisher.ok=True
        result=self.flow.publish_announcement("group","text","sent",confirmed=True)
        self.assertEqual(result.status,"published")
        self.assertEqual(self.publisher.calls,1)

    def test_mpb_readback_exception_retains_sent_and_never_reposts(self):
        class FlakyReadback(Publisher):
            def __init__(self):
                super().__init__()
                self.fail=True
            def readback(self,destination,message_id):
                if self.fail:
                    raise TimeoutError("MPB readback unavailable")
                return True
        publisher=FlakyReadback()
        flow=CampaignHandoff(Links(),Gate(),publisher,delivery_db=self.db)
        first=flow.publish_announcement("group","text","flaky",confirmed=True)
        self.assertEqual(first.status,"unverified")
        self.assertEqual(first.reference,"42")
        self.assertEqual(self.db.execute(
            "SELECT state FROM announcement_receipts WHERE operation_key='flaky'"
        ).fetchone()[0],"sent")
        second=flow.publish_announcement("group","text","flaky",confirmed=True)
        self.assertEqual(second.status,"unverified")
        self.assertEqual(publisher.calls,1)
        publisher.fail=False
        recovered=flow.reconcile_announcement("group","text","flaky")
        self.assertEqual(recovered.status,"published")
        self.assertEqual(publisher.calls,1)
        self.assertEqual(self.db.execute(
            "SELECT state FROM announcement_receipts WHERE operation_key='flaky'"
        ).fetchone()[0],"verified")

    def test_confirm_requires_exact_boolean_not_truthy_text(self):
        for unconfirmed in (False, None, "true", 1):
            with self.subTest(value=unconfirmed):
                result=self.flow.publish_announcement(
                    "group","text","confirmation",confirmed=unconfirmed)
                self.assertEqual(result.status,"confirmation_required")
        self.assertEqual(self.publisher.calls,0)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM announcement_receipts").fetchone()[0],0)

    def test_delivery_ledger_survives_process_restart_for_verified_and_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            path=str(Path(temp)/"delivery.sqlite3")
            db=connect(path)
            flow=CampaignHandoff(Links(),Gate(),self.publisher,delivery_db=db)
            self.assertEqual(flow.publish_announcement(
                "group","text","done",confirmed=True).status,"published")
            db.close()
            db=connect(path)
            flow=CampaignHandoff(Links(),Gate(),self.publisher,delivery_db=db)
            try:
                self.assertEqual(flow.publish_announcement(
                    "group","text","done",confirmed=True).status,"published")
                self.assertEqual(self.publisher.calls,1)
                class UnknownPublisher(Publisher):
                    def publish(self,destination,text,operation_key):
                        self.calls+=1
                        raise TimeoutError("outcome unknown")
                other=UnknownPublisher()
                unverified=CampaignHandoff(Links(),Gate(),other,delivery_db=db)
                self.assertEqual(unverified.publish_announcement(
                    "group","text-2","unknown-key",confirmed=True).status,"unverified")
                self.assertEqual(other.calls,1)
            finally:
                db.close()
            db=connect(path)
            try:
                restored=CampaignHandoff(Links(),Gate(),other,delivery_db=db)
                self.assertEqual(restored.publish_announcement(
                    "group","text-2","unknown-key",confirmed=True).status,"unverified")
                self.assertEqual(other.calls,1)
                self.assertEqual(db.execute(
                    "SELECT state FROM announcement_receipts "
                    "WHERE operation_key='unknown-key'").fetchone()[0],"reserved")
            finally:
                db.close()

    def test_read_only_mpb_reconcile_recovers_unknown_post_without_republish(self):
        class UnknownThenFound(Publisher):
            def __init__(self):
                super().__init__()
                self.lookups=0
            def publish(self,destination,text,operation_key):
                self.calls+=1
                return None
            def find_by_operation_key(self,destination,operation_key):
                self.lookups+=1
                return {"message_id":"42"}
        publisher=UnknownThenFound()
        flow=CampaignHandoff(Links(),Gate(),publisher,delivery_db=self.db)
        self.assertEqual(flow.publish_announcement(
            "group","text","unknown",confirmed=True).status,"unverified")
        self.assertEqual(publisher.calls,1)
        publisher.ok=False
        not_yet=flow.reconcile_announcement("group","text","unknown")
        self.assertEqual(not_yet.status,"unverified")
        self.assertEqual(not_yet.reference,"42")
        self.assertEqual(self.db.execute(
            "SELECT state FROM announcement_receipts WHERE operation_key='unknown'"
        ).fetchone()[0],"sent")
        publisher.ok=True
        confirmed=flow.reconcile_announcement("group","text","unknown")
        self.assertEqual(confirmed.status,"published")
        self.assertEqual(publisher.calls,1)
        self.assertEqual(publisher.lookups,1)
        self.assertEqual(flow.publish_announcement(
            "group","text","unknown",confirmed=True).status,"published")
        self.assertEqual(publisher.calls,1)
        with self.assertRaisesRegex(ValueError,"announcement_operation_conflict"):
            flow.reconcile_announcement("group","changed","unknown")

    def test_unknown_delivery_without_owner_lookup_cannot_be_faked_complete(self):
        class NoLookup(Publisher):
            def publish(self,destination,text,operation_key):
                self.calls+=1
                return None
        publisher=NoLookup()
        flow=CampaignHandoff(Links(),Gate(),publisher,delivery_db=self.db)
        flow.publish_announcement("group","text","unknown-2",confirmed=True)
        self.assertEqual(flow.reconcile_announcement(
            "group","text","unknown-2").status,"unverified")
        self.assertEqual(publisher.calls,1)
        self.assertEqual(self.db.execute(
            "SELECT state FROM announcement_receipts WHERE operation_key='unknown-2'"
        ).fetchone()[0],"reserved")

    def test_missing_event_id_rejected(self):
        with self.assertRaises(ValueError): self.flow.track_entry("", "l1")

if __name__ == "__main__": unittest.main()
