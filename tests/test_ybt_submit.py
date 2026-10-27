from __future__ import annotations

import importlib.util
import io
import http.server
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "ybt_submit.py"
SPEC = importlib.util.spec_from_file_location("ybt_submit", MODULE_PATH)
assert SPEC and SPEC.loader
ybt = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ybt
SPEC.loader.exec_module(ybt)


class ParserTests(unittest.TestCase):
    def test_parse_submit_form(self) -> None:
        page = """
        <form action="submit.php?pid=1000" method="post">
          <input type="hidden" name="pid" value="999">
          <select name="lang">
            <option value="1">G++</option>
            <option value="5">Python</option>
          </select>
          <textarea name="code">old</textarea>
          <input type="submit" name="submit" value="提交">
        </form>
        """
        form = ybt.find_submit_form(page)
        data, language = ybt.prepare_form_data(form, 1000, "int main() {}", "cpp")
        values = dict(data)
        self.assertEqual(values["pid"], "1000")
        self.assertEqual(values["lang"], "1")
        self.assertEqual(values["code"], "int main() {}")
        self.assertEqual(language, "1")

    def test_login_form_is_not_a_submit_form(self) -> None:
        page = """
        <form action="login.php" method="post">
          <input name="username">
          <input type="password" name="password">
        </form>
        """
        with self.assertRaises(ybt.LoginError):
            ybt.find_submit_form(page)

    def test_code_textarea_can_have_a_different_name(self) -> None:
        page = """
        <form action="submit.php" method="post">
          <input type="hidden" name="problem_id" value="0">
          <select name="language"><option value="2">GCC</option></select>
          <textarea name="source_code"></textarea>
        </form>
        """
        form = ybt.find_submit_form(page)
        data, _ = ybt.prepare_form_data(form, 1234, "int main(){}", "c")
        values = dict(data)
        self.assertEqual(values["problem_id"], "1234")
        self.assertEqual(values["language"], "2")
        self.assertEqual(values["source_code"], "int main(){}")

    def test_current_action_php_form(self) -> None:
        page = """
        <form action="action.php" method="post">
          <input name="user_id" value="ExampleUser" readonly>
          <input name="problem_id" value="1000" readonly>
          <select name="language">
            <option value="7" selected>G++14</option>
            <option value="5">Python</option>
          </select>
          <input type="checkbox" name="nbny" value="1">
          <textarea name="source"></textarea>
          <input type="submit" value="提交" name="submit">
          <input type="reset" value="清空" name="reset">
        </form>
        """
        form = ybt.find_submit_form(page)
        data, language = ybt.prepare_form_data(form, 1000, "print(5)", "python")
        values = dict(data)
        self.assertEqual(form.action, "action.php")
        self.assertEqual(values["problem_id"], "1000")
        self.assertEqual(values["language"], "5")
        self.assertEqual(values["source"], "print(5)")
        self.assertNotIn("nbny", values)
        self.assertNotIn("reset", values)
        self.assertEqual(language, "5")

    def test_status_record_parser(self) -> None:
        page = """
        <script>
        var ee="Uid932583:Uid932583`043342686`1000`Accepted|30:AC|596_3,`7`110`2026-10-26 14:41:01#Uid932583:Uid932583`043342685`1000`Unaccepted|0:WA|596_3,`7`109`2026-10-26 14:39:51#".split("#");
        </script>
        """
        records = ybt.parse_status_records(page)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].run_id, 43342686)
        self.assertEqual(records[0].user, "Uid932583")
        self.assertTrue(records[0].result.startswith("Accepted"))

    def test_verdict_parser(self) -> None:
        result, parts = ybt.parse_verdict_response(
            "Uid932583:1000:43342686:110:Accepted|30:AC|596_3"
        )
        self.assertEqual(result, "Accepted|30:AC|596_3")
        self.assertEqual(parts[2], "43342686")

    def test_display_name_matches_status_uid(self) -> None:
        self.assertTrue(
            ybt.verdict_user_matches("ExampleUser", "Uid929575")
        )
        self.assertFalse(
            ybt.verdict_user_matches("Uid929575", "Uid929573")
        )

    def test_language_helpers(self) -> None:
        self.assertEqual(ybt.infer_language(Path("answer.cpp")), "cpp")
        self.assertEqual(ybt.infer_language(Path("answer.py")), "python")
        self.assertEqual(ybt.resolve_language("G++"), "cpp")
        self.assertEqual(ybt.resolve_language("5"), "python")
        self.assertEqual(
            ybt.choose_language("c", [ybt.Option("c", "C")]),
            "c",
        )
        self.assertEqual(
            ybt.choose_language(
                "c",
                [ybt.Option("7", "G++14"), ybt.Option("2", "GCC")],
            ),
            "7",
        )

    def test_logged_user_parser(self) -> None:
        self.assertEqual(ybt.extract_logged_user('var user="Uid932583";'), "Uid932583")
        self.assertEqual(ybt.extract_logged_user('var user="";'), "")

    def test_rate_limit_marker(self) -> None:
        with self.assertRaises(ybt.RateLimitError):
            ybt.raise_for_submit_error("<script>alert('提交过于频繁，40秒后重试');</script>")
        with self.assertRaises(ybt.RateLimitError):
            ybt.raise_for_submit_error("Too Fast!")

    def test_action_php_error_markers(self) -> None:
        with self.assertRaisesRegex(ybt.SubmitError, "Invalid User"):
            ybt.raise_for_submit_error("Invalid User.")
        with self.assertRaisesRegex(ybt.SubmitError, "No Such Problem"):
            ybt.raise_for_submit_error("No Such Problem.")

class RateLimiterTests(unittest.TestCase):
    def test_interval_is_persisted_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            now = [1000.0]
            sleeps: list[float] = []

            first = ybt.SubmitRateLimiter(
                state_path,
                "http://example.test/",
                "alice",
                interval=40.0,
                safety_margin=1.0,
                clock=lambda: now[0],
                sleeper=sleeps.append,
            )
            first.record()

            now[0] = 1010.0
            second = ybt.SubmitRateLimiter(
                state_path,
                "http://example.test/",
                "alice",
                interval=40.0,
                safety_margin=1.0,
                clock=lambda: now[0],
                sleeper=sleeps.append,
            )
            second.wait()
            self.assertEqual(len(sleeps), 1)
            self.assertAlmostEqual(sleeps[0], 31.0)

            now[0] = 1041.0
            second.wait()
            self.assertEqual(len(sleeps), 1)

    def test_memory_limit_survives_state_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blocked_parent = Path(directory) / "blocked"
            blocked_parent.write_text("not a directory", encoding="utf-8")
            state_path = blocked_parent / "state.json"
            now = [1000.0]
            sleeps: list[float] = []
            limiter = ybt.SubmitRateLimiter(
                state_path,
                "http://example.test/",
                "alice",
                interval=40.0,
                safety_margin=1.0,
                clock=lambda: now[0],
                sleeper=sleeps.append,
            )
            limiter.record()
            now[0] = 1020.0
            limiter.wait()
            self.assertEqual(len(sleeps), 1)
            self.assertAlmostEqual(sleeps[0], 21.0)


class MockYbtHandler(http.server.BaseHTTPRequestHandler):
    last_submission: dict[str, str] | None = None
    run_id = 43342686
    rate_limit_next_submit = False
    empty_next_submit = False
    submit_attempts = 0
    timing_calls = 0
    navigation_paths: list[str] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def _authenticated(self) -> bool:
        return "auth=1" in self.headers.get("Cookie", "")

    def _timing_initialized(self) -> bool:
        return "timing=1" in self.headers.get("Cookie", "")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/submit_time.php":
            type(self).timing_calls += 1
            self._send(
                "no",
                headers={"Set-Cookie": "timing=1; Path=/"},
            )
            return
        if parsed.path == "/index.php":
            user = "alice" if self._authenticated() else ""
            self._send(f'<script>var user="{user}";</script>')
            return
        if parsed.path in {
            "/problem_list.php",
            "/problem_search2.php",
            "/problem_show.php",
        }:
            type(self).navigation_paths.append(parsed.path)
            self._send("ok")
            return
        if parsed.path == "/submit.php":
            if not self._authenticated():
                self._send(
                    '<p>你还没有登录</p><form action="login.php" method="post"></form>'
                )
                return
            self._send(
                """
                <form action="action.php" method="post">
                  <input name="user_id" value="alice" readonly>
                  <input name="problem_id" value="0" readonly>
                  <select name="language">
                    <option value="1">G++</option>
                    <option value="5">Python</option>
                  </select>
                  <input type="checkbox" name="nbny" value="1">
                  <textarea name="source"></textarea>
                  <input type="submit" name="submit" value="提交">
                </form>
                """
            )
            return
        if parsed.path == "/status.php":
            if self.last_submission is None:
                records = ""
            else:
                records = (
                    "alice:alice`043342686`1000`C`1`42`2026-10-26 15:00:00#"
                )
            self._send(f'var ee="{records}".split("#");')
            return
        if parsed.path == "/statusx1.php":
            self._send(
                f"alice:1000:{self.run_id}:42:Accepted|30:AC|596_3,"
            )
            return
        self._send("not found", status=404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        values = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
        if self.path == "/login.php":
            if values.get("username") == "alice" and values.get("password") == "secret":
                self._send(
                    "ok",
                    status=302,
                    headers={"Location": "/index.php", "Set-Cookie": "auth=1; Path=/"},
                )
            else:
                self._send("<script>alert('密码错误');</script>")
            return
        if (
            self.path == "/action.php"
            and self._authenticated()
            and self._timing_initialized()
        ):
            type(self).submit_attempts += 1
            if type(self).rate_limit_next_submit and type(self).submit_attempts == 1:
                self._send("Too Fast!")
                return
            if type(self).empty_next_submit:
                self._send("\ufeff")
                return
            type(self).last_submission = values
            self._send("Submitted.")
            return
        self._send("not found", status=404)


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        MockYbtHandler.last_submission = None
        MockYbtHandler.rate_limit_next_submit = False
        MockYbtHandler.empty_next_submit = False
        MockYbtHandler.submit_attempts = 0
        MockYbtHandler.timing_calls = 0
        MockYbtHandler.navigation_paths = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockYbtHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}/"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_login_submit_and_poll(self) -> None:
        client = ybt.HttpClient(self.base_url)
        session_user = ybt.login(client, "alice", "secret")
        self.assertEqual(session_user, "alice")

        with redirect_stdout(io.StringIO()):
            code = ybt.submit_once(
                client=client,
                problem_id=1000,
                source="int main() { return 0; }",
                language_key="cpp",
                dry_run=False,
                assume_yes=True,
                poll_timeout=2.0,
                poll_interval=0.01,
                session_user=session_user,
            )

        self.assertEqual(code, 0)
        self.assertEqual(MockYbtHandler.timing_calls, 1)
        self.assertEqual(
            MockYbtHandler.navigation_paths,
            [
                "/problem_list.php",
                "/problem_search2.php",
                "/problem_show.php",
            ],
        )
        self.assertIsNotNone(MockYbtHandler.last_submission)
        self.assertEqual(MockYbtHandler.last_submission["problem_id"], "1000")
        self.assertEqual(MockYbtHandler.last_submission["language"], "1")
        self.assertEqual(
            MockYbtHandler.last_submission["source"], "int main() { return 0; }"
        )
        self.assertNotIn("nbny", MockYbtHandler.last_submission)

    def test_wrong_password(self) -> None:
        client = ybt.HttpClient(self.base_url)
        with self.assertRaisesRegex(ybt.LoginError, "密码错误"):
            ybt.login(client, "alice", "wrong")

    def test_rate_limit_retries_once(self) -> None:
        MockYbtHandler.rate_limit_next_submit = True
        client = ybt.HttpClient(self.base_url)
        session_user = ybt.login(client, "alice", "secret")
        with tempfile.TemporaryDirectory() as directory:
            limiter = ybt.SubmitRateLimiter(
                Path(directory) / "state.json",
                self.base_url,
                session_user,
                interval=0.0,
                safety_margin=0.0,
                sleeper=lambda _: None,
            )
            with redirect_stdout(io.StringIO()):
                code = ybt.submit_once(
                    client=client,
                    problem_id=1000,
                    source="int main() { return 0; }",
                    language_key="cpp",
                    dry_run=False,
                    assume_yes=True,
                    poll_timeout=2.0,
                    poll_interval=0.01,
                    session_user=session_user,
                    rate_limiter=limiter,
                )
        self.assertEqual(code, 0)
        self.assertEqual(MockYbtHandler.submit_attempts, 2)

    def test_empty_action_response_is_reported(self) -> None:
        MockYbtHandler.empty_next_submit = True
        client = ybt.HttpClient(self.base_url)
        session_user = ybt.login(client, "alice", "secret")
        with tempfile.TemporaryDirectory() as directory:
            limiter = ybt.SubmitRateLimiter(
                Path(directory) / "state.json",
                self.base_url,
                session_user,
                interval=0.0,
                safety_margin=0.0,
                sleeper=lambda _: None,
            )
            with redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ybt.SubmitError, "empty response"):
                    ybt.submit_once(
                        client=client,
                        problem_id=1000,
                        source="int main() { return 0; }",
                        language_key="cpp",
                        dry_run=False,
                        assume_yes=True,
                        poll_timeout=2.0,
                        poll_interval=0.01,
                        session_user=session_user,
                        rate_limiter=limiter,
                    )


if __name__ == "__main__":
    unittest.main()
