"""Campaign/Referral reuse existing GATE owner member confirmation, offline only."""
import tempfile
import unittest
from pathlib import Path

from tumblrkit.core import connect
from tumblrkit.campaign_runtime import CampaignContest, GateEligibility
from tumblrkit.referral_runtime import ReferralManager


class FakeExistingGateOwner:
    def __init__(self):
        self.calls=[]
    def perform(self,action,target,payload):
        self.calls.append((action,target,payload))
        return {"verified":target=="123:42"}


class GateCampaignTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db=connect(str(Path(temp.name)/"private.sqlite3"))
        self.addCleanup(self.db.close)
        self.backend=FakeExistingGateOwner()
        self.gate=GateEligibility(self.db,self.backend)
        self.owner=CampaignContest(self.db,clock=lambda:1000,eligibility=self.gate)
        self.owner.create("recruit","Prize",deadline=1300,winners=1,
                          gate_topic_id="123")

    def test_gate_verified_user_can_enter_and_claim_referral_benefits(self):
        self.assertEqual(self.owner.enter("recruit","42"),"entered")
        self.assertEqual(self.owner.enter("recruit","42"),"already_entered")
        referral=ReferralManager(self.db,gate=self.gate,clock=lambda:1000)
        ref=referral.issue("recruit","10")
        observed=referral.confirm("recruit","42",ref["referral_code"])
        self.assertEqual(observed["state"],"verified")
        self.assertEqual(referral.report("recruit")["referrers"][0]["verified_members"],1)
        self.assertEqual(self.backend.calls,[
            ("telegram.check_membership","123:42",{}),
            ("telegram.check_membership","123:42",{}),
            ("telegram.check_membership","123:42",{}),
        ])

    def test_denied_or_missing_gate_cannot_create_entrant(self):
        with self.assertRaisesRegex(PermissionError,"campaign_eligibility_not_verified"):
            self.owner.enter("recruit","77")
        with self.assertRaisesRegex(PermissionError,"campaign_eligibility_not_verified"):
            self.owner.enter("recruit","some-user")
        self.assertFalse(GateEligibility(self.db,None)("recruit","42"))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM campaign_entries").fetchone()[0],0)

    def test_invalid_topic_is_rejected_before_any_membership_request(self):
        with self.assertRaisesRegex(ValueError,"campaign_gate_topic_invalid"):
            self.owner.create("invalid","Prize",deadline=1300,winners=1,
                              gate_topic_id="123:malicious")
        self.assertEqual(self.backend.calls,[])


if __name__=="__main__":
    unittest.main()
