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
        for field in ('id="modules"','id="messages"','id="chat-form"','id="instruction"','id="connection"','id="revision"','__TUMBLR_COMMIT__'):
            self.assertIn(field,self.html)
    def test_catalog_reuses_all_nine_module_names(self):
        for name in ("account","discovery","library","media","engagement","publishing","campaigns","referrals","analytics"):
            self.assertIn('"'+name+'"',self.js)
    def test_no_external_requests_or_local_credentials(self):
        for operation in ("fetch(", "XMLHttpRequest", "localStorage", "sessionStorage", "document.cookie", "innerHTML", "eval("):
            self.assertNotIn(operation,self.js)
    def test_safe_text_rendering(self):
        self.assertIn("item.textContent=text",self.js)
        self.assertIn('event.preventDefault()',self.js)
    def test_mobile_layout(self):
        self.assertIn("@media(max-width:760px)",self.css)
        self.assertIn('name="viewport"',self.html)

if __name__=="__main__":
    unittest.main()
