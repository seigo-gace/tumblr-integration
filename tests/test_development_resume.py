"""Resume records are current source evidence, never product acceptance."""
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.tumblr_development_resume import inspect, ROOT


class DevelopmentResumeTests(unittest.TestCase):
    def copy_records(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        target = Path(temp.name)
        # Copy only the catalog, existing records and their named paths.
        for name in ("tumblrkit", "docs", "tests", "app"):
            shutil.copytree(ROOT / name, target / name)
        return target

    def test_all_original_modules_have_scoped_next_work_without_completion(self):
        result = inspect()
        self.assertEqual((result["total_actions"], result["ui_route_count"],
                          result["bridge_route_count"], result["no_catalog_route_count"]),
                         (79, 65, 14, 0))
        self.assertEqual(len(result["modules"]), 9)
        self.assertEqual(sum(row["action_count"] for row in result["modules"]), 79)
        self.assertTrue(result["routing_is_not_acceptance"])
        self.assertEqual(result["product_completion"], "NOT_VERIFIED")
        for row in result["modules"]:
            self.assertEqual(row["runtime_acceptance"], "NOT_VERIFIED")
            self.assertTrue(row["source_files"] and row["test_files"] and row["next_work"])

    def test_selected_module_preserves_original_operation_ids_and_next_work(self):
        result = inspect(module="engagement")
        self.assertEqual(len(result["modules"]), 1)
        row = result["modules"][0]
        self.assertEqual(row["no_catalog_route_ids"], [])
        self.assertIn("Runtime acceptance remains open", row["next_work"])
        self.assertIn("tests/test_engagement_runtime.py", row["test_files"])
        with self.assertRaisesRegex(ValueError, "MODULE_UNKNOWN"):
            inspect(module="unknown")

    def test_old_summary_cannot_silently_reappear(self):
        target = self.copy_records()
        path = target / "docs/79_ACTION_COVERAGE.md"
        path.write_text(path.read_text().replace("65 link to an owner UI, 14 link to a Bridge contract, 0 have no route",
                                                "44 link to an owner UI, 13 link to a Bridge contract, 22 have no route"))
        with self.assertRaisesRegex(ValueError, "COVERAGE_TOTALS_STALE"):
            inspect(target)

    def test_stale_catalog_snapshot_is_rejected_without_importing_app(self):
        target = self.copy_records()
        path = target / "tumblrkit/action_catalog.py"
        path.write_text(path.read_text() + '\nraise RuntimeError("MUST_NOT_EXECUTE")\n')
        with self.assertRaisesRegex(ValueError, "MODULE_RECORD_CATALOG_STALE"):
            inspect(target)

    def test_missing_or_escaping_source_reference_is_rejected(self):
        for replacement in ("missing.py", "../../outside.py"):
            with self.subTest(replacement=replacement):
                target = self.copy_records()
                path = target / "docs/NINE_MODULE_SOURCE_MAP.md"
                path.write_text(path.read_text().replace("`tumblrkit/account_runtime.py`", f"`{replacement}`"))
                with self.assertRaisesRegex(ValueError, "MODULE_PATH_UNVERIFIED"):
                    inspect(target)

    def test_missing_module_and_wrong_pending_ids_do_not_produce_a_resume(self):
        target = self.copy_records()
        path = target / "docs/NINE_MODULE_SOURCE_MAP.md"
        path.write_text('\n'.join(line for line in path.read_text().splitlines()
                                   if not line.startswith("| account |")))
        with self.assertRaisesRegex(ValueError, "MODULE_RECORDS_INCOMPLETE"):
            inspect(target)
        target = self.copy_records()
        path = target / "docs/NINE_MODULE_SOURCE_MAP.md"
        path.write_text(path.read_text().replace("| engagement | — |", "| engagement | `engagement.02` |", 1))
        with self.assertRaisesRegex(ValueError, "MODULE_RECORD_ACTIONS_STALE"):
            inspect(target)


if __name__ == "__main__":
    unittest.main()
