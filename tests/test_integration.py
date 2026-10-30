import unittest
from tumblrkit.integration import CampaignHandoff

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
    def test_missing_event_id_rejected(self):
        with self.assertRaises(ValueError): self.flow.track_entry("", "l1")

if __name__ == "__main__": unittest.main()
