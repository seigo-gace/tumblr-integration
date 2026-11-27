"""Regression contract for independent authenticated conversation follow-ups.

Only loopback HTTP and a recording local operation adapter are used.
The tests deliberately exercise the existing /assistant request_id contract.
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_app_serve import APP, SHA_A, TOKEN, fetch
from tumblrkit.appserve import AppServer, build_release
from tumblrkit.assistant import Assistant, ScriptedProvider


class ConversationFollowupTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        release = build_release(APP, Path(work.name), SHA_A)
        self.calls = []
        assistant = Assistant(providers=[])
        self.server = AppServer(release, TOKEN, assistant=assistant)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        assistant.catalog_perform = self.record
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.a = self.login()
        self.b = self.login()
        self.assertNotEqual(self.a, self.b)

    def record(self, action, payload):
        self.calls.append((action, dict(payload)))
        return {"verified": True, "summary": "mock result", "published": False}

    def login(self):
        status, _, headers = fetch(
            self.base + "/session", method="POST",
            data=b"token=" + TOKEN.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 204)
        return headers["Set-Cookie"].split(";", 1)[0]

    def turn(self, cookie, **payload):
        status, body, _ = fetch(
            self.base + "/assistant", method="POST",
            data=json.dumps(payload).encode(),
            headers={"Cookie": cookie, "Content-Type": "application/json",
                     "Origin": self.base},
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def ask(self, cookie, message):
        result = self.turn(cookie, message=message)
        self.assertIn(result["state"], ("guidance", "input_required"))
        self.assertFalse(result["executed"])
        self.assertTrue(result.get("request_id"), result)
        return result["request_id"]

    def confirmation(self, cookie, campaign):
        result = self.turn(cookie, message="当選者へ通知\n企画ID: " + campaign
                           + "\n通知ID: event-1")
        self.assertEqual(result["state"], "confirmation_required", result)
        return result["request_id"]

    def test_interleaved_inputs_remain_in_their_own_sessions(self):
        a = self.ask(self.a, "投稿文をゼロから作る")
        b = self.ask(self.b, "投稿文をゼロから作る")
        self.assertNotEqual(a, b)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.b, message="Bの本文", request_id=b)["state"],
                         "completed")
        self.assertEqual(self.turn(self.a, message="Aの本文", request_id=a)["state"],
                         "completed")
        self.assertEqual(self.calls, [
            ("publisher.01", {"text": "Bの本文"}),
            ("publisher.01", {"text": "Aの本文"}),
        ])

    def test_foreign_input_id_cannot_complete_or_consume_owner_input(self):
        a = self.ask(self.a, "投稿文をゼロから作る")
        result = self.turn(self.b, message="別セッションの本文", request_id=a)
        self.assertNotEqual(result["state"], "completed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.a, message="本人の本文", request_id=a)["state"],
                         "completed")
        self.assertEqual(self.calls, [("publisher.01", {"text": "本人の本文"})])

    def test_foreign_confirmation_cannot_execute_or_consume_owner_request(self):
        a = self.confirmation(self.a, "campaign-a")
        result = self.turn(self.b, confirm=True, request_id=a)
        self.assertNotEqual(result["state"], "completed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.a, confirm=True, request_id=a)["state"],
                         "completed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "campaign.08")
        self.assertEqual(self.calls[0][1]["campaign_id"], "campaign-a")
        self.turn(self.a, confirm=True, request_id=a)
        self.assertEqual(len(self.calls), 1)

    def test_abort_is_scoped_to_own_conversation(self):
        a = self.ask(self.a, "投稿文をゼロから作る")
        b = self.confirmation(self.b, "campaign-b")
        self.turn(self.a, abort=True, request_id=a)
        result = self.turn(self.a, message="中断済み本文", request_id=a)
        self.assertNotEqual(result["state"], "completed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.b, confirm=True, request_id=b)["state"],
                         "completed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["campaign_id"], "campaign-b")

    def test_foreign_abort_does_not_clear_owner_confirmation(self):
        a = self.confirmation(self.a, "campaign-a")
        self.turn(self.b, abort=True, request_id=a)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.a, confirm=True, request_id=a)["state"],
                         "completed")
        self.assertEqual(len(self.calls), 1)


    def test_same_cookie_interleaved_operation_ids_keep_separate_inputs(self):
        a = self.ask(self.a, "当選者へ通知\n企画ID: campaign-tab-a")
        b = self.ask(self.a, "当選者へ通知\n企画ID: campaign-tab-b")
        self.assertNotEqual(a, b)
        self.assertEqual(self.calls, [])
        second = self.turn(self.a, message="event-tab-b", request_id=b)
        self.assertEqual(second["state"], "confirmation_required", second)
        first = self.turn(self.a, message="event-tab-a", request_id=a)
        self.assertEqual(first["state"], "confirmation_required", first)
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.a, confirm=True,
                                  request_id=first["request_id"])["state"], "completed")
        self.assertEqual(self.turn(self.a, confirm=True,
                                  request_id=second["request_id"])["state"], "completed")
        self.assertEqual(self.calls, [
            ("campaign.08", {"campaign_id": "campaign-tab-a", "event_id": "event-tab-a"}),
            ("campaign.08", {"campaign_id": "campaign-tab-b", "event_id": "event-tab-b"}),
        ])

    def test_premature_confirmation_preserves_missing_input(self):
        operation = self.ask(self.a, "当選者へ通知\n企画ID: campaign-incomplete")
        refused = self.turn(self.a, confirm=True, request_id=operation)
        self.assertNotIn(refused["state"], ("completed", "confirmation_required"))
        self.assertFalse(refused["executed"])
        self.assertEqual(self.calls, [])
        proposed = self.turn(self.a, message="event-ready", request_id=operation)
        self.assertEqual(proposed["state"], "confirmation_required", proposed)
        self.assertFalse(proposed["executed"])
        self.assertEqual(self.calls, [])
        completed = self.turn(self.a, confirm=True, request_id=proposed["request_id"])
        self.assertEqual(completed["state"], "completed", completed)
        self.assertEqual(self.calls, [
            ("campaign.08", {"campaign_id": "campaign-incomplete", "event_id": "event-ready"}),
        ])

    def test_client_claimed_session_id_cannot_override_authentication(self):
        claimed = "same-client-supplied-session"
        a = self.turn(self.a, message="投稿文をゼロから作る", session_id=claimed)
        b = self.turn(self.b, message="投稿文をゼロから作る", session_id=claimed)
        for result in (a, b):
            self.assertEqual(result["state"], "input_required", result)
            self.assertFalse(result["executed"])
            self.assertTrue(result.get("request_id"))
        self.assertNotEqual(a["request_id"], b["request_id"])
        refused = self.turn(self.b, message="混入させない本文",
                            request_id=a["request_id"], session_id=claimed)
        self.assertNotEqual(refused["state"], "completed")
        self.assertFalse(refused["executed"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.b, message="Bの正しい本文",
                                  request_id=b["request_id"], session_id=claimed)["state"],
                         "completed")
        self.assertEqual(self.turn(self.a, message="Aの正しい本文",
                                  request_id=a["request_id"], session_id=claimed)["state"],
                         "completed")
        self.assertEqual(self.calls, [
            ("publisher.01", {"text": "Bの正しい本文"}),
            ("publisher.01", {"text": "Aの正しい本文"}),
        ])

    def test_bearer_authentication_supports_input_followup(self):
        def bearer_turn(**payload):
            status, body, _ = fetch(
                self.base + "/assistant", token=TOKEN, method="POST",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 200, body)
            return json.loads(body)

        asked = bearer_turn(message="投稿文をゼロから作る")
        self.assertEqual(asked["state"], "input_required", asked)
        self.assertTrue(asked.get("request_id"))
        self.assertFalse(asked["executed"])
        self.assertEqual(self.calls, [])
        completed = bearer_turn(message="Bearerの本文", request_id=asked["request_id"])
        self.assertEqual(completed["state"], "completed", completed)
        self.assertEqual(self.calls, [("publisher.01", {"text": "Bearerの本文"})])

    def test_ai_selected_operation_continues_missing_input_and_goals_over_http(self):
        provider = ScriptedProvider("openrouter", payload={
            "action": "publisher.01", "target": "", "fields": {},
            "question": "本文はどうしますか？", "reply": "",
        }, paid=False)
        self.server.assistant.providers = [provider]
        request = "次に載せる文章を用意したいです"
        goals = ["投稿", "集計"]
        asked = self.turn(self.a, message=request, goals=goals)
        self.assertEqual(asked["state"], "input_required", asked)
        self.assertEqual(asked["action"], "publisher.01")
        self.assertIn("本文", asked["text"])
        self.assertTrue(asked.get("request_id"))
        self.assertFalse(asked["executed"])
        self.assertEqual(asked["plan"]["goals"], goals)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(provider.prompts), 1)
        self.assertIn(request, provider.prompts[0])
        for goal in goals:
            self.assertIn(goal, provider.prompts[0])
        self.assertNotIn(TOKEN, provider.prompts[0])
        self.assertNotIn(self.a.split("=", 1)[1], provider.prompts[0])

        body = "今日は新しい一日を楽しみましょう。"
        completed = self.turn(self.a, message=body, request_id=asked["request_id"])
        self.assertEqual(completed["state"], "completed", completed)
        self.assertEqual(completed["plan"]["goals"], goals)
        self.assertEqual(self.calls, [("publisher.01", {"text": body})])
        self.assertEqual(len(provider.prompts), 1)
        self.assertFalse(completed["executed"])

        replay = self.turn(self.a, message=body, request_id=asked["request_id"])
        self.assertNotEqual(replay["state"], "completed")
        self.assertEqual(self.calls, [("publisher.01", {"text": body})])


class BrowserRetryTests(unittest.TestCase):
    def test_actual_browser_send_and_retry_handlers(self):
        import subprocess

        # Execute the production send/response/form/button block, not a
        # reimplementation of its retry rules. Only DOM and fetch are mocked.
        script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const start = source.indexOf("  function showAssist(");
const end = source.indexOf("  // Real app-owned drafts", start);
assert.ok(start >= 0 && end > start, "production send block missing");
function harness() {
  const calls = [], notices = [], replies = [];
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      handlers: {}, hidden: false, value: "", disabled: false,
      addEventListener(name, fn) { this.handlers[name] = fn; }
    });
    return nodes.get(id);
  }
  const context = vm.createContext({
    document: {getElementById: node},
    form: node("form"), confirm: node("confirm"), assist: node("assist"),
    connection: node("connection"), instruction: node("instruction"),
    sendButton: node("send"), selectedGoals: new Set(["投稿"]),
    pendingFiles: [], display: text => notices.push(text),
    renderView() {}, renderPlan() {}, resetDraftEdit() {},
    refreshAttachmentList() {}, displayAttachments() {},
    uploadAttachment() { throw Error("unexpected attachment"); },
    fetch(url, options) {
      assert.equal(url, "/assistant");
      assert.equal(options.method, "POST");
      calls.push(JSON.parse(options.body));
      assert.ok(replies.length, "unexpected fetch");
      const reply = replies.shift();
      if (reply === null) return Promise.reject(Error("lost response"));
      return Promise.resolve({ok: true, json: async () => reply});
    }
  });
  const names = ["sending", "lastText", "pending", "pendingState",
                 "assistantBusy", "lastRequest"];
  const declarations = names.map(name => {
    const line = source.split("\n").find(line => line.trimStart().startsWith("let " + name + "="));
    assert.ok(line, "missing production state: " + name);
    return line;
  }).join("\n");
  vm.runInContext(declarations + "\n" + source.slice(start, end), context);
  async function click(id, event = "click") {
    await node(id).handlers[event]({preventDefault() {}});
    await new Promise(resolve => setImmediate(resolve));
  }
  async function send(message, reply) {
    replies.push(reply);
    node("instruction").value = message;
    await click("form", "submit");
  }
  const waiting = id => ({state: "input_required", request_id: id,
                          text: "本文を書いてください", recovery: true});
  const done = {state: "completed", text: "完了", executed: false};
  return {calls, notices, replies, node, context, click, send, waiting, done};
}
(async () => {
  {
    const h = harness();
    await h.send("本文付きの初回依頼", null);
    await h.click("retry");
    assert.equal(h.calls.length, 1);
    assert.ok(!("request_id" in h.calls[0]));
    assert.ok(h.notices.some(text => text.includes("再送")));
    assert.equal(h.node("connection").textContent, "外部接続：結果未確認");
  }
  {
    const h = harness();
    await h.send("依頼", h.waiting("id-a"));
    await h.send("回答", null);
    assert.deepEqual(h.calls[1], {message:"回答", goals:["投稿"],
                                  attachments:[], request_id:"id-a"});
    h.replies.push(h.done);
    await h.click("retry");
    assert.equal(h.calls.length, 3);
    assert.deepEqual(h.calls[2], h.calls[1]);
    await h.click("retry");
    assert.equal(h.calls.length, 3);
  }
  {
    const h = harness();
    await h.send("依頼", h.waiting("id-a"));
    await h.send("回答", null);
    // Inject a new server response using the real response handler:
    // an old failed payload must not become a reply to a newer operation.
    h.context.apply(h.waiting("id-b"));
    await h.click("retry");
    assert.equal(h.calls.length, 2);
    assert.ok(h.notices.some(text => text.includes("再送")));
  }
  for (const id of ["confirm", "abort", "draft"]) {
    const h = harness();
    const reply = id === "confirm"
      ? {state:"confirmation_required", request_id:"id-c", recovery:true}
      : h.waiting("id-a");
    await h.send("依頼", reply);
    h.replies.push(null);
    await h.click(id);
    assert.equal(h.calls.length, 2, id);
    assert.equal(h.calls[1][id], true, id);
    if (id !== "draft") assert.equal(h.calls[1].request_id, reply.request_id);
    await h.click("retry");
    assert.equal(h.calls.length, 2, id);
  }
  {
    const h = harness();
    await h.send("依頼", h.waiting("id-a"));
    await h.send("回答", h.waiting("id-b"));
    await h.click("retry");
    assert.equal(h.calls.length, 2);
    await h.send("次の回答", h.done);
    assert.equal(h.calls[2].request_id, "id-b");
    await h.click("retry");
    assert.equal(h.calls.length, 3);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", script, str(APP / "app.js")],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
