"""The original 79-action UX IDs are a contract, not 79 fake adapters."""
import unittest

from tumblrkit.action_catalog import AREAS, CATALOG, BY_ID, lookup, resolve_bridge_action


class ActionCatalogTests(unittest.TestCase):
    def test_exact_79_design_actions_without_renumbering(self):
        self.assertEqual(len(CATALOG), 79)
        self.assertEqual(len(BY_ID), 79)
        self.assertEqual([len(area[2]) for area in AREAS],
                         [6,7,8,7,7,12,12,9,11])
        self.assertEqual(CATALOG[0].action_id, "account.01")
        self.assertEqual(CATALOG[0].name, "接続状態を見る")
        self.assertEqual(CATALOG[-1].action_id, "analytics.11")
        self.assertEqual(CATALOG[-1].name, "レポートを出す")
        self.assertEqual(lookup("publisher.07").name,"今すぐ投稿")
        self.assertTrue(lookup("publisher.07").confirmation)
        self.assertTrue(lookup("campaign.07").confirmation)
        self.assertFalse(lookup("analytics.01").confirmation)
        self.assertTrue(lookup("referral.03").confirmation)

    def test_catalog_routes_only_reuse_existing_bridge_action_names(self):
        self.assertEqual(resolve_bridge_action("analytics.01"),("analytics.get_summary","today"))
        self.assertEqual(resolve_bridge_action("analytics.03"),("analytics.get_summary","month"))
        self.assertEqual(resolve_bridge_action("discovery.02"),("tumblr.discover","followers"))
        self.assertEqual(resolve_bridge_action("discovery.05","映像"),("tumblr.discover","tag:映像"))
        self.assertEqual(resolve_bridge_action("discovery.04","映像"),("tumblr.discover","related:映像"))
        self.assertEqual(resolve_bridge_action("publisher.07","a"*32),
                         ("tumblr.publish_draft","a"*32))
        for action in ("campaign.07","referral.05","publisher.08","analytics.11"):
            with self.assertRaisesRegex(ValueError,"catalog_action_not_connected"):
                resolve_bridge_action(action,"target")
        with self.assertRaisesRegex(ValueError,"catalog_target_required"):
            resolve_bridge_action("engagement.01","")

    def test_assistant_original_id_follows_existing_confirmation_gate(self):
        from tumblrkit.assistant import Assistant
        from tumblrkit.bridge import Bridge
        class FakeOwner:
            def __init__(self):self.calls=[]
            def preview(self,action,target):
                return {"blog":"owner","title":"保存済み候補"}
            def perform(self,action,target,payload):
                self.calls.append((action,target,payload))
                return {"verified":True}
        owner=FakeOwner()
        chat=Assistant(bridge=Bridge({"engagement":owner}),providers=[])
        proposal=chat.turn(catalog_action="engagement.01",selected_target="post-42")
        self.assertEqual(proposal["state"],"confirmation_required")
        self.assertEqual(owner.calls,[])
        result=chat.turn(confirm=True,request_id=proposal["request_id"])
        self.assertEqual(result["state"],"completed")
        self.assertEqual(owner.calls[0][0],"engagement.like")
        unavailable=chat.turn(catalog_action="campaign.07",selected_target="something")
        self.assertEqual(unavailable["state"],"not_connected")
        self.assertEqual(len(owner.calls),1)

    def test_all_action_ids_are_unique_and_not_an_execution_claim(self):
        self.assertEqual(len({action.action_id for action in CATALOG}),79)
        for action in CATALOG:
            self.assertIn(action.domain, {area[0] for area in AREAS})
            self.assertIs(type(action.confirmation), bool)
            self.assertIn(action.output_type,
                          ("preview-and-confirm","report-card","media-list"))
        for value in ("publisher.13","publisher.00","media.08","",None):
            with self.assertRaisesRegex(ValueError,"catalog_action_"):
                lookup(value)


if __name__=="__main__":
    unittest.main()
