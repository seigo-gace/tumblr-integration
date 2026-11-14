import hashlib
import tempfile
import unittest
from pathlib import Path

from tumblrkit.drafts import DraftStore
from tumblrkit.post_scheduler import ScheduledPublisher


class FakePublisher:
    def __init__(self,success=True):
        self.calls=[]
        self.success=success

    def perform(self,action,target,payload):
        self.calls.append((action,target,payload))
        if not self.success:
            raise OSError("uncertain provider")
        return {"verified":True,"provider_post_id":"987"}


class ScheduledPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"drafts.sqlite3"
        self.drafts=DraftStore(self.path)
        self.draft=self.drafts.save("投稿本文")
        self.digest=hashlib.sha256(self.draft["text"].encode("utf-8")).hexdigest()
        self.now=1000
        self.remote=FakePublisher()
        self.owner=ScheduledPublisher(self.drafts,self.remote,lambda:self.now)

    def test_queue_then_reschedule_and_execute_once(self):
        item=self.owner.queue(self.draft["id"],1200,self.digest)
        self.assertEqual(item["state"],"pending")
        duplicate=self.owner.queue(self.draft["id"],1200,self.digest)
        self.assertEqual(duplicate["state"],"already_scheduled")
        self.assertEqual(duplicate["schedule_id"],item["schedule_id"])
        self.assertEqual(self.owner.reschedule(item["schedule_id"],1300,self.digest)["publish_at"],1300)
        self.now=1200
        self.assertEqual(self.owner.dispatch_due()["state"],"idle")
        self.now=1301
        first=self.owner.dispatch_due()
        self.assertEqual(first["state"],"verified")
        self.assertEqual(first["provider_post_id"],"987")
        self.assertEqual(self.owner.dispatch_due()["state"],"idle")
        self.assertEqual(len(self.remote.calls),1)
        reopened=ScheduledPublisher(self.drafts,self.remote,lambda:self.now)
        self.assertEqual(reopened.list()[0]["state"],"verified")

    def test_cancelled_schedule_never_sends(self):
        item=self.owner.queue(self.draft["id"],1200,self.digest)
        self.assertTrue(self.owner.cancel(item["schedule_id"]))
        self.assertFalse(self.owner.cancel(item["schedule_id"]))
        self.now=1400
        self.assertEqual(self.owner.dispatch_due()["state"],"idle")
        self.assertEqual(self.remote.calls,[])

    def test_draft_change_at_due_time_cancels_without_posting(self):
        item=self.owner.queue(self.draft["id"],1200,self.digest)
        self.drafts.update(self.draft["id"],"編集後",self.draft["text"])
        self.now=1201
        self.assertEqual(self.owner.dispatch_due()["state"],"draft_changed")
        self.assertEqual(self.owner.list()[0]["state"],"cancelled")
        self.assertFalse(self.remote.calls)

    def test_unknown_send_is_never_repeated_even_after_restart(self):
        self.owner.publisher=FakePublisher(False)
        item=self.owner.queue(self.draft["id"],1200,self.digest)
        self.now=1201
        self.assertEqual(self.owner.dispatch_due()["state"],"needs_reconcile")
        self.assertEqual(ScheduledPublisher(self.drafts,FakePublisher(),
                         lambda:self.now).dispatch_due()["state"],"idle")
        self.assertEqual(self.owner.list()[0]["state"],"needs_reconcile")

    def test_scheduling_needs_exact_draft_digest(self):
        with self.assertRaisesRegex(ValueError,"schedule_draft_changed"):
            self.owner.queue(self.draft["id"],1200,"a"*64)
        with self.assertRaisesRegex(ValueError,"schedule_time_invalid"):
            self.owner.queue(self.draft["id"],1000,self.digest)


if __name__=="__main__":
    unittest.main()
