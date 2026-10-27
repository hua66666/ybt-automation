from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Sequence

from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

import ybt_submit as ybt


class UiSubmitError(ybt.YbtError):
    pass


def _profile_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) if root else Path(tempfile.gettempdir())
    return base / "ybt_submit" / "edge_profile_ui"


def _extract_browser_username(text: str) -> str:
    marker = "\u3011"
    return text.rsplit(marker, 1)[-1].strip() if marker in text else ""


async def _browser_username(page: Page) -> str:
    link = page.locator('a[href="my_info.php"]').first
    if not await link.count():
        return ""
    return _extract_browser_username((await link.inner_text()).strip())


def _parse_ui_submit_response(body: str) -> int | None:
    text = body.lstrip("\ufeff").strip()
    if not text.startswith("ok!"):
        return None
    run_id = text[3:].strip()
    return int(run_id) if run_id.isdigit() else None


async def _login_if_needed(
    page: Page, base_url: str, username: str, password: str
) -> str:
    await page.goto(base_url + "index.php", wait_until="domcontentloaded")
    current = await _browser_username(page)
    if current:
        return current

    await page.evaluate("idx302()")
    await page.fill("input[name=username]", username)
    await page.fill("input[name=password]", password)
    try:
        await page.click("input[name=login][type=submit]")
        await page.wait_for_url(
            re.compile(r"/index\.php(?:[?#].*)?$"),
            timeout=30000,
            wait_until="domcontentloaded",
        )
    except PlaywrightTimeoutError as exc:
        raise UiSubmitError(
            "Browser login did not establish an authenticated session before timeout."
        ) from exc
    await page.wait_for_load_state("domcontentloaded")

    current = await _browser_username(page)
    if current:
        return current
    body = await page.locator("body").inner_text()
    if "???" in body:
        raise UiSubmitError("The site requires a verification code; automatic login stopped.")
    raise UiSubmitError("Browser login did not establish an authenticated session.")


async def _open_submit_page(page: Page, base_url: str, problem_id: int) -> None:
    await page.goto(base_url + "problem_list.php", wait_until="domcontentloaded")
    await page.fill("#xproblem", str(problem_id))
    print("Opening problem search...", flush=True)
    await page.evaluate("search()")
    await page.wait_for_url(
        re.compile(rf"problem_search2\.php\?xproblem={problem_id}"),
        timeout=30000,
        wait_until="domcontentloaded",
    )

    problem_link = page.locator(f'a[href="problem_show.php?pid={problem_id}"]').first
    if not await problem_link.count():
        raise UiSubmitError(f"Problem {problem_id} was not found in the problem list search.")
    print("Opening problem page...", flush=True)
    await problem_link.click(no_wait_after=True)
    await page.wait_for_url(
        re.compile(rf"problem_show\.php\?pid={problem_id}$"),
        timeout=30000,
        wait_until="domcontentloaded",
    )

    submit_link = page.locator('a[onclick="submit()"]').first
    if not await submit_link.count():
        raise UiSubmitError("The problem page has no clickable submit control.")
    print("Clicking problem-page submit...", flush=True)
    await submit_link.click()

    panel = page.locator("#bbb1")
    try:
        await panel.wait_for(state="visible", timeout=30000)
        await page.locator("#bbb1 #source").wait_for(state="visible", timeout=5000)
    except PlaywrightTimeoutError as exc:
        raise UiSubmitError("The problem-page submit panel did not become ready.") from exc

    panel_problem = await page.locator("#problem").input_value()
    if panel_problem.strip() != str(problem_id):
        raise UiSubmitError(
            f"The submit panel shows problem {panel_problem!r}, expected {problem_id}."
        )


async def _write_ui_diagnostic(
    page: Page, problem_id: int, reason: str, response_body: str = ""
) -> Path:
    directory = Path(tempfile.gettempdir()) / "ybt_submit"
    directory.mkdir(parents=True, exist_ok=True)
    stem = time.strftime(f"ui_submit_error_{problem_id}_%Y%m%d_%H%M%S")
    screenshot = directory / f"{stem}.png"
    diagnostic = directory / f"{stem}.txt"
    await page.screenshot(path=str(screenshot), full_page=True)

    panel_text = ""
    alert_text = ""
    if await page.locator("#bbb1").count():
        panel_text = await page.locator("#bbb1").inner_text()
    if await page.locator("#xalert").count():
        alert_text = await page.locator("#xalert").inner_text()
    diagnostic.write_text(
        "\n".join(
            [
                f"Problem: {problem_id}",
                f"URL: {page.url}",
                f"Reason: {reason}",
                f"Alert: {alert_text}",
                f"Response: {response_body!r}",
                "",
                "Submit panel:",
                panel_text,
                "",
                f"Screenshot: {screenshot}",
            ]
        ),
        encoding="utf-8",
    )
    return diagnostic


async def _pause_for_verdict(page: Page, base_url: str, run_id: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while True:
        await page.goto(
            base_url + f"statusx1.php?runidx={run_id}",
            wait_until="domcontentloaded",
        )
        result, _ = ybt.parse_verdict_response(await page.locator("body").inner_text())
        print(f"Verdict: {result or '(empty)'}", flush=True)
        if result.lower() not in ybt.PENDING_RESULTS:
            return result
        if time.monotonic() >= deadline:
            raise UiSubmitError(f"Verdict for run {run_id} is still pending.")
        await asyncio.sleep(1.5)


class BrowserUiSession:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        state_path: Path,
        submit_interval: float,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.state_path = state_path
        self.submit_interval = submit_interval
        self._playwright = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self.session_user = ""

    async def open(self) -> None:
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(_profile_dir()),
                channel="msedge",
                headless=False,
                no_viewport=True,
            )
            self.page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            self.session_user = await _login_if_needed(
                self.page, self.base_url, self.username, self.password
            )
            print(f"Browser login: {self.session_user}", flush=True)
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self.page = None

    async def submit(
        self,
        problem_id: int,
        source: str,
        assume_yes: bool,
        poll_timeout: float,
        dry_run: bool = False,
    ) -> int:
        if self.page is None:
            raise UiSubmitError("Browser session is not open.")

        page = self.page
        try:
            self.session_user = await _login_if_needed(
                page, self.base_url, self.username, self.password
            )
        except PlaywrightTimeoutError as exc:
            raise UiSubmitError(
                f"Browser navigation timed out while logging in: {exc}"
            ) from exc
        rate_limiter = ybt.SubmitRateLimiter(
            state_path=self.state_path,
            base_url=self.base_url,
            username=self.session_user,
            interval=self.submit_interval,
        )
        try:
            await _open_submit_page(page, self.base_url, problem_id)
        except PlaywrightTimeoutError as exc:
            raise UiSubmitError(
                f"Browser navigation timed out while opening problem {problem_id}: {exc}"
            ) from exc

        await page.select_option("#bbb1 select[name=language]", "7")
        await page.fill("#bbb1 #source", source)
        print(f"Problem: {problem_id}", flush=True)
        print("Language: G++14 (C-compatible)", flush=True)
        print(f"Source: {len(source)} characters", flush=True)

        if dry_run:
            print("Browser dry run complete; the submit button was not clicked.", flush=True)
            return 0
        if not assume_yes:
            answer = input(f"Click the real submit button for problem {problem_id}? [y/N] ")
            if answer.strip().lower() not in {"y", "yes", "?"}:
                print("Cancelled.", flush=True)
                return 0

        rate_limiter.wait()
        rate_limiter.record()
        submit_button = page.locator("#bbb1 input[type=button]").first
        try:
            async with page.expect_response(
                lambda response: "problem_ac.php" in response.url
                and response.request.method == "POST",
                timeout=30000,
            ) as response_info:
                await submit_button.click()
            response = await response_info.value
            response_body = await response.text()
        except PlaywrightTimeoutError as exc:
            diagnostic = await _write_ui_diagnostic(
                page, problem_id, "The submit button produced no response."
            )
            raise UiSubmitError(
                "The submit button produced no response. "
                f"Diagnostic: {diagnostic}"
            ) from exc

        run_id = _parse_ui_submit_response(response_body)
        if run_id is None:
            alert_text = ""
            if await page.locator("#xalert").count():
                alert_text = (await page.locator("#xalert").inner_text()).strip()
            reason = "The site returned no new run ID."
            if alert_text:
                reason += f" Site message: {alert_text}"
            diagnostic = await _write_ui_diagnostic(
                page, problem_id, reason, response_body
            )
            raise UiSubmitError(f"{reason} Diagnostic: {diagnostic}")

        print(f"Run ID: {run_id}", flush=True)
        result = await _pause_for_verdict(page, self.base_url, run_id, poll_timeout)
        if result.lower().startswith("accepted"):
            return 0
        if result.lower().startswith("compile error"):
            return 2
        return 1


async def submit_code(
    problem_id: int,
    source: str,
    username: str,
    password: str,
    base_url: str,
    state_path: Path,
    submit_interval: float,
    assume_yes: bool,
    poll_timeout: float,
    dry_run: bool = False,
) -> int:
    session = BrowserUiSession(
        base_url=base_url,
        username=username,
        password=password,
        state_path=state_path,
        submit_interval=submit_interval,
    )
    await session.open()
    try:
        return await session.submit(
            problem_id=problem_id,
            source=source,
            assume_yes=assume_yes,
            poll_timeout=poll_timeout,
            dry_run=dry_run,
        )
    finally:
        await session.close()


def _credentials(args: argparse.Namespace) -> tuple[str, str]:
    username = (args.username or os.environ.get("YBT_USERNAME") or input("YBT username: ")).strip()
    password = os.environ.get("YBT_PASSWORD") or getpass.getpass("YBT password: ")
    return username, password


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit one YBT problem by clicking the browser UI.")
    parser.add_argument("--problem", type=int, required=True)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--username", default="")
    parser.add_argument("--base-url", default=ybt.DEFAULT_BASE_URL)
    parser.add_argument("--submit-interval", type=float, default=40.0)
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    parser.add_argument("--state-file", type=Path, default=ybt.default_state_path())
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    username, password = _credentials(args)
    if not username or not password:
        print("Username and password are required.", file=os.sys.stderr)
        return 2

    try:
        source = args.file.read_text(encoding="utf-8")
        return asyncio.run(
            submit_code(
                problem_id=args.problem,
                source=source,
                username=username,
                password=password,
                base_url=args.base_url,
                state_path=args.state_file.expanduser().resolve(),
                submit_interval=args.submit_interval,
                assume_yes=args.yes,
                poll_timeout=args.poll_timeout,
                dry_run=args.dry_run,
            )
        )
    except (ybt.YbtError, OSError, PlaywrightTimeoutError) as exc:
        print(f"Error: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
