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
        self.assertIn('const goals=Array.from(selectedGoals)',self.js)
        self.assertIn('goals:goals,attachments:',self.js)
        self.assertIn('renderPlan(turn.plan)',self.js)
        self.assertIn('selected_action:item.action,selected_target:target',self.js)
        self.assertIn('plan.recommendations.slice(visibleStart,visibleStart+3)',self.js)
        self.assertIn('if(visibleStart+3<plan.recommendations.length)',self.js)
        self.assertIn('.goal[aria-pressed="true"]',self.css)
        self.assertLess(self.html.index('id="messages"'),self.html.index('id="goal-grid"'))
        self.assertLess(self.html.index('id="chat-form"'),self.html.index('id="goal-grid"'))
        self.assertLess(self.html.index('id="draft-list"'),self.html.index('id="goal-grid"'))
        self.assertIn('grid-template-columns:repeat(3,minmax(0,1fr))',self.css)
        self.assertNotIn('min-height:81px',self.css)
        self.assertNotIn('フォロー・タグ・公開候補を確認',self.html)
        self.assertEqual(self.html.count('class="goal"'),6)

    def test_media_save_history_uses_real_total_and_browsable_page_controls(self):
        self.assertIn('"/media/jobs?offset="+offset+"&limit=100"',self.js)
        self.assertIn('"次の100件"',self.js)
        self.assertIn('"前の100件"',self.js)
        self.assertIn('!data.has_next',self.js)
        self.assertNotIn('MP4保存履歴（最新100件）',self.js)

    def test_bulk_library_gallery_is_paginated_without_silent_200_limit(self):
        self.assertNotIn('found>=200',self.js)
        self.assertIn('references.slice(offset,offset+50)',self.js)
        self.assertIn('position.textContent=',self.js)
        self.assertIn('"/library/selected",{candidate_ids:batch}',self.js)
        self.assertIn('selectedCount.textContent=',self.js)
        self.assertIn('libraryOffset+=50;showLibrary()',self.js)
        self.assertNotIn('libraryOffset+=50;librarySelection.clear()',self.js)

    def test_owner_post_list_has_genuine_next_previous_page_controls(self):
        self.assertIn('view.action==="tumblr.list_posts"',self.js)
        self.assertIn('"next_target","次の投稿20件"',self.js)
        self.assertIn('"prev_target","前の投稿20件"',self.js)
        self.assertIn('selected_action:"tumblr.list_posts",selected_target:target',self.js)
        self.assertIn('view.total_verified===true',self.js)

    def test_discovery_saved_pages_never_hide_rows_beyond_first_hundred(self):
        self.assertIn('view.stored_total',self.js)
        self.assertIn('view.stored_offset',self.js)
        self.assertIn('"stored_next_target","保存済みの次100件"',self.js)
        self.assertIn('"stored_prev_target","保存済みの前100件"',self.js)
        self.assertIn('selected_action:"tumblr.discover",selected_target:target',self.js)

    def test_connected_read_only_tumblr_exposes_write_reauthorization_button(self):
        self.assertIn('if(state.can_publish!==true)',self.js)
        self.assertIn('connect.textContent="投稿権限を追加"',self.js)
        self.assertIn('connect.textContent="Tumblr再連携"',self.js)
        self.assertIn('fetch("/oauth/start"',self.js)
        self.assertIn('url.hostname!=="www.tumblr.com"',self.js)

    def test_chat_first_without_oversized_metadata_or_repeated_explanations(self):
        self.assertNotIn('class="eyebrow"', self.html)
        self.assertNotIn('TGtumbler / Tumblr</span>', self.html)
        self.assertNotIn('Tumblr 操作センター</h1>', self.html)
        self.assertNotIn('分野のボタンを選ばなくても送信できます。', self.html)
        self.assertNotIn('何をしたいかを書いてください。未接続', self.html)
        self.assertNotIn('class="footnote"', self.html)
        self.assertNotIn('class="draft-info"', self.html)
        self.assertIn('id="revision" data-commit="__TUMBLR_COMMIT__" hidden', self.html)
        self.assertIn('id="connection" class="visually-hidden"', self.html)
        self.assertIn('id="messages" class="messages" role="log" aria-live="polite"></div>', self.html)
        self.assertLess(self.html.index('id="selected-title"'), self.html.index('id="messages"'))
        self.assertLess(self.html.index('id="messages"'), self.html.index('id="chat-form"'))
        self.assertIn('min-height:100dvh', self.css)
        self.assertIn('.composer{display:flex', self.css)
        self.assertIn('.messages{flex:1', self.css)
        self.assertNotIn('radial-gradient', self.css)
        self.assertNotIn('min-height:570px', self.css)

    def test_verified_structured_result_renderer(self):
        self.assertIn("function renderView(view)", self.js)
        self.assertIn("renderView(turn.view)", self.js)
        self.assertIn('view.status!=="connected"', self.js)
        self.assertIn('document.createElement("table")', self.js)
        self.assertIn('document.createElement("a")', self.js)
        self.assertIn('link.rel="noopener noreferrer"', self.js)
        self.assertIn('url.protocol!=="https:"', self.js)
        self.assertIn('view.action==="analytics.get_summary"', self.js)
        self.assertIn("Number.isFinite(item.value)", self.js)
        self.assertIn(".result-table", self.css)
        self.assertIn(".result-bar-track", self.css)
        self.assertIn('key==="thumbnail_url"', self.js)
        self.assertIn('host.endsWith(".media.tumblr.com")', self.js)
        self.assertIn('img.referrerPolicy="no-referrer"', self.js)
        self.assertIn(".result-thumbnail", self.css)

    def test_mobile_layout(self):
        self.assertIn("@media(max-width:760px)",self.css)
        self.assertIn('name="viewport"',self.html)

if __name__=="__main__":
    unittest.main()
