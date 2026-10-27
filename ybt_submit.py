#!/usr/bin/env python3
"""Submit source code to the YBT online judge and poll the verdict."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import http.cookiejar
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Sequence

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows is the primary target.
    msvcrt = None

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows is the primary target.
    fcntl = None


DEFAULT_BASE_URL = "http://ybt.ssoier.cn:8088/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
PENDING_RESULTS = {"", "c", "compiling", "waiting", "judging", "queued", "pending"}
DEFAULT_SUBMIT_INTERVAL = 40.0
SUBMIT_SAFETY_MARGIN = 1.0
SUBMIT_CONFIRM_TIMEOUT = 5.0
SUBMIT_LOCK_TIMEOUT = 300.0


class YbtError(RuntimeError):
    """Base error for expected site or submission failures."""


class LoginError(YbtError):
    """Raised when the site does not establish an authenticated session."""


class FormError(YbtError):
    """Raised when the submit form cannot be understood."""


class SubmitError(YbtError):
    """Raised when the site rejects a submission."""


class RateLimitError(SubmitError):
    """Raised when the site says submissions are too close together."""


class SubmitFileLock:
    """Serialize submissions across separate tool processes."""

    def __init__(self, path: Path, timeout: float = SUBMIT_LOCK_TIMEOUT) -> None:
        self.path = path
        self.timeout = timeout
        self._handle = None
        self._locked = False

    def __enter__(self) -> SubmitFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()

        deadline = time.monotonic() + self.timeout
        announced = False
        while True:
            try:
                self._handle.seek(0)
                if msvcrt is not None:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(
                        self._handle.fileno(),
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                else:
                    raise SubmitError("No cross-process file lock is available.")
                self._locked = True
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise SubmitError(
                        "Another YBT submission kept the account lock for too long. "
                        "Close duplicate tool windows and try again."
                    ) from exc
                if not announced:
                    print(
                        "Another YBT submission is active; waiting for it to finish.",
                        flush=True,
                    )
                    announced = True
                time.sleep(0.5)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            if self._locked:
                self._handle.seek(0)
                if msvcrt is not None:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                elif fcntl is not None:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._handle.close()
            self._handle = None


@dataclass(frozen=True)
class Language:
    key: str
    label: str
    aliases: tuple[str, ...]
    preferred_values: tuple[str, ...]


LANGUAGES = {
    "cpp": Language(
        "cpp",
        "G++",
        ("g++", "c++", "cpp", "gnu c++", "c++17", "c++14"),
        ("1", "7", "0", "cpp"),
    ),
    # The site's G++14 compiler is the reliable default. C sources are
    # generated as a C17/C++17-compatible subset and locally checked with TCC.
    "c": Language(
        "c",
        "G++14 (C17-compatible)",
        ("g++14", "c language", "c"),
        ("7", "1", "2", "c"),
    ),
    "java": Language("java", "Java", ("java", "javac"), ("3", "java")),
    "pascal": Language("pascal", "Pascal", ("pascal", "fpc"), ("4", "pascal")),
    "python": Language("python", "Python", ("python", "py", "pypy"), ("5", "python")),
}

LANGUAGE_ALIASES = {
    "1": "cpp",
    "g++": "cpp",
    "cpp": "cpp",
    "c++": "cpp",
    "2": "c",
    "gcc": "c",
    "c": "c",
    "3": "java",
    "java": "java",
    "4": "pascal",
    "pas": "pascal",
    "pascal": "pascal",
    "5": "python",
    "py": "python",
    "python": "python",
}

EXTENSION_LANGUAGES = {
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c": "c",
    ".java": "java",
    ".pas": "pascal",
    ".py": "python",
}

LOGIN_ERROR_MARKERS = (
    "用户不存在",
    "密码错误",
    "用户名或密码不能为空",
    "验证码",
)

SUBMIT_ERROR_MARKERS = (
    "你还没有登录",
    "请先登录",
    "没有登录",
    "Invalid User.",
    "Invalid Submit.",
    "No Such Problem.",
    "Please Select Language.",
    "Source too long!",
    "代码不能为空",
    "系统繁忙",
    "提交失败",
    "题目不存在",
    "无权限",
)

RATE_LIMIT_MARKERS = (
    "提交过于频繁",
    "提交间隔",
    "40秒",
    "40 秒",
    "频率过高",
    "频繁提交",
    "Too Fast!",
    "too fast",
)


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: object
    text: str


@dataclass
class Option:
    value: str
    text: str
    selected: bool = False


@dataclass
class Field:
    tag: str
    name: str = ""
    value: str = ""
    field_type: str = ""
    options: list[Option] = field(default_factory=list)
    checked: bool = False


@dataclass
class Form:
    action: str
    method: str
    fields: list[Field]


@dataclass(frozen=True)
class StatusRecord:
    user: str
    run_id: int
    problem_id: int
    result: str
    language: str
    code_length: str
    submitted_at: str


def default_state_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) if root else Path(tempfile.gettempdir())
    return base / "ybt_submit" / "submit_state.json"


class SubmitRateLimiter:
    def __init__(
        self,
        state_path: Path,
        base_url: str,
        username: str,
        interval: float,
        safety_margin: float = SUBMIT_SAFETY_MARGIN,
        clock=time.time,
        sleeper=time.sleep,
    ) -> None:
        self.state_path = state_path
        self.interval = max(0.0, interval)
        self.safety_margin = max(0.0, safety_margin)
        self.clock = clock
        self.sleeper = sleeper
        self._memory_last_submit: float | None = None
        key_input = f"{base_url.rstrip('/')}|{username.casefold()}"
        self.account_key = hashlib.sha256(key_input.encode("utf-8")).hexdigest()[:24]

    def _read_states(self) -> dict[str, float]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(value, dict):
            return {}
        accounts = value.get("accounts", value)
        if not isinstance(accounts, dict):
            return {}
        result: dict[str, float] = {}
        for key, timestamp in accounts.items():
            if isinstance(key, str) and isinstance(timestamp, (int, float)):
                result[key] = float(timestamp)
        return result

    def seconds_remaining(self) -> float:
        last_submit = self._read_states().get(self.account_key)
        if self._memory_last_submit is not None:
            last_submit = max(
                last_submit or 0.0,
                self._memory_last_submit,
            )
        if last_submit is None:
            return 0.0
        allowed_at = last_submit + self.interval + self.safety_margin
        return max(0.0, allowed_at - float(self.clock()))

    def wait(self) -> None:
        remaining = self.seconds_remaining()
        if remaining <= 0:
            return
        print(
            f"Submission interval: waiting {remaining:.1f}s before submitting.",
            flush=True,
        )
        self.sleeper(remaining)

    def record(self) -> None:
        timestamp = float(self.clock())
        self._memory_last_submit = timestamp
        states = self._read_states()
        states[self.account_key] = timestamp
        payload = json.dumps(
            {"version": 1, "accounts": states},
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_name(
                f"{self.state_path.name}.{os.getpid()}.tmp"
            )
            temporary.write_text(payload + "\n", encoding="utf-8")
            os.replace(temporary, self.state_path)
        except OSError as exc:
            print(
                f"Warning: cannot save submission time to {self.state_path}: {exc}",
                file=sys.stderr,
            )

    def submission_lock(self) -> SubmitFileLock:
        return SubmitFileLock(self.state_path.with_name(self.state_path.name + ".lock"))


class HttpClient:
    def __init__(self, base_url: str, timeout: float = 25.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def url(self, path_or_url: str) -> str:
        return urllib.parse.urljoin(self.base_url, path_or_url)

    def get(self, path_or_url: str, referer: str | None = None) -> HttpResponse:
        return self._request("GET", path_or_url, referer=referer)

    def post(
        self,
        path_or_url: str,
        data: Iterable[tuple[str, str]],
        referer: str | None = None,
    ) -> HttpResponse:
        body = urllib.parse.urlencode(list(data), doseq=True).encode("utf-8")
        return self._request("POST", path_or_url, body=body, referer=referer)

    def _request(
        self,
        method: str,
        path_or_url: str,
        body: bytes | None = None,
        referer: str | None = None,
    ) -> HttpResponse:
        url = self.url(path_or_url)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            parsed_url = urllib.parse.urlsplit(url)
            headers["Origin"] = f"{parsed_url.scheme}://{parsed_url.netloc}"
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Sec-Fetch-Dest"] = "document"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-User"] = "?1"

        last_error: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(
                url, data=body, headers=headers, method=method
            )
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        text = raw.decode(charset, errors="replace")
                    except LookupError:
                        text = raw.decode("utf-8", errors="replace")
                    return HttpResponse(
                        url=response.geturl(),
                        status=getattr(response, "status", 200),
                        headers=response.headers,
                        text=text,
                    )
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                charset = exc.headers.get_content_charset() or "utf-8"
                text = raw.decode(charset, errors="replace")
                if 400 <= exc.code < 500:
                    raise YbtError(
                        f"Site returned HTTP {exc.code} for {url}: {plain_text(text)[:300]}"
                    ) from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc

            if attempt < 2:
                time.sleep(1.0 + attempt)

        raise YbtError(f"Cannot reach {url}: {last_error}") from last_error


class SubmitFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[Form] = []
        self._form: Form | None = None
        self._select: Field | None = None
        self._option: Option | None = None
        self._textarea: Field | None = None
        self._textarea_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()

        if tag == "form":
            self._form = Form(
                action=values.get("action", ""),
                method=values.get("method", "get").lower(),
                fields=[],
            )
            return
        if self._form is None:
            return
        if tag == "input":
            self._form.fields.append(
                Field(
                    tag="input",
                    name=values.get("name", ""),
                    value=values.get("value", ""),
                    field_type=values.get("type", "text").lower(),
                    checked="checked" in values,
                )
            )
        elif tag == "button":
            self._form.fields.append(
                Field(
                    tag="button",
                    name=values.get("name", ""),
                    value=values.get("value", ""),
                    field_type=values.get("type", "submit").lower(),
                )
            )
        elif tag == "select":
            self._select = Field(
                tag="select",
                name=values.get("name", ""),
                field_type="select",
                options=[],
            )
            self._form.fields.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = Option(
                value=values.get("value", ""),
                text="",
                selected="selected" in values,
            )
            self._select.options.append(self._option)
        elif tag == "textarea":
            self._textarea = Field(
                tag="textarea",
                name=values.get("name", ""),
                field_type="textarea",
            )
            self._textarea_parts = []
            self._form.fields.append(self._textarea)

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option.text += data
        if self._textarea is not None:
            self._textarea_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "option":
            if self._option is not None:
                self._option.text = normalize_space(self._option.text)
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            if self._textarea is not None:
                self._textarea.value = "".join(self._textarea_parts)
            self._textarea = None
            self._textarea_parts = []
        elif tag == "form":
            if self._form is not None:
                self.forms.append(self._form)
            self._form = None

    def close(self) -> None:
        super().close()
        if self._form is not None:
            self.forms.append(self._form)
            self._form = None


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def plain_text(page: str) -> str:
    without_scripts = re.sub(
        r"(?is)<(script|style)\b.*?</\1>", " ", html.unescape(page)
    )
    return normalize_space(re.sub(r"(?s)<[^>]+>", " ", without_scripts))


def extract_logged_user(page: str) -> str:
    match = re.search(r"""var\s+user\s*=\s*["']([^"']*)["']""", page)
    if not match:
        return ""
    return html.unescape(match.group(1)).strip()


def login(client: HttpClient, username: str, password: str) -> str:
    response = client.post(
        "login.php",
        (("username", username), ("password", password), ("login", "登录")),
        referer=client.url("index.php"),
    )
    response_text = f"{plain_text(response.text)} {' '.join(alert_messages(response.text))}"
    for marker in LOGIN_ERROR_MARKERS:
        if marker in response_text:
            raise LoginError(f"Login failed: {marker}")

    home = client.get("index.php")
    logged_user = extract_logged_user(home.text)
    if logged_user and logged_user.lower() not in {"0", "guest"}:
        return logged_user

    if username.lower() in plain_text(home.text).lower() and "用户登录" not in home.text:
        return username
    raise LoginError(
        "Login did not establish a session. Check the username/password and try again."
    )


def parse_forms(page: str) -> list[Form]:
    parser = SubmitFormParser()
    parser.feed(page)
    parser.close()
    return parser.forms


def find_submit_form(page: str) -> Form:
    forms = parse_forms(page)
    candidates = [
        form
        for form in forms
        if "login.php" not in form.action.lower()
        and any(field.tag == "textarea" for field in form.fields)
    ]
    if candidates:
        return candidates[0]

    if "你还没有登录" in page or any(
        "login.php" in form.action.lower() for form in forms
    ):
        raise LoginError("The submit page still requires a login.")
    raise FormError(
        "Could not find the submit form. Run again with --dry-run to inspect the page."
    )


def resolve_language(value: str) -> str:
    key = value.strip().lower()
    if key == "auto":
        return "auto"
    try:
        return LANGUAGE_ALIASES[key]
    except KeyError as exc:
        choices = ", ".join(sorted(set(LANGUAGE_ALIASES.values())))
        raise YbtError(f"Unknown language '{value}'. Use one of: {choices}") from exc


def infer_language(path: Path) -> str:
    key = EXTENSION_LANGUAGES.get(path.suffix.lower())
    if not key:
        raise YbtError(
            f"Cannot infer a language from '{path.suffix or path.name}'. "
            "Pass --language cpp, c, java, pascal, or python."
        )
    return key


def option_matches(option: Option, aliases: Sequence[str]) -> bool:
    text = normalize_space(option.text).lower()
    text_words = re.sub(r"[^a-z0-9+]+", " ", text).strip()
    for alias in aliases:
        if text == alias or text_words == alias:
            return True
        if len(alias) >= 3 and alias in text:
            return True
    return False


def choose_language(language_key: str, options: Sequence[Option]) -> str:
    if language_key in LANGUAGE_ALIASES:
        language_key = LANGUAGE_ALIASES[language_key]
    if language_key not in LANGUAGES:
        raise YbtError(f"Unsupported language key: {language_key}")

    spec = LANGUAGES[language_key]
    for option in options:
        if option_matches(option, spec.aliases):
            return option.value
    for preferred in spec.preferred_values:
        for option in options:
            if option.value.strip().lower() == preferred:
                return option.value
    selected = [option for option in options if option.selected]
    if selected:
        return selected[0].value
    raise FormError(f"Could not map {spec.label} to a language option.")


def find_textarea(form: Form) -> Field:
    preferred_names = ("code", "source", "sourcecode", "src", "answer")
    textareas = [field for field in form.fields if field.tag == "textarea"]
    if not textareas:
        raise FormError("The submit form has no textarea for source code.")
    for name in preferred_names:
        for field in textareas:
            if name in field.name.lower():
                return field
    return textareas[0]


def prepare_form_data(
    form: Form, problem_id: int, source: str, language_key: str
) -> tuple[list[tuple[str, str]], str]:
    code_field = find_textarea(form)
    selected_language = ""
    data: list[tuple[str, str]] = []

    for field in form.fields:
        name = field.name
        value = field.value
        lower_name = name.lower()
        field_type = field.field_type.lower()

        if field is code_field:
            value = source
        elif field.tag == "select":
            if "lang" in lower_name:
                value = choose_language(language_key, field.options)
                selected_language = value
            else:
                selected = [option for option in field.options if option.selected]
                if selected:
                    value = selected[0].value
                elif field.options:
                    value = field.options[0].value
        elif (
            "pid" in lower_name
            or lower_name in {"problem", "problem_id", "problemid"}
        ):
            value = str(problem_id)
        elif field_type in {"checkbox", "radio"}:
            if not field.checked:
                continue
            if not value:
                value = "on"
        elif field_type in {"reset", "button"}:
            continue

        if name:
            data.append((name, value))

    names = {name.lower() for name, _ in data}
    if not any("pid" in name or name in {"problem", "problem_id"} for name in names):
        data.append(("pid", str(problem_id)))
    if not any("lang" in name for name in names):
        spec = LANGUAGES[language_key]
        data.append(("lang", spec.preferred_values[0]))

    if not selected_language:
        selected_language = dict(data).get("lang", LANGUAGES[language_key].label)
    return data, selected_language


def alert_messages(page: str) -> list[str]:
    return [
        html.unescape(value).strip()
        for value in re.findall(
            r"""alert\(\s*["'](.*?)["']\s*\)""", page, re.I | re.S
        )
    ]


def raise_for_submit_error(page: str) -> None:
    messages = " | ".join(alert_messages(page))
    combined = f"{plain_text(page)} {messages}"
    combined_folded = combined.casefold()
    for marker in RATE_LIMIT_MARKERS:
        if marker.casefold() in combined_folded:
            detail = messages or marker
            raise RateLimitError(f"Site rate limit reached: {detail}")
    for marker in SUBMIT_ERROR_MARKERS:
        if marker.casefold() in combined_folded:
            detail = messages or marker
            raise SubmitError(f"Site rejected the submission: {detail}")


def extract_run_id(page: str) -> int | None:
    patterns = (
        r"statusx1?\.php\?runidx=(\d+)",
        r"show_ce_info\.php\?runid=(\d+)",
        r"[?&]runid(?:x)?=(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.I)
        if match:
            return int(match.group(1))
    return None


def parse_status_records(page: str) -> list[StatusRecord]:
    match = re.search(r"""var\s+ee\s*=\s*["'](.*?)["']\s*\.split""", page, re.S)
    if not match:
        return []

    records: list[StatusRecord] = []
    raw_records = html.unescape(match.group(1)).split("#")
    for raw_record in raw_records:
        parts = raw_record.split("`")
        if len(parts) < 7:
            continue
        try:
            run_id = int(parts[1])
            problem_id = int(parts[2])
        except ValueError:
            continue
        records.append(
            StatusRecord(
                user=parts[0].split(":")[-1],
                run_id=run_id,
                problem_id=problem_id,
                result=parts[3],
                language=parts[4],
                code_length=parts[5],
                submitted_at=parts[6],
            )
        )
    return records


def fetch_status_records(
    client: HttpClient, problem_id: int, username: str = ""
) -> list[StatusRecord]:
    query = urllib.parse.urlencode(
        {
            "showname": username,
            "showpid": str(problem_id),
            "showres": "",
            "showlang": "",
        }
    )
    page = client.get(f"status.php?{query}", referer=client.url("status.php"))
    return parse_status_records(page.text)


def fetch_status_snapshot(
    client: HttpClient, problem_id: int, username: str
) -> tuple[list[StatusRecord], bool]:
    """Return records for this user when possible, otherwise a global snapshot."""
    records = fetch_status_records(client, problem_id, username)
    if records:
        return records, True
    return fetch_status_records(client, problem_id), False


def initialize_submit_session(
    client: HttpClient, page_url: str
) -> tuple[float, HttpResponse]:
    """Call the timing endpoint that initializes the site's submit cookie."""
    response = client.get("submit_time.php", referer=page_url)
    text = response.text.lstrip("\ufeff \t\r\n")
    match = re.search(r"\d+", text)
    if not match:
        return 0.0, response
    return min(float(match.group()), 120.0), response


def wait_for_new_run_id(
    client: HttpClient,
    problem_id: int,
    username: str,
    before_ids: set[int],
    timeout: float = SUBMIT_CONFIRM_TIMEOUT,
    interval: float = 1.0,
) -> int | None:
    deadline = time.monotonic() + timeout
    while True:
        records, user_specific = fetch_status_snapshot(
            client, problem_id, username
        )
        new_records = [
            record for record in records if record.run_id not in before_ids
        ]
        if user_specific and new_records:
            return max(record.run_id for record in new_records)
        if not user_specific and len(new_records) == 1:
            return new_records[0].run_id
        if len(new_records) > 1:
            raise SubmitError(
                "The site accepted the request, but multiple new runs appeared. "
                f"Check {client.url(f'status.php?showpid={problem_id}')}"
            )
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def write_submit_error_diagnostic(
    problem_id: int,
    page_url: str,
    form: Form,
    data: list[tuple[str, str]],
    timer_response: HttpResponse | None,
    response: HttpResponse,
    client: HttpClient,
) -> Path:
    directory = Path(tempfile.gettempdir()) / "ybt_submit"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = directory / f"submit_error_{problem_id}_{stamp}.txt"

    sensitive_words = ("token", "csrf", "session", "password", "secret")
    source_names = {"source", "code", "sourcecode", "answer"}

    def safe_value(name: str, value: str) -> str:
        lowered = name.lower()
        if lowered in source_names or any(word in lowered for word in sensitive_words):
            return f"<redacted: {len(value)} chars>"
        return value[:300]

    lines = [
        f"time={time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"problem_id={problem_id}",
        f"submit_page={page_url}",
        f"form_action={form.action!r}",
        f"form_method={form.method}",
        "",
        "[form fields]",
    ]
    for field in form.fields:
        description = (
            f"{field.tag} name={field.name!r} type={field.field_type!r} "
            f"value={safe_value(field.name, field.value)!r}"
        )
        if field.options:
            description += (
                " options="
                + repr(
                    [
                        (option.value, option.text, option.selected)
                        for option in field.options
                    ]
                )
            )
        lines.append(description)

    lines.extend(["", "[submitted data]"])
    for name, value in data:
        lines.append(f"{name}={safe_value(name, value)!r}")

    lines.extend(["", "[submit timer]"])
    if timer_response is None:
        lines.append("(not available)")
    else:
        lines.extend(
            [
                f"url={timer_response.url}",
                f"status={timer_response.status}",
                f"body={timer_response.text!r}",
            ]
        )

    lines.extend(
        [
            "",
            "[action response]",
            f"url={response.url}",
            f"status={response.status}",
        ]
    )
    for name, value in response.headers.items():
        if name.lower() == "set-cookie":
            continue
        lines.append(f"header {name}: {value}")
    lines.extend(
        [
            f"body_repr={response.text[:4000]!r}",
            "",
            "[cookies]",
        ]
    )
    for cookie in client.cookies:
        lines.append(
            f"name={cookie.name} domain={cookie.domain} path={cookie.path} "
            f"secure={cookie.secure}"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def parse_verdict_response(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split(":", 4)
    if len(parts) < 5:
        return "", parts
    return parts[4].strip(), parts


def verdict_user_matches(expected_user: str, actual_user: str) -> bool:
    if not expected_user or not actual_user:
        return True
    if expected_user.casefold() == actual_user.casefold():
        return True
    # The login page exposes a display name while statusx returns a Uid.
    actual_is_uid = re.fullmatch(r"Uid\d+", actual_user, re.I) is not None
    expected_is_uid = re.fullmatch(r"Uid\d+", expected_user, re.I) is not None
    return actual_is_uid and not expected_is_uid


def poll_verdict(
    client: HttpClient,
    run_id: int,
    timeout: float,
    interval: float,
    expected_user: str,
) -> tuple[str, list[str]]:
    deadline = time.monotonic() + timeout
    last_result = ""
    last_parts: list[str] = []
    while True:
        response = client.get(
            f"statusx1.php?runidx={run_id}",
            referer=client.url(f"statusx.php?runidx={run_id}"),
        )
        result, parts = parse_verdict_response(response.text)
        last_result, last_parts = result, parts
        if parts:
            actual_user = parts[0].lstrip("\ufeff").strip()
            if not verdict_user_matches(expected_user, actual_user):
                raise YbtError(
                    f"Run {run_id} belongs to '{actual_user}', not "
                    f"'{expected_user}'. Refusing to report another user's verdict. "
                    f"Open {client.url(f'statusx.php?runidx={run_id}')}"
                )
        print(f"Verdict: {result or '(empty)'}", flush=True)
        if result.lower() not in PENDING_RESULTS:
            return result, parts
        if time.monotonic() >= deadline:
            raise YbtError(
                f"Verdict still pending after {timeout:.0f}s. "
                f"Open {client.url(f'statusx.php?runidx={run_id}')}"
            )
        time.sleep(interval)


def open_problem_submit_page(client: HttpClient, problem_id: int) -> HttpResponse:
    """Follow the site's normal list-search-problem-submit navigation."""
    list_page = client.get("problem_list.php", referer=client.url("index.php"))
    search_page = client.get(
        f"problem_search2.php?xproblem={problem_id}",
        referer=list_page.url,
    )
    problem_page = client.get(
        f"problem_show.php?pid={problem_id}",
        referer=search_page.url,
    )
    return client.get(
        f"submit.php?pid={problem_id}",
        referer=problem_page.url,
    )


def submit_once(
    client: HttpClient,
    problem_id: int,
    source: str,
    language_key: str,
    dry_run: bool,
    assume_yes: bool,
    poll_timeout: float,
    poll_interval: float,
    session_user: str,
    rate_limiter: SubmitRateLimiter | None = None,
) -> int:
    submit_url = f"submit.php?pid={problem_id}"
    page = open_problem_submit_page(client, problem_id)
    form = find_submit_form(page.text)
    data, selected_language = prepare_form_data(
        form, problem_id, source, language_key
    )
    code_field = find_textarea(form)

    print(f"Problem:  {problem_id}")
    print(f"Language: {LANGUAGES[language_key].label} (form value {selected_language})")
    print(f"Source:   {len(source)} characters")
    print(f"Form:     {form.method.upper()} {urllib.parse.urljoin(page.url, form.action)}")
    print(f"Code field: {code_field.name or '(unnamed textarea)'}")

    if dry_run:
        visible_names = [
            name for name, _ in data if name.lower() not in {"code", "source"}
        ]
        print(f"Detected fields: {', '.join(visible_names) or '(none)'}")
        print("Dry run complete; nothing was submitted.")
        return 0

    if not assume_yes:
        answer = input(f"Submit to problem {problem_id} now? [y/N] ").strip().lower()
        if answer not in {"y", "yes", "是"}:
            print("Cancelled.")
            return 0

    action_url = urllib.parse.urljoin(client.url(submit_url), form.action or submit_url)

    timer_response: HttpResponse | None = None

    def perform_submission() -> tuple[HttpResponse, set[int]]:
        nonlocal timer_response
        if rate_limiter is not None:
            rate_limiter.wait()

        site_wait, timer_response = initialize_submit_session(client, page.url)
        if site_wait > 0:
            print(f"Site submit timer: waiting {site_wait:g}s.", flush=True)
            time.sleep(site_wait)

        before_records, _ = fetch_status_snapshot(
            client, problem_id, session_user
        )
        before_ids = {record.run_id for record in before_records}
        try:
            response = client.post(action_url, data, referer=page.url)
        finally:
            if rate_limiter is not None:
                rate_limiter.record()

        try:
            raise_for_submit_error(response.text)
        except RateLimitError:
            if rate_limiter is None:
                raise
            print(
                "The server rejected the interval; waiting and retrying once.",
                flush=True,
            )
            rate_limiter.wait()
            try:
                response = client.post(action_url, data, referer=page.url)
            finally:
                rate_limiter.record()
        raise_for_submit_error(response.text)
        return response, before_ids

    if rate_limiter is None:
        response, before_ids = perform_submission()
    else:
        with rate_limiter.submission_lock():
            response, before_ids = perform_submission()

    run_id = extract_run_id(response.text)
    success_markers = ("Submitted", "提交成功")
    response_path = urllib.parse.urlparse(response.url).path.lower()
    response_folded = response.text.casefold()
    response_looks_successful = (
        any(marker.casefold() in response_folded for marker in success_markers)
        or "status" in response_path
    )

    if run_id is None:
        run_id = wait_for_new_run_id(
            client,
            problem_id,
            session_user,
            before_ids,
        )

    if run_id is None and not response_looks_successful:
        if response.text.strip("\ufeff \t\r\n") == "":
            diagnostic_path = write_submit_error_diagnostic(
                problem_id=problem_id,
                page_url=page.url,
                form=form,
                data=data,
                timer_response=timer_response,
                response=response,
                client=client,
            )
            raise SubmitError(
                "The site returned an empty response and no new run ID was found. "
                "The submission was silently discarded, possibly by a temporary "
                "login/anti-automation limit. Do not retry immediately. "
                f"Diagnostic: {diagnostic_path}"
            )
        response_excerpt = (
            " | ".join(alert_messages(response.text))
            or plain_text(response.text)[:500]
            or "(empty response)"
        )
        raise SubmitError(
            "The site did not confirm the submission and no new run ID was found. "
            f"Response: {response_excerpt}"
        )
    if run_id is None:
        print("Submission was accepted, but the new run ID was not visible yet.")
        print(f"Open: {client.url(f'status.php?showpid={problem_id}')}")
        return 0

    print(f"Run ID:   {run_id}")
    if poll_timeout <= 0:
        print(f"Open: {client.url(f'statusx.php?runidx={run_id}')}")
        return 0
    result, parts = poll_verdict(
        client, run_id, poll_timeout, poll_interval, session_user
    )
    if parts:
        print(f"Result:   {result}")
    if result.lower().startswith("accepted"):
        return 0
    if result.lower().startswith("compile error"):
        print(f"Compile log: {client.url(f'show_ce_info.php?runid={run_id}')}")
        return 2
    return 1


def probe_site(client: HttpClient, problem_id: int | None = None) -> int:
    home = client.get("index.php")
    title = re.search(r"(?is)<title>(.*?)</title>", home.text)
    print(f"Site:   {client.base_url}")
    print(f"Status: HTTP {home.status}")
    print(f"Title:  {normalize_space(html.unescape(title.group(1))) if title else '(none)'}")
    print(f"Login:  {'yes' if extract_logged_user(home.text) else 'no'}")
    if problem_id is not None:
        problem = client.get(f"problem_show.php?pid={problem_id}")
        title = re.search(r"(?is)<h3>(.*?)</h3>", problem.text)
        print(f"Problem {problem_id}: HTTP {problem.status}")
        if title:
            print(f"Problem title: {normalize_space(html.unescape(re.sub(r'<[^>]+>', '', title.group(1))))}")
    return 0


def read_source(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise YbtError(f"Cannot read source file '{path}': {exc}") from exc

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise YbtError(f"Source file '{path}' is neither UTF-8 nor GB18030 text.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Submit one source file to ybt.ssoier.cn and wait for its verdict.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--problem", type=int, help="YBT problem ID")
    parser.add_argument("-f", "--file", type=Path, help="source file to submit")
    parser.add_argument(
        "-l",
        "--language",
        default="auto",
        help="cpp, c, java, pascal, python, a numeric form value, or auto",
    )
    parser.add_argument("--username", help="YBT username (else YBT_USERNAME or prompt)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="login and inspect the submit form without submitting",
    )
    parser.add_argument(
        "--yes", action="store_true", help="skip the final interactive confirmation"
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="submit but do not poll the verdict",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=180.0,
        help="maximum seconds to wait for a verdict",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=1.5, help="seconds between polls"
    )
    parser.add_argument(
        "--submit-interval",
        type=float,
        default=DEFAULT_SUBMIT_INTERVAL,
        help="minimum seconds between submissions for the same account",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=default_state_path(),
        help="file used to remember the last submission time",
    )
    parser.add_argument(
        "--timeout", type=float, default=25.0, help="HTTP request timeout in seconds"
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL, help="site base URL"
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="check the public site without logging in or submitting",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stderr.isatty() and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    client = HttpClient(args.base_url, timeout=args.timeout)

    try:
        if args.probe:
            return probe_site(client, args.problem)
        if args.problem is None or args.file is None:
            parser.error("--problem and --file are required unless --probe is used")
        if args.problem <= 0:
            parser.error("--problem must be a positive integer")
        if args.submit_interval < 0:
            parser.error("--submit-interval cannot be negative")

        source_path = args.file.expanduser().resolve()
        source = read_source(source_path)
        if not source.strip():
            raise YbtError("Source file is empty.")

        language = resolve_language(args.language)
        if language == "auto":
            language = infer_language(source_path)

        username = (
            args.username or os.environ.get("YBT_USERNAME") or input("YBT username: ")
        ).strip()
        if not username:
            raise LoginError("Username is empty.")
        password = os.environ.get("YBT_PASSWORD")
        if password is None:
            password = getpass.getpass("YBT password: ")
        if not password:
            raise LoginError("Password is empty.")

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
            problem_id=args.problem,
            source=source,
            language_key=language,
            dry_run=args.dry_run,
            assume_yes=args.yes,
            poll_timeout=0.0 if args.no_wait else args.poll_timeout,
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
