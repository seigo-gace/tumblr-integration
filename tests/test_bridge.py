import unittest
from tumblrkit.bridge import Bridge, ActionRequest

class FakeAdapter:
    def __init__(self):self.calls=0
    def perform(self,action,target,payload):
        self.calls+=1
        return {"verified":True,"provider_id":"fake-123"}

class BridgeTests(unittest.TestCase):
    def test_read_then_next_adapter_call(self):
        adapter=FakeAdapter()
        bridge=Bridge({"tumblr":adapter})
        result=bridge.dispatch(ActionRequest("r1","tumblr.discover","tag:art"))
        self.assertEqual("completed",result.state)
        self.assertEqual(1,adapter.calls)

    def test_requires_publish_confirmation(self):
        adapter=FakeAdapter()
        bridge=Bridge({"tumblr":adapter})
        result=bridge.dispatch(ActionRequest("r2","tumblr.publish_draft","my-blog"))
        self.assertEqual("confirmation_required",result.state)
        self.assertEqual(0,adapter.calls)

    def test_idempotent_result(self):
        adapter=FakeAdapter()
        bridge=Bridge({"tumblr":adapter})
        req=ActionRequest("same","tumblr.publish_draft","my-blog",confirmed=True)
        self.assertEqual("completed",bridge.dispatch(req).state)
        self.assertEqual("completed",bridge.dispatch(req).state)
        self.assertEqual(1,adapter.calls)

    def test_not_connected_is_not_success(self):
        bridge=Bridge({})
        self.assertEqual("not_connected",bridge.dispatch(ActionRequest("r3","telegram.check_membership","group")).state)

    def test_unverified_is_not_success(self):
        class Empty:
            def perform(self,*args):return {"provider_id":"x"}
        bridge=Bridge({"telegram":Empty()})
        self.assertEqual("unverified",bridge.dispatch(ActionRequest("r4","telegram.check_membership","group")).state)

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValueError):
            Bridge({}).dispatch(ActionRequest("r5","telegram.delete_group","group"))

if __name__=="__main__":unittest.main()
