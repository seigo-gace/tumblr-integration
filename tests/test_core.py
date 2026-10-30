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

    def test_library(self):
        upsert_post(self.db,"p1","https://example.com/p1")
        mark_post(self.db,"p1",seen=True,favorite=True)
        self.assertEqual((1,1),self.db.execute("SELECT seen,favorite FROM posts WHERE post_id='p1'").fetchone())

    def test_jobs(self):
        self.assertTrue(enqueue(self.db,"job1","scan"))
        self.assertFalse(enqueue(self.db,"job1","scan"))

if __name__=="__main__":unittest.main()
