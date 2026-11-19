import tempfile
import unittest
from pathlib import Path

from tumblrkit.core import connect,create_campaign
from tumblrkit.referral_runtime import ReferralManager


class ReferralRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=connect(str(Path(self.temp.name)/"private.sqlite3"))
        self.addCleanup(self.db.close)
        create_campaign(self.db,"launch","紹介", {2:"tier2"})
        self.gate_calls=[]
        def trusted(campaign,member):
            self.gate_calls.append((campaign,member))
            return member in ("bob","charlie")
        self.owner=ReferralManager(self.db,gate=trusted,clock=lambda:1000)

    def test_tier_setup_before_first_referral_preserves_existing_table(self):
        configured=self.owner.set_tier("launch",3,"tier3")
        self.assertEqual(configured["threshold"],3)
        self.assertFalse(configured["grants_issued"])
        updated=self.owner.set_tier("launch",2,"new-tier2")
        self.assertEqual(updated["reward"],"new-tier2")
        tiers=self.db.execute(
            "SELECT threshold,reward FROM tiers WHERE campaign_id='launch' ORDER BY threshold"
        ).fetchall()
        self.assertEqual(tiers,[(2,"new-tier2"),(3,"tier3")])
        for threshold,reward in ((0,"bad"),(10001,"bad"),(True,"bad"),
                                  (3,""),(3,"x"*151)):
            with self.assertRaisesRegex(ValueError,"referral_tier_invalid"):
                self.owner.set_tier("launch",threshold,reward)
        self.owner.issue("launch","alice")
        with self.assertRaisesRegex(ValueError,"referral_tiers_locked"):
            self.owner.set_tier("launch",4,"late-tier")
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM tiers WHERE campaign_id='launch'"
        ).fetchone()[0],2)

    def test_real_gate_confirmation_reward_once_and_restart(self):
        code=self.owner.issue("launch","alice")["referral_code"]
        self.assertEqual(code,self.owner.issue("launch","alice")["referral_code"])
        self.assertEqual(self.owner.confirm("launch","bob",code)["new_rewards"],[])
        self.assertEqual(self.owner.confirm("launch","charlie",code)["new_rewards"],["tier2"])
        self.assertEqual(self.owner.confirm("launch","charlie",code)["new_rewards"],[])
        report=ReferralManager(self.db).report("launch")
        self.assertEqual(report["referrers"][0]["verified_members"],2)
        self.assertEqual(report["referrers"][0]["awarded_tiers"],1)
        self.assertEqual(report["referrers"][0]["tier_progress"][0]["threshold"],2)
        self.assertEqual(report["referrers"][0]["tier_progress"][0]["remaining"],0)
        self.assertTrue(report["referrers"][0]["tier_progress"][0]["awarded"])
        self.assertIsNotNone(report["referrers"][0]["tier_progress"][0]["awarded_at"])
        self.assertTrue(report["clicks_are_not_memberships"])

    def test_referral_report_shows_remaining_verified_members_not_clicks(self):
        code=self.owner.issue("launch","alice")["referral_code"]
        before=self.owner.report("launch")["referrers"][0]["tier_progress"][0]
        self.assertEqual(before["remaining"],2)
        self.assertFalse(before["awarded"])
        self.owner.confirm("launch","bob",code)
        current=self.owner.report("launch")["referrers"][0]["tier_progress"][0]
        self.assertEqual(current["verified_members"],1)
        self.assertEqual(current["remaining"],1)
        self.assertFalse(current["awarded"])
        self.owner.confirm("launch","charlie",code)
        earned=self.owner.report("launch")["referrers"][0]["tier_progress"][0]
        self.assertEqual(earned["remaining"],0)
        self.assertTrue(earned["awarded"])

    def test_missing_gate_blocks_without_touching_membership(self):
        code=self.owner.issue("launch","alice")["referral_code"]
        with self.assertRaisesRegex(PermissionError,"referral_gate_not_connected"):
            ReferralManager(self.db).confirm("launch","bob",code)
        with self.assertRaisesRegex(PermissionError,"referral_gate_not_verified"):
            self.owner.confirm("launch","other",code)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memberships").fetchone()[0],0)

    def test_revocation_cannot_turn_into_new_membership(self):
        code=self.owner.issue("launch","alice")["referral_code"]
        self.assertEqual(self.owner.revoke("launch","alice"),"revoked")
        self.assertEqual(self.owner.revoke("launch","alice"),"already_revoked")
        with self.assertRaisesRegex(ValueError,"referral_revoked"):
            self.owner.issue("launch","alice")
        with self.assertRaisesRegex(ValueError,"referral_code_inactive"):
            self.owner.confirm("launch","bob",code)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM memberships").fetchone()[0],0)

    def test_self_referral_is_refused_even_if_gate_verifies(self):
        code=self.owner.issue("launch","bob")["referral_code"]
        with self.assertRaisesRegex(ValueError,"referral_self_entry"):
            self.owner.confirm("launch","bob",code)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM grants").fetchone()[0],0)


if __name__=="__main__":
    unittest.main()
