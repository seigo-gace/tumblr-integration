import tempfile
import unittest
from pathlib import Path

from tumblrkit.core import connect
from tumblrkit.campaign_runtime import CampaignContest


class CampaignRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"existing-private-ledger.sqlite3"
        self.db=connect(str(self.path))
        self.addCleanup(self.db.close)
        self.now=1000
        self.allow={"alice","bob","charlie","dan"}
        self.calls=[]
        def eligibility(campaign,applicant):
            self.calls.append((campaign,applicant))
            return applicant in self.allow
        self.owner=CampaignContest(self.db,clock=lambda:self.now,
                                   eligibility=eligibility)
        self.result=self.owner.create("launch","賞品",deadline=1100,
                                      winners=2,claim_seconds=60)

    def test_entry_draw_claim_expiry_promotion_and_restart_are_idempotent(self):
        self.assertEqual(self.owner.enter("launch","alice"),"entered")
        self.assertEqual(self.owner.enter("launch","alice"),"already_entered")
        for applicant in ("bob","charlie","dan"):
            self.owner.enter("launch",applicant)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM campaign_entries").fetchone()[0],4)
        with self.assertRaisesRegex(ValueError,"campaign_draw_too_early"):
            self.owner.draw("launch")
        self.now=1101
        first=self.owner.draw("launch")
        self.assertEqual(len(first.winners),2)
        self.assertEqual(first.commitment,self.result["seed_commitment"])
        self.assertEqual(self.owner.draw("launch"),first)
        self.assertEqual(len(self.owner.outcomes("launch")),2)
        winner=first.winners[0]
        self.assertEqual(self.owner.claim("launch",winner),"claimed")
        self.assertEqual(self.owner.claim("launch",winner),"already_claimed")
        self.now=1162
        promoted=self.owner.promote_expired("launch")
        self.assertEqual(len(promoted),1)
        self.assertEqual(self.owner.promote_expired("launch"),[])
        self.assertEqual(len(self.owner.outcomes("launch")),5)
        self.assertEqual(self.owner.close("launch"),"closed")
        self.assertEqual(self.owner.close("launch"),"already_closed")
        reopened=connect(str(self.path))
        self.addCleanup(reopened.close)
        second=CampaignContest(reopened,clock=lambda:self.now,
                               eligibility=lambda *_:True)
        self.assertEqual(second.close("launch"),"already_closed")
        with self.assertRaisesRegex(ValueError,"campaign_closed"):
            second.draw("launch")

    def test_missing_adapter_or_unverified_eligibility_never_enrolls(self):
        with self.assertRaisesRegex(PermissionError,"campaign_eligibility_not_verified"):
            self.owner.enter("launch","other")
        disconnected=CampaignContest(self.db,clock=lambda:self.now)
        with self.assertRaisesRegex(PermissionError,"campaign_eligibility_not_connected"):
            disconnected.enter("launch","alice")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM campaign_entries")
                         .fetchone()[0],0)

    def test_claim_after_deadline_fails_and_cannot_reopen_draw(self):
        self.owner.enter("launch","alice")
        self.now=1100
        first=self.owner.draw("launch")
        self.now=1160
        with self.assertRaisesRegex(ValueError,"campaign_claim_not_available"):
            self.owner.claim("launch",first.winners[0])
        self.assertEqual(self.owner.promote_expired("launch"),[])
        self.assertEqual(self.owner.draw("launch"),first)

    def test_deadline_can_change_only_before_draw_and_entries_have_status(self):
        revised=self.owner.change_deadline("launch",1200)
        self.assertEqual(revised["deadline"],1200)
        self.owner.enter("launch","alice")
        entries=self.owner.entries("launch")
        self.assertEqual(entries[0]["status"],"entered")
        self.assertIsNone(entries[0]["rank"])
        self.now=1200
        result=self.owner.draw("launch")
        self.assertEqual(result.winners,("alice",))
        with self.assertRaisesRegex(ValueError,"campaign_deadline_locked"):
            self.owner.change_deadline("launch",2000)

    def test_durable_outcomes_do_not_send_unverified_notifications(self):
        self.owner.enter("launch","alice")
        self.now=1101
        self.owner.draw("launch")
        outcome=self.owner.outcomes("launch")
        self.assertEqual(len(outcome),1)
        self.assertEqual(outcome[0]["state"],"winner")
        self.assertEqual(len(outcome[0]["event_id"]),64)
        self.assertFalse(hasattr(self.owner,"send"))
        self.assertEqual(len(self.owner.outcomes("launch")),1)


if __name__=="__main__":
    unittest.main()
