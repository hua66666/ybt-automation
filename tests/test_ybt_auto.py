from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import threading
import http.server
import unittest
from contextlib import redirect_stdout
from pathlib import Path


OUTPUT_DIR = Path(__file__).resolve().parents[1]
AUTO_PATH = OUTPUT_DIR / "ybt_auto.py"
SUBMIT_PATH = OUTPUT_DIR / "ybt_submit.py"

for name, path in (("ybt_submit", SUBMIT_PATH), ("ybt_auto", AUTO_PATH)):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

ybt_auto = sys.modules["ybt_auto"]


PROBLEM_HTML = """
<img src="main7.png">
<center><h3>1000：入门测试题目</h3></center>
<h3>【题目描述】</h3>
<script>pshow("求两个整数的和。\\n<img src=\\"pic/1000.png\\">");</script>
<h3>【输入】</h3>
<script>pshow("一行，两个用空格隔开的整数。");</script>
<h3>【输出】</h3>
<script>pshow("两个整数的和。");</script>
<h3>【输入样例】</h3>
<font size=3><pre>2 3</pre></font>
<h3>【输出样例】</h3>
<font size=3><pre>5</pre></font>
"""


class ProblemParserTests(unittest.TestCase):
    def test_parse_problem(self) -> None:
        problem = ybt_auto.parse_problem(
            PROBLEM_HTML, 1000, "http://ybt.ssoier.cn:8088/"
        )
        self.assertEqual(problem.title, "入门测试题目")
        self.assertEqual(problem.description, "求两个整数的和。")
        self.assertEqual(problem.sample_input, "2 3")
        self.assertEqual(problem.sample_output, "5")
        self.assertTrue(problem.has_usable_sample)
        self.assertEqual(problem.images, ("http://ybt.ssoier.cn:8088/pic/1000.png",))
        self.assertIn("Sample input", ybt_auto.problem_prompt(problem, "python"))
        self.assertIn("standard C17", ybt_auto.problem_prompt(problem, "c"))

    def test_extract_fenced_code(self) -> None:
        code = ybt_auto.extract_code(
            "Here is the solution:\n```python\nprint('ok')\n```"
        )
        self.assertEqual(code, "print('ok')\n")

    def test_reject_unsafe_python(self) -> None:
        with self.assertRaisesRegex(ybt_auto.AutoError, "disallowed module"):
            ybt_auto.validate_python_code("import os\nprint(os.getcwd())")
        with self.assertRaisesRegex(ybt_auto.AutoError, "disallowed function"):
            ybt_auto.validate_python_code("print(open('secret.txt').read())")


class SampleRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.problem = ybt_auto.parse_problem(
            PROBLEM_HTML, 1000, "http://ybt.ssoier.cn:8088/"
        )

    def test_python_sample_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solution.py"
            path.write_text(
                "a, b = map(int, input().split())\nprint(a + b)\n",
                encoding="utf-8",
            )
            result = ybt_auto.run_sample(path, self.problem, "python", 2.0)
        self.assertTrue(result.passed)
        self.assertEqual(result.actual, "5")

    def test_python_sample_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solution.py"
            path.write_text("print(0)\n", encoding="utf-8")
            result = ybt_auto.run_sample(path, self.problem, "python", 2.0)
        self.assertFalse(result.passed)
        self.assertEqual(result.actual, "0")

    def test_c_sample_passes_with_bundled_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solution.c"
            path.write_text(
                "#include <stdio.h>\n"
                "int main(void) { int a, b; "
                "if (scanf(\"%d%d\", &a, &b) != 2) return 1; "
                "printf(\"%d\\n\", a + b); return 0; }\n",
                encoding="utf-8",
            )
            result = ybt_auto.run_sample(path, self.problem, "c", 3.0)
        self.assertTrue(result.passed, result.stderr)
        self.assertEqual(result.actual, "5")

    def test_comments_are_ignored_in_published_samples(self) -> None:
        problem = ybt_auto.Problem(
            pid=1266,
            title="",
            description="",
            input_spec="",
            output_spec="",
            sample_input="3 3 // three companies, three machines",
            sample_output="70 // maximum profit",
            images=(),
            url="",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "solution.py"
            path.write_text(
                "import sys\n"
                "data = sys.stdin.read()\n"
                "assert '//' not in data\n"
                "print(70)\n",
                encoding="utf-8",
            )
            result = ybt_auto.run_sample(path, problem, "python", 2.0)
        self.assertTrue(result.passed, result.stderr)
        self.assertEqual(result.expected, "70")
        self.assertNotIn("//", ybt_auto.problem_prompt(problem, "python"))


class FakeSolver:
    def solve(
        self, prompt: str, previous_code: str = "", sample_error: str = ""
    ) -> str:
        return "```python\nprint(sum(map(int, input().split())))\n```"


class PipelineTests(unittest.TestCase):
    def test_generate_and_validate(self) -> None:
        problem = ybt_auto.parse_problem(
            PROBLEM_HTML, 1000, "http://ybt.ssoier.cn:8088/"
        )
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                code, path = ybt_auto.generate_and_test(
                    problem=problem,
                    solver=FakeSolver(),
                    language="python",
                    attempts=1,
                    sample_timeout=2.0,
                    compiler="",
                    output_dir=Path(directory),
                    show_code=False,
                )
            self.assertTrue(path.exists())
        self.assertIn("sum", code)


class MockSolverHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.assert_json = payload.get("model") == "test-model"
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "```python\nprint(42)\n```",
                        }
                    }
                ]
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SolverApiTests(unittest.TestCase):
    def test_openai_compatible_response(self) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockSolverHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            solver = ybt_auto.OpenAICompatibleSolver(
                base_url=f"http://{host}:{port}/v1",
                api_key="test-key",
                model="test-model",
                timeout=2.0,
            )
            result = solver.solve("solve it")
            self.assertEqual(ybt_auto.extract_code(result), "print(42)\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
