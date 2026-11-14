import unittest
from tumblrkit.core import connect, create_campaign, issue_referral, confirm_membership, record_event, event_counts, upsert_post, mark_post, enqueue

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        create_campaign(self.db,"c1","Test",{2:"bonus",3:"extra"})
        self.code=issue_referral(self.db,"c1","alice")

    def tearDown(self):self.db.close()

    def test_referral_stable(self):
        self.assertEqual(self.code,issue_referral(self.db,"c1","alice"))

    def test_referral_code_is_stable_and_inactive_campaign_does_not_issue(self):
        first=issue_referral(self.db,"c1","parallel-referrer")
        second=issue_referral(self.db,"c1","parallel-referrer")
        self.assertEqual(first,second)
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM referrals WHERE campaign_id='c1' "
            "AND referrer_id='parallel-referrer'").fetchone()[0],1)
        with self.db:
            self.db.execute("UPDATE campaigns SET active=0 WHERE id='c1'")
        with self.assertRaises(ValueError):
            issue_referral(self.db,"c1","new-referrer")
        self.assertIsNone(self.db.execute(
            "SELECT code FROM referrals WHERE referrer_id='new-referrer'").fetchone())

    def test_gate_confirmation_requires_exact_boolean_true(self):
        for invalid in (1,"true",[],None,False):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PermissionError):
                    confirm_membership(self.db,"c1","bob",
                                       gate_verified=invalid,referral_code=self.code)
        self.assertIsNone(self.db.execute(
            "SELECT member_id FROM memberships WHERE member_id='bob'").fetchone())

    def test_gate_required(self):
        with self.assertRaises(PermissionError):
            confirm_membership(self.db,"c1","bob",gate_verified=False,referral_code=self.code)

    def test_self_referral_blocked(self):
        with self.assertRaises(ValueError):
            confirm_membership(self.db,"c1","alice",gate_verified=True,referral_code=self.code)

    def test_tiers_once(self):
        self.assertEqual([],confirm_membership(self.db,"c1","bob",gate_verified=True,referral_code=self.code))
        self.assertEqual(["bonus"],confirm_membership(self.db,"c1","charlie",gate_verified=True,referral_code=self.code))
        self.assertEqual([],confirm_membership(self.db,"c1","charlie",gate_verified=True,referral_code=self.code))
        self.assertEqual(["extra"],confirm_membership(self.db,"c1","dana",gate_verified=True,referral_code=self.code))

    def test_invalid_referral(self):
        with self.assertRaises(ValueError):
            confirm_membership(self.db,"c1","bob",gate_verified=True,referral_code="nope")

    def test_event_idempotency(self):
        self.assertTrue(record_event(self.db,"e1","click","tumblr"))
        self.assertFalse(record_event(self.db,"e1","click","tumblr"))
        self.assertEqual([("tumblr","click",1)],event_counts(self.db))

    def test_reused_event_id_with_different_origin_is_rejected(self):
        self.assertTrue(record_event(self.db,"event-1","click","tumblr"))
        self.assertFalse(record_event(self.db,"event-1","click","tumblr"))
        for kind,source in (("member","tumblr"),("click","telegram")):
            with self.subTest(kind=kind,source=source):
                with self.assertRaisesRegex(ValueError,"campaign_event_id_conflict"):
                    record_event(self.db,"event-1",kind,source)
        self.assertEqual(event_counts(self.db),[("tumblr","click",1)])
        for bad in ("",None,42):
            with self.assertRaises(ValueError):
                record_event(self.db,bad,"click","tumblr")

    def test_job_key_conflict_cannot_change_existing_action(self):
        self.assertTrue(enqueue(self.db,"same-job","scan"))
        self.assertFalse(enqueue(self.db,"same-job","scan"))
        with self.assertRaisesRegex(ValueError,"campaign_job_key_conflict"):
            enqueue(self.db,"same-job","publish")
        self.assertEqual(self.db.execute(
            "SELECT action FROM jobs WHERE job_key='same-job'").fetchone()[0],"scan")
        with self.assertRaises(ValueError):
            enqueue(self.db,"","scan")

    def test_library(self):
        upsert_post(self.db,"p1","https://example.com/p1")
        mark_post(self.db,"p1",seen=True,favorite=True)
        self.assertEqual((1,1),self.db.execute("SELECT seen,favorite FROM posts WHERE post_id='p1'").fetchone())

    def test_jobs(self):
        self.assertTrue(enqueue(self.db,"job1","scan"))
        self.assertFalse(enqueue(self.db,"job1","scan"))

if __name__=="__main__":unittest.main()
