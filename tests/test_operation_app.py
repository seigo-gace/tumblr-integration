"""Static safety contract for the TGtumbler Tumblr operation center."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / "app"

class OperationAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html=(ROOT/"index.html").read_text(encoding="utf-8")
        cls.js=(ROOT/"app.js").read_text(encoding="utf-8")
        cls.css=(ROOT/"styles.css").read_text(encoding="utf-8")
    def test_entry_elements_exist(self):
        for field in ('id="messages"','id="chat-form"','id="instruction"','id="connection"','id="revision"','id="assist"','__TUMBLR_COMMIT__'):
            self.assertIn(field,self.html)
        self.assertNotIn('id="modules"',self.html)
    def test_send_does_not_require_a_module(self):
        self.assertNotIn("操作分野を選んでください",self.js)
        self.assertIn('fetch("/assistant"',self.js)
    def test_catalog_reuses_all_nine_module_names(self):
        from tumblrkit.assistant import MODULES
        for name in ("account","discovery","library","media","engagement","publishing","campaigns","referrals","analytics"):
            self.assertIn(name,MODULES)
    def test_no_external_requests_or_local_credentials(self):
        for operation in ("XMLHttpRequest", "localStorage", "sessionStorage", "document.cookie", "innerHTML", "eval(", "https://", "http://"):
            self.assertNotIn(operation,self.js)
    def test_safe_text_rendering(self):
        self.assertIn("item.textContent=text",self.js)
        self.assertIn('event.preventDefault()',self.js)
    def test_six_canonical_goal_buttons_are_visible_and_multiselect(self):
        import re
        labels=re.findall(r'data-goal="([^"]+)"',self.html)
        self.assertEqual(labels,["巡回","投稿","キャンペーン","紹介","集計","メディア"])
        self.assertEqual(self.html.count('aria-pressed="false"'),6)
        self.assertIn('id="goal-selection"',self.html)
        self.assertIn('id="clear-goals"',self.html)
        self.assertIn('new Set()',self.js)
        self.assertIn('selectedGoals.add(goal)',self.js)
        self.assertIn('selectedGoals.delete(goal)',self.js)
        self.assertIn('instruction.required=selectedGoals.size===0',self.js)
        self.assertIn('const message=goals.length',self.js)
        self.assertIn('.goal[aria-pressed="true"]',self.css)

    def test_mobile_layout(self):
        self.assertIn("@media(max-width:760px)",self.css)
        self.assertIn('name="viewport"',self.html)

if __name__=="__main__":
    unittest.main()
