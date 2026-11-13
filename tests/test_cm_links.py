import sqlite3
import unittest

from tumblrkit.cm_links import CMRegistry, append_cm_buttons


class CMRegistryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.cm = CMRegistry(self.db)

    def register(self, count=5):
        for number in range(count):
            self.cm.add(f"c{number}", f"案内{number}",
                        f"https://example.org/item/{number}")

    def test_preview_is_read_only_and_rotates_only_after_verified_delivery(self):
        self.register()
        first = self.cm.select("100")
        self.assertEqual([item["id"] for item in first], ["c0", "c1", "c2"])
        self.assertEqual(self.cm.select("100"), first)
        self.assertFalse(self.cm.advance_after_delivery(
            "100", "operation-1", [item["id"] for item in first], verified=False))
        self.assertEqual(self.cm.select("100"), first)
        self.assertTrue(self.cm.advance_after_delivery(
            "100", "operation-1", [item["id"] for item in first], verified=True))
        self.assertEqual([item["id"] for item in self.cm.select("100")],
                         ["c1", "c2", "c3"])
        self.assertFalse(self.cm.advance_after_delivery(
            "100", "operation-1", [item["id"] for item in first], verified=True))
        self.assertEqual([item["id"] for item in self.cm.select("100")],
                         ["c1", "c2", "c3"])

    def test_restart_reuses_owner_connection_and_position(self):
        self.register()
        chosen = [item["id"] for item in self.cm.select("100")]
        self.cm.advance_after_delivery("100", "op", chosen, verified=True)
        another = CMRegistry(self.db)
        self.assertEqual([item["id"] for item in another.select("100")],
                         ["c1", "c2", "c3"])
        self.assertEqual([item["id"] for item in another.select("200")],
                         ["c0", "c1", "c2"])

    def test_three_or_less_never_duplicate_and_disabled_are_skipped(self):
        self.register(3)
        self.cm.set_enabled("c1", False)
        self.assertEqual([item["id"] for item in self.cm.select("100")], ["c0", "c2"])
        self.assertEqual(self.cm.list_links()[1]["enabled"], False)
        self.cm.set_enabled("c2", False)
        self.assertEqual([item["id"] for item in self.cm.select("100")], ["c0"])
        self.cm.set_enabled("c0", False)
        self.assertEqual(self.cm.select("100"), [])

    def test_destination_binding_and_stale_selection(self):
        self.cm.add("all", "全体", "https://example.org/all")
        self.cm.add("special", "限定", "https://example.org/target", "100")
        self.assertEqual(len(self.cm.select("100")), 2)
        self.assertEqual(len(self.cm.select("200")), 1)
        first = [item["id"] for item in self.cm.select("100")]
        self.cm.set_enabled("special", False)
        with self.assertRaisesRegex(ValueError, "cm_selection_stale"):
            self.cm.advance_after_delivery("100", "op", first, verified=True)

    def test_keyboard_preserves_existing_rows(self):
        self.register(2)
        original = [[{"text": "応募する", "callback_data": "join:1"}]]
        combined = append_cm_buttons(original, self.cm.select("100"))
        self.assertEqual(combined[0], original[0])
        self.assertEqual(original, [[{"text": "応募する", "callback_data": "join:1"}]])
        self.assertEqual(combined[1][0]["text"], "PR / 案内0")
        self.assertEqual(combined[1][0]["url"], "https://example.org/item/0")
        self.assertEqual(len(combined), 3)

    def test_invalid_links_and_duplicate_operation_rejected(self):
        for url in ("http://example.org", "https://user:pass@example.org",
                    "https://example.org:444/", "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "cm_url_invalid"):
                self.cm.add("invalid", "無効", url)
        self.register()
        first = [item["id"] for item in self.cm.select("100")]
        self.cm.advance_after_delivery("100", "shared-id", first, verified=True)
        with self.assertRaisesRegex(ValueError, "cm_operation_conflict"):
            self.cm.advance_after_delivery("200", "shared-id", first, verified=True)
        with self.assertRaisesRegex(ValueError, "cm_selection_invalid"):
            self.cm.advance_after_delivery("100", "next-id", ["c1", "c1"], verified=True)


if __name__ == "__main__":
    unittest.main()
