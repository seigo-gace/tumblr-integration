"""The original 79-action UX IDs are a contract, not 79 fake adapters."""
import unittest

from tumblrkit.action_catalog import AREAS, CATALOG, BY_ID, lookup


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
