#!/usr/bin/env python3
"""Fetch, solve, sample-test, and optionally submit a contiguous range of YBT problems.

The batch runner reuses ybt_auto.py's generation/sample pipeline. Submissions open the
same visible submit panel used by a person, fill it, and click the real submit button.
By default it only generates and locally validates code; add --submit --yes to send
sample-passing solutions to YBT.

Already-accepted problems are recorded in a progress file and skipped on rerun, so
an interrupted batch can be resumed without resubmitting.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ybt_auto import (  # noqa: E402
    AutoError,
    create_solver,
    fetch_problem,
    generate_and_test,
)
from ybt_submit import (  # noqa: E402
    DEFAULT_BASE_URL,
    HttpClient,
    YbtError,
    default_state_path,
)
from ybt_ui_submit import BrowserUiSession  # noqa: E402

PROGRESS_FILENAME = "batch_progress.json"

# Statuses that mean "do not resubmit on a normal rerun".
DONE_STATUSES = {"accepted"}


def default_progress_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    return Path(root) / "ybt_submit" / PROGRESS_FILENAME


def collect_pids(args: argparse.Namespace) -> list[int]:
    pids: list[int] = []
    if args.problems:
        for token in args.problems.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                pid = int(token)
            except ValueError:
                raise SystemExit(f"--problems entry is not an integer: {token!r}")
            if pid <= 0:
                raise SystemExit("--problems must contain positive problem IDs.")
            pids.append(pid)
    elif args.start is not None and args.end is not None:
        if args.start <= 0 or args.end <= 0:
            raise SystemExit("--start and --end must be positive.")
        if args.end < args.start:
            raise SystemExit("--end must be >= --start.")
        pids = list(range(args.start, args.end + 1))
    else:
        raise SystemExit("Give either --start/--end or --problems 1000,1001,...")
    # De-duplicate while preserving order.
    seen: set[int] = set()
    unique: list[int] = []
    for pid in pids:
        if pid not in seen:
            seen.add(pid)
            unique.append(pid)
    return unique


def load_progress(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    results = data.get("results") if isinstance(data, dict) else None
    return results if isinstance(results, dict) else {}


def save_progress(path: Path, results: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": 1, "results": results},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_one_problem(
    client: HttpClient,
    pid: int,
    solver,
    args: argparse.Namespace,
    output_dir: Path,
    browser_session: BrowserUiSession | None,
    loop: asyncio.AbstractEventLoop | None,
) -> tuple[str, str]:
    """Process one problem. Returns (status, human-readable detail)."""
    problem = fetch_problem(client, pid, args.base_url)
    print(f"--- {pid} {problem.title} ---")

    if not problem.has_usable_sample:
        return "no_sample", "no published sample output; refusing to submit unverified code"

    code, source_path = generate_and_test(
        problem=problem,
        solver=solver,
        language=args.language,
        attempts=args.attempts,
        sample_timeout=args.sample_timeout,
        compiler=args.compiler,
        output_dir=output_dir,
        show_code=args.show_code,
    )

    if not args.submit:
        return "generated_only", str(source_path)

    if browser_session is None or loop is None:
        raise AutoError("Internal error: --submit requested without a browser session.")

    rc = loop.run_until_complete(
        browser_session.submit(
            problem_id=pid,
            source=code,
            assume_yes=True,
            poll_timeout=args.poll_timeout,
        )
    )
    if rc == 0:
        return "accepted", str(source_path)
    if rc == 2:
        return "compile_error", str(source_path)
    return "submitted_non_accepted", str(source_path)


def classify_failure(exc: Exception) -> tuple[str, str]:
    text = str(exc)
    lowered = text.lower()
    if isinstance(exc, AutoError):
        if "no usable published sample" in lowered:
            return "no_sample", text
        if "could not extract the problem statement" in lowered:
            return "problem_missing", text
        return "local_failed", text
    if isinstance(exc, YbtError):
        if "no such problem" in lowered or "http 404" in lowered:
            return "problem_missing", text
        if "login failed" in lowered or "login did not establish" in lowered:
            return "login_failed", text
        if "session is not open" in lowered or "requires a login" in lowered:
            return "session_lost", text
        return "site_error", text
    return "error", text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Process a range of YBT problems in order: fetch statement, generate a "
            "solution, run the published sample locally, and (with --submit --yes) "
            "submit sample-passing solutions."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--start", type=int, help="first problem ID (inclusive)")
    parser.add_argument("--end", type=int, help="last problem ID (inclusive)")
    parser.add_argument(
        "--problems",
        help="explicit comma-separated list, e.g. 1000,1001,1005",
    )
    parser.add_argument(
        "--language",
        choices=("c", "python", "cpp"),
        default="c",
        help="generated solution language",
    )
    # Solver passthrough.
    parser.add_argument("--solver", choices=("openai", "command"), default="openai")
    parser.add_argument("--solver-command")
    parser.add_argument("--solver-url")
    parser.add_argument("--solver-model")
    parser.add_argument("--solver-timeout", type=float, default=120.0)
    parser.add_argument("--attempts", type=int, default=2)
    parser.add_argument("--sample-timeout", type=float, default=5.0)
    parser.add_argument("--compiler", default="")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=SCRIPT_DIR / "generated",
        help="directory for generated source files",
    )
    parser.add_argument("--show-code", action="store_true")
    # Submit control.
    parser.add_argument(
        "--submit",
        action="store_true",
        help="actually submit sample-passing solutions (default: only generate+validate)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="required together with --submit to suppress confirmation",
    )
    parser.add_argument("--username", help="YBT username (else YBT_USERNAME or prompt)")
    parser.add_argument("--submit-interval", type=float, default=40.0)
    parser.add_argument("--state-file", type=Path, default=default_state_path())
    parser.add_argument("--poll-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    parser.add_argument("--timeout", type=float, default=25.0)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--progress-file",
        type=Path,
        default=default_progress_path(),
        help="file recording per-problem results; accepted problems are skipped on rerun",
    )
    parser.add_argument(
        "--redo",
        action="store_true",
        help="ignore saved progress and reprocess every problem in the range",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if not sys.stdout.isatty() and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stderr.isatty() and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    pids = collect_pids(args)
    progress_path = args.progress_file.expanduser().resolve()
    results = {} if args.redo else load_progress(progress_path)

    if args.submit and not args.yes:
        parser.error(
            "--submit requires --yes for batch mode; this is deliberate so the flag "
            "combination is explicit."
        )

    client = HttpClient(args.base_url, timeout=args.timeout)
    solver = create_solver(args)

    browser_session: BrowserUiSession | None = None
    loop: asyncio.AbstractEventLoop | None = None
    username = ""
    password = ""

    if args.submit:
        username = (
            args.username
            or os.environ.get("YBT_USERNAME")
            or input("YBT username: ")
        ).strip()
        if not username:
            print("Error: YBT username is empty.", file=sys.stderr)
            return 2
        password = os.environ.get("YBT_PASSWORD")
        if password is None:
            password = getpass.getpass("YBT password: ")
        if not password:
            print("Error: YBT password is empty.", file=sys.stderr)
            return 2

        loop = asyncio.new_event_loop()
        browser_session = BrowserUiSession(
            base_url=args.base_url,
            username=username,
            password=password,
            state_path=args.state_file.expanduser().resolve(),
            submit_interval=args.submit_interval,
        )
        try:
            loop.run_until_complete(browser_session.open())
        except Exception as exc:
            try:
                loop.run_until_complete(browser_session.close())
            except Exception:
                pass
            loop.close()
            print(f"Login failed: {exc}", file=sys.stderr)
            return 2
        print(f"Logged in as: {browser_session.session_user}")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Batch range: {pids[0]}..{pids[-1]} ({len(pids)} problem(s))")
    mode = "GENERATE+SAMPLE VALIDATE ONLY" if not args.submit else "BROWSER AUTO SUBMIT"
    print(f"Mode:        {mode}")
    print(f"Progress:    {progress_path}")
    print()

    counts: dict[str, int] = {}
    try:
        for index, pid in enumerate(pids, start=1):
            existing = results.get(str(pid))
            if (
                not args.redo
                and existing
                and existing.get("status") in DONE_STATUSES
            ):
                print(f"[{index}/{len(pids)}] {pid}: already accepted, skipping.")
                counts["accepted"] = counts.get("accepted", 0) + 1
                continue

            print(f"[{index}/{len(pids)}] Processing problem {pid} ...")
            status = ""
            detail = ""
            try:
                status, detail = run_one_problem(
                    client=client,
                    pid=pid,
                    solver=solver,
                    args=args,
                    output_dir=output_dir,
                    browser_session=browser_session,
                    loop=loop,
                )
            except KeyboardInterrupt:
                results[str(pid)] = {
                    "status": "interrupted",
                    "detail": "cancelled by user",
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                save_progress(progress_path, results)
                print("\nInterrupted; progress saved.", file=sys.stderr)
                return 130
            except (YbtError, AutoError) as exc:
                status, detail = classify_failure(exc)
                print(f"Problem {pid} failed: {exc}", file=sys.stderr)

            results[str(pid)] = {
                "status": status,
                "detail": detail[:500],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_progress(progress_path, results)
            counts[status] = counts.get(status, 0) + 1
            print(f"== {pid}: {status} ==")
            print()
    finally:
        save_progress(progress_path, results)
        if browser_session is not None and loop is not None:
            try:
                loop.run_until_complete(browser_session.close())
            except Exception as exc:
                print(f"Warning: browser close failed: {exc}", file=sys.stderr)
            finally:
                loop.close()

    print("===== Batch summary =====")
    for pid in pids:
        entry = results.get(str(pid), {})
        print(f"  {pid:>6}  {entry.get('status', '?'):<24} {entry.get('detail', '')}")
    print()
    total = len(pids)
    accepted = counts.get("accepted", 0)
    failed = (
        counts.get("local_failed", 0)
        + counts.get("site_error", 0)
        + counts.get("login_failed", 0)
        + counts.get("session_lost", 0)
        + counts.get("error", 0)
    )
    print(f"Total {total}: accepted {accepted}, "
          f"other submitted {counts.get('submitted_non_accepted', 0) + counts.get('compile_error', 0)}, "
          f"skipped {counts.get('no_sample', 0) + counts.get('problem_missing', 0)}, "
          f"failed {failed}")
    if not args.submit:
        print("Nothing was submitted. Re-run with --submit --yes after reviewing generated code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
