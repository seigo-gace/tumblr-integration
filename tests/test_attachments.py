"""Local photo/file chat upload tests: no external network or provider calls."""
import base64
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from tumblrkit.appserve import AppServer, build_release
from tumblrkit.assistant import Assistant
from tumblrkit.attachments import MAX_ATTACHMENT_BYTES

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "test-owner-private-token"


def request(port, method, path, payload=None, auth=True, origin=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
    if auth:
        headers["Authorization"] = "Bearer " + TOKEN
    if origin:
        headers["Origin"] = "https://tumblr.hentaigay.org"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        payload = json.dumps(payload).encode("utf-8")
    conn.request(method, path, body=payload, headers=headers)
    response = conn.getresponse()
    status = response.status
    data = response.read()
    headers = dict(response.getheaders())
    conn.close()
    return status, data, headers


class UploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.release = build_release(ROOT / "app", Path(temp.name), "a" * 40)
        self.server = AppServer(self.release, TOKEN, assistant=Assistant(providers=[]))
        self.server.__enter__()
        self.addCleanup(lambda: self.server.__exit__(None, None, None))

    def upload(self, name, binary, **kwargs):
        return request(self.server.port, "POST", "/attachments",
                       {"name": name, "data": base64.b64encode(binary).decode("ascii")}, **kwargs)

    def test_photo_upload_and_private_readback(self):
        photo = b"\x89PNG\r\n\x1a\n" + b"valid-test-bytes"
        status, raw, _ = self.upload("写真.png", photo)
        self.assertEqual(status, 201)
        record = json.loads(raw)
        self.assertEqual(record["media_type"], "image/png")
        self.assertEqual(record["name"], "写真.png")
        self.assertEqual(record["size"], len(photo))
        status, body, headers = request(self.server.port, "GET", "/attachments/" + record["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body, photo)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertTrue(headers["Content-Disposition"].startswith("inline"))
        status, _, _ = request(self.server.port, "GET", "/attachments/" + record["id"], auth=False)
        self.assertEqual(status, 401)
        status, raw, _ = request(
            self.server.port, "POST", "/assistant",
            {"message": "この写真を確認", "attachments": [record["id"]]},
        )
        self.assertEqual(status, 200)
        answer = json.loads(raw)
        self.assertEqual(answer["state"], "attachments_received")
        self.assertFalse(answer["executed"])
        self.assertIn("未接続", answer["text"])
        self.assertEqual(answer["attachments"][0]["id"], record["id"])

    def test_document_upload_and_download_only(self):
        status, raw, _ = self.upload("memo.txt", "テスト文書".encode("utf-8"))
        self.assertEqual(status, 201)
        ident = json.loads(raw)["id"]
        status, body, headers = request(self.server.port, "GET", "/attachments/" + ident)
        self.assertEqual(status, 200)
        self.assertEqual(body, "テスト文書".encode("utf-8"))
        self.assertIn("attachment;", headers["Content-Disposition"])
        status, raw, _ = request(self.server.port, "DELETE", "/attachments/" + ident)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["deleted"])
        status, _, _ = request(self.server.port, "GET", "/attachments/" + ident)
        self.assertEqual(status, 404)

    def test_office_zip_file_is_accepted_but_download_only(self):
        data = bytes.fromhex("504b0304") + b"mock-archive"
        status, raw, _ = self.upload("report.docx", data)
        self.assertEqual(status, 201)
        record = json.loads(raw)
        self.assertEqual(record["media_type"],
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        status, body, headers = request(self.server.port, "GET", "/attachments/" + record["id"])
        self.assertEqual(status, 200)
        self.assertEqual(body, data)
        self.assertTrue(headers["Content-Disposition"].startswith("attachment;"))

    def test_auth_origin_and_invalid_files_are_rejected(self):
        photo = b"GIF89a" + b"rest"
        self.assertEqual(self.upload("image.gif", photo, auth=False)[0], 401)
        self.assertEqual(self.upload("image.gif", photo, origin=False)[0], 201)
        self.assertEqual(self.upload("malware.exe", b"MZ\x00")[0], 400)
        self.assertEqual(self.upload("too-big.png", b"\x89PNG\r\n\x1a\n" + b"a" * MAX_ATTACHMENT_BYTES)[0], 400)
        self.assertEqual(self.upload("../", photo)[0], 400)
        status, _, _ = request(self.server.port, "POST", "/assistant",
                                {"message": "test", "attachments": ["f" * 32]})
        self.assertEqual(status, 400)

    def test_client_composer_contains_attachment_picker_and_upload_flow(self):
        html = (ROOT / "app" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "app" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "app" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('id="attachments" type="file"', html)
        self.assertIn('id="attachment-list"', html)
        self.assertIn('fetch("/attachments"', js)
        self.assertIn("pendingFiles.splice", js)
        self.assertIn('"/attachments/"+encodeURIComponent(record.id)', js)
        self.assertIn(".attach-button", css)


if __name__ == "__main__":
    unittest.main()
