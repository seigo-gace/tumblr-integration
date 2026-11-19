"""Keep the original 79-action design-to-source map synchronized.

Routing counts are informational only: a UI route is NOT acceptance.
"""
from pathlib import Path
import re
import unittest

from tumblrkit.action_catalog import CATALOG, OWNER_UI_ROUTES, BRIDGE_ROUTES


class ActionCoverageMapTests(unittest.TestCase):
    def test_every_original_action_has_exactly_one_status_in_source_map(self):
        document=(Path(__file__).resolve().parents[1]/
                  "docs"/"79_ACTION_COVERAGE.md").read_text(encoding="utf-8")
        found=[line.split("`")[1] for line in document.splitlines() if line.startswith("| `")]
        expected=[action.action_id for action in CATALOG]
        self.assertEqual(found,expected)
        self.assertEqual(len(set(found)),79)
        self.assertFalse(set(OWNER_UI_ROUTES)&set(BRIDGE_ROUTES))
        self.assertTrue(set(OWNER_UI_ROUTES)<=set(expected))
        self.assertTrue(set(BRIDGE_ROUTES)<=set(expected))
        self.assertEqual(len(expected)-len(OWNER_UI_ROUTES)-len(BRIDGE_ROUTES),20)

    def test_source_owner_routes_stay_distinct_from_external_acceptance(self):
        document=(Path(__file__).resolve().parents[1]/
                  "docs"/"79_ACTION_COVERAGE.md").read_text(encoding="utf-8")
        for action_id in ("library.07","analytics.04","media.05","media.06",
                          "media.07","referral.07","analytics.11","engagement.06","publisher.06","analytics.06"):
            self.assertIn(action_id,OWNER_UI_ROUTES)
            self.assertIn("Source",next(line for line in document.splitlines()
                                        if line.startswith("| `"+action_id+"` |")))
        self.assertIn("Runtime and user acceptance remain",document)


if __name__=="__main__":
    unittest.main()
