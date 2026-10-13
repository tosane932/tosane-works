import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app as puoppo


class PuoppoSecurityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.old_db = puoppo.DB_NAME
        self.old_emails = puoppo.ALLOWED_EMAILS
        self.old_available = puoppo.app.config["GOOGLE_LOGIN_AVAILABLE"]
        puoppo.DB_NAME = os.path.join(self.folder.name, "puoppo.db")
        puoppo.init_db()
        with puoppo.get_db_connection() as conn:
            conn.execute("INSERT INTO users (google_sub, email, name) VALUES ('sub', 'owner@example.com', 'Owner')")
            conn.execute("INSERT INTO history (keyword) VALUES ('旧利用者の検索語')")
        puoppo.ALLOWED_EMAILS = {"owner@example.com"}
        puoppo.app.config.update(TESTING=True, SECRET_KEY="test-secret", GOOGLE_LOGIN_AVAILABLE=True)
        self.client = puoppo.app.test_client()

    def tearDown(self):
        puoppo.DB_NAME = self.old_db
        puoppo.ALLOWED_EMAILS = self.old_emails
        puoppo.app.config["GOOGLE_LOGIN_AVAILABLE"] = self.old_available
        self.folder.cleanup()

    def login(self):
        with self.client.session_transaction() as data:
            data["_user_id"] = "1"
            data["_fresh"] = True

    def test_shared_history_is_not_displayed_or_deleted(self):
        response = self.client.get("/?keyword=%E5%B0%8F%E9%BA%A6")
        self.assertEqual(response.status_code, 200)
        self.assertIn("小麦", response.get_data(as_text=True))
        self.assertNotIn("旧利用者の検索語", response.get_data(as_text=True))
        self.assertEqual(self.client.post("/delete", data={"id": "1"}).status_code, 404)
        with puoppo.get_db_connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM history").fetchone()[0], 1)

    def test_anonymous_and_invalid_requests_never_call_gemini(self):
        with patch.object(puoppo, "client") as gemini:
            self.assertEqual(self.client.post("/search", data={"keyword": "パン"}).status_code, 403)
            self.login()
            self.assertEqual(self.client.post("/search", data={"keyword": "パン"}).status_code, 400)
            token = self.client.get("/").get_data(as_text=True)
            self.assertIn('name="csrf_token"', token)
            with self.client.session_transaction() as data:
                csrf = data["csrf_token"]
            self.assertEqual(self.client.post("/search", data={"keyword": "あ" * 101, "csrf_token": csrf}).status_code, 400)
            gemini.models.generate_content.assert_not_called()

    def test_daily_and_cooldown_limit_is_atomic(self):
        self.assertTrue(puoppo.reserve_analysis_slot(1))
        self.assertFalse(puoppo.reserve_analysis_slot(1))
        with puoppo.get_db_connection() as conn:
            day = dt.datetime.now(dt.timezone.utc).date().isoformat()
            self.assertEqual(conn.execute("SELECT count FROM analysis_usage WHERE user_id=1 AND day=?", (day,)).fetchone()[0], 1)

    def test_generated_html_is_escaped_and_get_does_not_analyze(self):
        self.login()
        with patch.object(puoppo, "client") as gemini:
            self.assertEqual(self.client.get("/?keyword=%E3%83%91%E3%83%B3").status_code, 200)
            gemini.models.generate_content.assert_not_called()
            with self.client.session_transaction() as data:
                csrf = data["csrf_token"]
            xml = b"<rss><channel><item><title>Bread news</title></item></channel></rss>"

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    return False

                def read(self, size):
                    return xml

            gemini.models.generate_content.return_value = SimpleNamespace(text="<script>alert(1)</script>")
            with patch.object(puoppo.urllib.request, "urlopen", return_value=Response()):
                result = self.client.post("/search", data={"keyword": "パン", "csrf_token": csrf})
            self.assertEqual(result.status_code, 200)
            self.assertIn("&lt;script&gt;", result.get_data(as_text=True))
            self.assertNotIn("<script>alert(1)</script>", result.get_data(as_text=True))
            gemini.models.generate_content.assert_called_once()


if __name__ == "__main__":
    unittest.main()
