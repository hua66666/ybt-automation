#!/usr/bin/env python3
"""Fetch one YBT problem, generate code, test it, and optionally submit it."""

from __future__ import annotations

import argparse
import ast
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ybt_submit import (  # noqa: E402
    DEFAULT_BASE_URL,
    HttpClient,
    SubmitRateLimiter,
    YbtError,
    default_state_path,
    login,
    normalize_space,
    plain_text,
    submit_once,
)


ALLOWED_PYTHON_IMPORTS = {
    "array",
    "bisect",
    "cmath",
    "collections",
    "copy",
    "decimal",
    "fractions",
    "functools",
    "heapq",
    "itertools",
    "math",
    "operator",
    "queue",
    "random",
    "re",
    "statistics",
    "string",
    "sys",
    "typing",
}

FORBIDDEN_PYTHON_CALLS = {"__import__", "compile", "eval", "exec", "open"}


class AutoError(YbtError):
    """Raised when the generation or validation pipeline cannot continue."""


@dataclass(frozen=True)
class Problem:
    pid: int
    title: str
    description: str
    input_spec: str
    output_spec: str
    sample_input: str
    sample_output: str
    images: tuple[str, ...]
    url: str

    @property
    def has_usable_sample(self) -> bool:
        return bool(self.sample_output.strip()) and not is_no_content(self.sample_output)


@dataclass(frozen=True)
class SampleResult:
    passed: bool
    expected: str
    actual: str
    stderr: str
    duration: float


def is_no_content(value: str) -> bool:
    cleaned = normalize_space(value)
    return cleaned in {"", "无", "(无)", "（无）", "[无]", "【无】"}


def decode_js_string(literal: str) -> str:
    try:
        value = json.loads(literal)
    except json.JSONDecodeError:
        value = literal
    return html.unescape(str(value))


def section_content(section: str) -> str:
    match = re.search(
        r"""pshow\(\s*("(?:\\.|[^"\\])*")\s*\)\s*;""",
        section,
        re.S,
    )
    if match:
        value = decode_js_string(match.group(1))
    else:
        value = section

    replaced = re.sub(r"(?i)<br\s*/?>", "\n", value)
    replaced = re.sub(r"(?i)</(?:p|div|li|tr)>", "\n", replaced)
    replaced = re.sub(r"(?s)<[^>]+>", "", replaced)
    return html.unescape(replaced).strip()


def extract_section(page: str, label: str) -> str:
    pattern = (
        rf"<h3>\s*【{re.escape(label)}】\s*</h3>"
        rf"(.*?)(?=<h3>|</td>|<p\s+align=center>|$)"
    )
    match = re.search(pattern, page, re.S | re.I)
    if not match:
        return ""
    return section_content(match.group(1))


def extract_pre_samples(page: str, label: str) -> str:
    pattern = (
        rf"<h3>\s*【{re.escape(label)}】\s*</h3>"
        rf"(.*?)(?=<h3>|</td>|<p\s+align=center>|$)"
    )
    match = re.search(pattern, page, re.S | re.I)
    if not match:
        return ""
    blocks = re.findall(r"(?is)<pre[^>]*>(.*?)</pre>", match.group(1))
    values = []
    for block in blocks:
        block = re.sub(r"(?i)<br\s*/?>", "\n", block)
        value = html.unescape(re.sub(r"(?s)<[^>]+>", "", block))
        values.append(value.strip("\r\n"))
    return "\n\n".join(values).strip()


def extract_images(page: str, base_url: str) -> tuple[str, ...]:
    body_match = re.search(
        r"(?is)<h3>\s*【题目描述】\s*</h3>(.*?)(?=<p\s+align=center>|$)",
        page,
    )
    body = body_match.group(1) if body_match else ""
    urls: list[str] = []
    for source in re.findall(
        r"""(?is)<img\b[^>]*\bsrc=\\?["']([^"'\\]+)\\?["']""",
        body,
    ):
        absolute = urllib.parse.urljoin(base_url, html.unescape(source))
        if absolute not in urls:
            urls.append(absolute)
    return tuple(urls)


def parse_problem(page: str, pid: int, base_url: str) -> Problem:
    title_match = re.search(
        rf"(?is)<h3>\s*{pid}\s*[：:]\s*(.*?)</h3>", page
    )
    if title_match:
        title = normalize_space(html.unescape(re.sub(r"<[^>]+>", "", title_match.group(1))))
    else:
        title = f"YBT {pid}"

    description = extract_section(page, "题目描述")
    input_spec = extract_section(page, "输入")
    output_spec = extract_section(page, "输出")
    if not description or not output_spec:
        raise AutoError(
            "Could not extract the problem statement. The site markup may have changed."
        )

    return Problem(
        pid=pid,
        title=title,
        description=description,
        input_spec=input_spec,
        output_spec=output_spec,
        sample_input=extract_pre_samples(page, "输入样例"),
        sample_output=extract_pre_samples(page, "输出样例"),
        images=extract_images(page, base_url),
        url=urllib.parse.urljoin(base_url, f"problem_show.php?pid={pid}"),
    )


def fetch_problem(
    client: HttpClient, pid: int, base_url: str = DEFAULT_BASE_URL
) -> Problem:
    response = client.get(
        f"problem_show.php?pid={pid}",
        referer=client.url("problem_list.php"),
    )
    return parse_problem(response.text, pid, base_url)


def problem_prompt(problem: Problem, language: str) -> str:
    if language == "python":
        code_rules = (
            "Use Python 3.10+ and only the standard library. "
            "Read stdin and write stdout. Avoid file and network access."
        )
    elif language == "c":
        code_rules = (
            "Use standard C17 only. Prefer <stdio.h>, <stdlib.h>, <string.h>, "
            "<math.h>, and <limits.h>. Do not use C++ syntax or C++ headers. "
            "The judge submits this source with G++14, so keep it valid C++17 "
            "as well. Avoid C-only features such as restrict and designated "
            "initializers, and cast void* results from malloc/realloc. "
            "Read stdin and write stdout."
        )
    else:
        code_rules = (
            "Use C++17 with #include <bits/stdc++.h> and std namespace. "
            "Read stdin and write stdout."
        )

    image_text = "\n".join(f"- {url}" for url in problem.images) or "- None"
    sample_input = sanitize_sample_text(problem.sample_input)
    sample_output = sanitize_sample_text(problem.sample_output)
    return f"""Solve this competitive programming problem.

Return only the complete source code, without Markdown fences or explanation.
{code_rules}
The judge uses exact input/output, so handle whitespace and line endings carefully.
Use an efficient algorithm that respects typical contest limits.

Problem ID: {problem.pid}
Title: {problem.title}

Description:
{problem.description}

Input specification:
{problem.input_spec or "(not specified)"}

Output specification:
{problem.output_spec}

Sample input:
{sample_input if sample_input else "(none)"}

Sample output:
{sample_output if sample_output else "(none)"}

Referenced images:
{image_text}
"""


def extract_code(response: str) -> str:
    text = response.lstrip("\ufeff").strip()
    fence = re.search(
        r"```(?:python|py|cpp|c\+\+|c)?\s*\r?\n(.*?)```",
        text,
        re.I | re.S,
    )
    if fence:
        text = fence.group(1)
    text = text.strip()
    if text.startswith("```") or text.endswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        raise AutoError("The solver returned an empty code response.")
    return text + "\n"


def validate_python_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise AutoError(f"Generated Python has a syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_PYTHON_IMPORTS:
                    raise AutoError(f"Generated Python imports disallowed module: {root}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root not in ALLOWED_PYTHON_IMPORTS:
                raise AutoError(f"Generated Python imports disallowed module: {root}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_PYTHON_CALLS:
                raise AutoError(
                    f"Generated Python calls disallowed function: {node.func.id}"
                )
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise AutoError("Generated Python uses a disallowed dunder attribute.")


def sanitize_sample_text(value: str) -> str:
    """Remove explanatory comments commonly embedded in published samples."""
    value = re.sub(r"(?s)/\*.*?\*/", "", value)
    return re.sub(r"//[^\r\n]*", "", value)


def normalize_output(value: str) -> str:
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def find_compiler(language: str, requested: str) -> str:
    if requested:
        path = Path(requested).expanduser()
        if not path.is_file():
            raise AutoError(f"Compiler not found: {path}")
        return str(path)

    if language == "c":
        local_tcc = SCRIPT_DIR / "tools" / "tcc" / "tcc" / "tcc.exe"
        candidates = (
            str(local_tcc),
            shutil.which("gcc") or "",
            shutil.which("clang") or "",
            shutil.which("tcc") or "",
        )
    else:
        candidates = (
            shutil.which("g++") or "",
            shutil.which("clang++") or "",
        )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise AutoError(f"No local compiler is available for language: {language}")


def sample_input_text(problem: Problem) -> str:
    if is_no_content(problem.sample_input):
        return ""
    value = sanitize_sample_text(problem.sample_input)
    return value if value.endswith("\n") else value + "\n"


def run_sample(
    source_path: Path,
    problem: Problem,
    language: str,
    timeout: float,
    compiler: str = "",
) -> SampleResult:
    source_path = source_path.resolve()
    expected = normalize_output(sanitize_sample_text(problem.sample_output))
    started = time.monotonic()
    working_dir = source_path.parent
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TMP": tempfile.gettempdir(),
        "TEMP": tempfile.gettempdir(),
    }

    if language == "python":
        command = [sys.executable, "-I", "-S", str(source_path)]
    else:
        compiler_path = find_compiler(language, compiler)
        executable = source_path.with_suffix(".exe")
        compiler_name = Path(compiler_path).name.lower()
        if language == "c" and compiler_name.startswith("tcc"):
            compile_command = [
                compiler_path,
                str(source_path),
                "-o",
                str(executable),
            ]
        elif language == "c":
            compile_command = [
                compiler_path,
                "-std=c17",
                "-O2",
                "-pipe",
                str(source_path),
                "-o",
                str(executable),
            ]
        else:
            compile_command = [
                compiler_path,
                "-std=c++17",
                "-O2",
                "-pipe",
                str(source_path),
                "-o",
                str(executable),
            ]
        try:
            compile_result = subprocess.run(
                compile_command,
                cwd=working_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(20.0, timeout),
                env=env,
            )
        except subprocess.TimeoutExpired:
            return SampleResult(
                passed=False,
                expected=expected,
                actual="",
                stderr="The C++ compiler timed out.",
                duration=time.monotonic() - started,
            )
        except OSError as exc:
            return SampleResult(
                passed=False,
                expected=expected,
                actual="",
                stderr=f"Cannot run the C++ compiler: {exc}",
                duration=time.monotonic() - started,
            )
        if compile_result.returncode != 0:
            return SampleResult(
                passed=False,
                expected=expected,
                actual="",
                stderr=compile_result.stderr or compile_result.stdout,
                duration=time.monotonic() - started,
            )
        command = [str(executable)]

    try:
        result = subprocess.run(
            command,
            input=sample_input_text(problem),
            cwd=working_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return SampleResult(
            passed=False,
            expected=expected,
            actual="",
            stderr=f"Program exceeded the {timeout:g}s sample timeout.",
            duration=time.monotonic() - started,
        )
    except OSError as exc:
        return SampleResult(
            passed=False,
            expected=expected,
            actual="",
            stderr=f"Cannot execute generated code: {exc}",
            duration=time.monotonic() - started,
        )

    actual = normalize_output(result.stdout)
    passed = result.returncode == 0 and actual == expected
    stderr = result.stderr
    if result.returncode != 0:
        stderr = f"Exit code {result.returncode}\n{stderr}".strip()
    elif not passed:
        stderr = stderr or "Output does not match the published sample."
    return SampleResult(
        passed=passed,
        expected=expected,
        actual=actual,
        stderr=stderr,
        duration=time.monotonic() - started,
    )


class OpenAICompatibleSolver:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return self.base_url + "/chat/completions"

    def solve(
        self, prompt: str, previous_code: str = "", sample_error: str = ""
    ) -> str:
        user_content = prompt
        if previous_code:
            user_content += f"""

Your previous code was:
```text
{previous_code}
```

It failed local sample validation with:
{sample_error}

Fix the code and return the complete corrected source code only.
"""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a careful competitive programming solver. "
                        "Return complete, compilable source code and no prose."
                    ),
                },
                {"role": "user", "content": user_content},
            ],
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "YbtAutoSolver/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise AutoError(
                f"Solver API returned HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AutoError(f"Cannot reach solver API: {exc}") from exc

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AutoError("Solver API returned an unexpected response shape.") from exc

        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            )
        if not isinstance(content, str):
            raise AutoError("Solver API did not return text content.")
        return content


class CommandSolver:
    def __init__(self, command: str, timeout: float = 180.0) -> None:
        self.command = shlex.split(command, posix=os.name != "nt")
        if not self.command:
            raise AutoError("--solver-command is empty.")
        self.timeout = timeout

    def solve(
        self, prompt: str, previous_code: str = "", sample_error: str = ""
    ) -> str:
        payload = {
            "prompt": prompt,
            "previous_code": previous_code,
            "sample_error": sample_error,
        }
        try:
            result = subprocess.run(
                self.command,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutoError(f"Solver command failed: {exc}") from exc
        if result.returncode != 0:
            raise AutoError(
                f"Solver command exited with {result.returncode}: "
                f"{(result.stderr or result.stdout)[:1000]}"
            )
        return result.stdout


def create_solver(args: argparse.Namespace):
    if args.solver == "command":
        if not args.solver_command:
            raise AutoError("--solver command requires --solver-command.")
        return CommandSolver(args.solver_command, timeout=args.solver_timeout)

    api_key = os.environ.get("SOLVER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    model = args.solver_model or os.environ.get("SOLVER_MODEL", "")
    if not model:
        raise AutoError(
            "No solver model configured. Set SOLVER_MODEL and SOLVER_API_KEY, "
            "or use --solver command --solver-command."
        )
    base_url = (
        args.solver_url
        or os.environ.get("SOLVER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )
    if "api.openai.com" in base_url and not api_key:
        raise AutoError(
            "OPENAI_API_KEY or SOLVER_API_KEY is required for api.openai.com."
        )
    return OpenAICompatibleSolver(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=args.solver_timeout,
    )


def save_source(
    output_dir: Path,
    problem: Problem,
    code: str,
    language: str,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    extension = {"python": ".py", "c": ".c", "cpp": ".cpp"}[language]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"ybt_{problem.pid}_{stamp}{extension}"
    suffix = 1
    while path.exists():
        path = output_dir / f"ybt_{problem.pid}_{stamp}_{suffix}{extension}"
        suffix += 1
    path.write_text(code, encoding="utf-8")
    return path


def generate_and_test(
    problem: Problem,
    solver,
    language: str,
    attempts: int,
    sample_timeout: float,
    compiler: str,
    output_dir: Path,
    show_code: bool,
) -> tuple[str, Path]:
    if not problem.has_usable_sample:
        raise AutoError(
            "This problem has no usable published sample, so automatic submission "
            "would be unverified. Use ybt_submit.py with reviewed code instead."
        )

    prompt = problem_prompt(problem, language)
    previous_code = ""
    sample_error = ""
    for attempt in range(1, attempts + 1):
        print(f"Solver attempt {attempt}/{attempts}...", flush=True)
        response = solver.solve(prompt, previous_code, sample_error)
        code = extract_code(response)
        if language == "python":
            validate_python_code(code)
        path = save_source(output_dir, problem, code, language)
        print(f"Candidate: {path}")
        if show_code:
            print("```" + ("python" if language == "python" else "cpp"))
            print(code.rstrip())
            print("```")

        result = run_sample(
            path,
            problem,
            language,
            timeout=sample_timeout,
            compiler=compiler,
        )
        print(
            f"Sample check: {'PASS' if result.passed else 'FAIL'} "
            f"({result.duration:.2f}s)"
        )
        if result.passed:
            return code, path

        previous_code = code
        sample_error = (
            f"Expected output:\n{result.expected}\n"
            f"Actual output:\n{result.actual}\n"
            f"Compiler/runtime error:\n{result.stderr}"
        )
        print(sample_error, file=sys.stderr)

    raise AutoError("All generated candidates failed the published sample.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch one YBT problem, generate and sample-test a solution, "
            "then optionally submit it."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--problem", type=int, required=True)
    parser.add_argument(
        "--language",
        choices=("c", "python", "cpp"),
        default="c",
        help="generated solution language; C is the default",
    )
    parser.add_argument(
        "--solver",
        choices=("openai", "command"),
        default="openai",
        help="OpenAI-compatible API or an external command",
    )
    parser.add_argument("--solver-command", help="command that reads JSON from stdin")
    parser.add_argument("--solver-url", help="OpenAI-compatible base URL")
    parser.add_argument("--solver-model", help="model name")
    parser.add_argument("--solver-timeout", type=float, default=120.0)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--sample-timeout", type=float, default=5.0)
    parser.add_argument(
        "--compiler",
        default="",
        help="optional full path to GCC/TCC; bundled TCC is detected automatically",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_DIR / "generated",
        help="directory for generated source files",
    )
    parser.add_argument(
        "--show-code",
        action="store_true",
        help="print each generated candidate",
    )
    parser.add_argument("--submit", action="store_true", help="submit after sample passes")
    parser.add_argument(
        "--ui-submit",
        action="store_true",
        help="submit by opening Edge and clicking the real site controls",
    )
    parser.add_argument("--yes", action="store_true", help="skip submit confirmation")
    parser.add_argument("--username", help="YBT username (else YBT_USERNAME or prompt)")
    parser.add_argument(
        "--submit-interval",
        type=float,
        default=40.0,
        help="minimum seconds between submissions for the same account",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=default_state_path(),
        help="file used to remember the last submission time",
    )
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stderr.isatty() and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.problem <= 0:
            parser.error("--problem must be positive")
        if args.attempts <= 0:
            parser.error("--attempts must be positive")
        if args.sample_timeout <= 0:
            parser.error("--sample-timeout must be positive")

        client = HttpClient(args.base_url, timeout=args.timeout)
        problem = fetch_problem(client, args.problem, args.base_url)
        print(f"Problem: {problem.pid} {problem.title}")
        print(f"Sample:  {'available' if problem.has_usable_sample else 'unavailable'}")
        solver = create_solver(args)
        code, source_path = generate_and_test(
            problem=problem,
            solver=solver,
            language=args.language,
            attempts=args.attempts,
            sample_timeout=args.sample_timeout,
            compiler=args.compiler,
            output_dir=args.output_dir.expanduser().resolve(),
            show_code=args.show_code,
        )

        if not args.submit:
            print(f"Saved sample-passing source: {source_path}")
            print("Nothing was submitted. Add --submit --yes for full automation.")
            return 0

        username = (
            args.username or os.environ.get("YBT_USERNAME") or input("YBT username: ")
        ).strip()
        if not username:
            raise AutoError("YBT username is empty.")
        password = os.environ.get("YBT_PASSWORD")
        if password is None:
            import getpass

            password = getpass.getpass("YBT password: ")
        if not password:
            raise AutoError("YBT password is empty.")

        if args.ui_submit:
            import asyncio

            from ybt_ui_submit import submit_code as submit_via_browser

            return asyncio.run(
                submit_via_browser(
                    problem_id=problem.pid,
                    source=code,
                    username=username,
                    password=password,
                    base_url=args.base_url,
                    state_path=args.state_file.expanduser().resolve(),
                    submit_interval=args.submit_interval,
                    assume_yes=args.yes,
                    poll_timeout=args.poll_timeout,
                )
            )

        session_user = login(client, username, password)
        print(f"Logged in as: {session_user}")
        rate_limiter = SubmitRateLimiter(
            state_path=args.state_file.expanduser().resolve(),
            base_url=args.base_url,
            username=session_user,
            interval=args.submit_interval,
        )
        return submit_once(
            client=client,
            problem_id=problem.pid,
            source=code,
            language_key=args.language,
            dry_run=False,
            assume_yes=args.yes,
            poll_timeout=args.poll_timeout,
            poll_interval=max(0.5, args.poll_interval),
            session_user=session_user,
            rate_limiter=rate_limiter,
        )
    except (YbtError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            print("\nInterrupted.", file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
