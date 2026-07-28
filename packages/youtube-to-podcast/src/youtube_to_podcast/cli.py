"""Command-line entrypoint for YouTube to Podcast."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__
from .config import (
    ConfigError,
    load_config,
    transistor_api_key,
    write_starter_config,
)
from .executor import apply_plan
from .hashing import PlanError, verify_plan
from .planner import build_plan, write_plan
from .review import render_review
from .transistor import TransistorClient
from .youtube import (
    fetch_metadata,
    list_public_videos,
    prepare_media,
    ytdlp_binary,
)


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def command_init(args: argparse.Namespace) -> int:
    path = write_starter_config(
        args.config,
        channel_url=args.channel_url,
        show_id=args.show_id,
        force=args.force,
    )
    env_path = path.parent / ".env.example"
    if not env_path.exists():
        env_path.write_text(
            "# Copy to .env; never commit the real key.\n"
            "TRANSISTOR_API_KEY=your_transistor_api_key\n",
            encoding="utf-8",
        )
    gitignore_path = path.parent / ".gitignore"
    required_ignores = (".env", ".youtube-to-podcast/")
    existing_lines = (
        gitignore_path.read_text(encoding="utf-8").splitlines()
        if gitignore_path.exists()
        else []
    )
    missing_ignores = [item for item in required_ignores if item not in existing_lines]
    if missing_ignores:
        prefix = "\n" if existing_lines and existing_lines[-1] else ""
        with gitignore_path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(missing_ignores) + "\n")
    _print(
        {
            "status": "initialized",
            "config_path": str(path),
            "env_example_path": str(env_path),
            "gitignore_path": str(gitignore_path),
            "next": [
                "Copy .env.example to .env and add TRANSISTOR_API_KEY",
                f"youtube-to-podcast doctor --config {path.name} --online",
                f"youtube-to-podcast plan --config {path.name}",
            ],
        }
    )
    return 0


def _tool_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    return (result.stdout or result.stderr).strip().splitlines()[0]


def command_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    checks: list[dict[str, Any]] = []
    try:
        yt_dlp = ytdlp_binary()
    except ConfigError:
        yt_dlp = None
    ffmpeg = shutil.which("ffmpeg")
    checks.append(
        {
            "name": "yt-dlp",
            "ok": bool(yt_dlp),
            "detail": _tool_version([yt_dlp, "--version"]) if yt_dlp else "not found",
        }
    )
    checks.append(
        {
            "name": "ffmpeg",
            "ok": bool(ffmpeg),
            "detail": _tool_version([ffmpeg, "-version"]) if ffmpeg else "not found",
        }
    )
    try:
        api_key = transistor_api_key(config)
    except ConfigError as exc:
        api_key = ""
        checks.append({"name": "TRANSISTOR_API_KEY", "ok": False, "detail": str(exc)})
    else:
        checks.append(
            {
                "name": "TRANSISTOR_API_KEY",
                "ok": True,
                "detail": "configured",
            }
        )
    if args.online and api_key:
        try:
            show = TransistorClient(api_key).get_show(config.show_id)
        except Exception as exc:
            checks.append(
                {
                    "name": "Transistor show",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        else:
            attrs = show.get("attributes") or {}
            checks.append(
                {
                    "name": "Transistor show",
                    "ok": str(show.get("id") or "") == config.show_id,
                    "detail": attrs.get("title") or attrs.get("name") or config.show_id,
                }
            )
    ok = all(item["ok"] for item in checks)
    _print(
        {
            "status": "healthy" if ok else "needs_attention",
            "config_path": str(config.path),
            "work_dir": str(config.work_dir),
            "mode": config.policy.mode,
            "publication": config.policy.publication,
            "checks": checks,
        }
    )
    return 0 if ok else 1


def command_plan(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = TransistorClient(transistor_api_key(config))
    show = client.get_show(config.show_id)
    if str(show.get("id") or "") != config.show_id:
        raise RuntimeError(
            f"Transistor did not return the configured show {config.show_id}"
        )
    listed = list_public_videos(config.channel_url)
    plan = build_plan(
        config,
        client,
        listed,
        fetch_metadata=fetch_metadata,
        prepare_media=lambda video_id, output_dir: prepare_media(
            video_id,
            output_dir,
            download_subtitles=config.policy.download_subtitles,
            subtitle_languages=config.policy.subtitle_languages,
        ),
    )
    json_path, markdown_path = write_plan(plan, config.work_dir)
    apply_command = (
        "youtube-to-podcast apply "
        f"--config {shlex.quote(str(config.path))} "
        f"--plan {shlex.quote(str(json_path))} "
        f"--approval-hash {plan['approval_hash']}"
    )
    review_path = render_review(
        plan,
        json_path.with_suffix(".html"),
        apply_command=apply_command,
    )
    render_review(
        plan,
        config.work_dir / "plans" / "latest.html",
        apply_command=apply_command,
    )
    _print(
        {
            "status": "planned",
            "plan_path": str(json_path),
            "summary_path": str(markdown_path),
            "review_path": str(review_path),
            "plan_hash": plan["plan_hash"],
            "approval_hash": plan["approval_hash"],
            **plan["summary"],
            "next": (
                "Review the Markdown summary. Remote writes require: "
                f"youtube-to-podcast apply --config {config.path.name} "
                f"--plan {json_path} --approval-hash {plan['approval_hash']}"
            ),
        }
    )
    return 0


def command_review(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plan_path = (
        args.plan.expanduser().resolve()
        if args.plan
        else config.work_dir / "plans" / "latest.json"
    )
    if not plan_path.exists():
        raise PlanError(f"Plan file does not exist: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    verify_plan(plan)
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else plan_path.with_suffix(".html")
    )
    apply_command = (
        "youtube-to-podcast apply "
        f"--config {shlex.quote(str(config.path))} "
        f"--plan {shlex.quote(str(plan_path))} "
        f"--approval-hash {plan['approval_hash']}"
    )
    render_review(plan, output_path, apply_command=apply_command)
    opened = False if args.no_open else webbrowser.open(output_path.as_uri())
    _print(
        {
            "status": "review_ready",
            "review_path": str(output_path),
            "browser_open_requested": not args.no_open,
            "browser_opened": bool(opened),
            "approval_hash": plan["approval_hash"],
        }
    )
    return 0


def command_apply(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    plan_path = (
        args.plan.expanduser().resolve()
        if args.plan
        else config.work_dir / "plans" / "latest.json"
    )
    if not plan_path.exists():
        raise PlanError(f"Plan file does not exist: {plan_path}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = apply_plan(
        config,
        TransistorClient(transistor_api_key(config)),
        plan,
        supplied_approval_hash=args.approval_hash,
    )
    _print({"status": "applied", "plan_path": str(plan_path), **result})
    return 0


def command_update_ytdlp(args: argparse.Namespace) -> int:
    before = _tool_version([sys.executable, "-m", "yt_dlp", "--version"])
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        "--no-deps",
        "--retries",
        "2",
        "--timeout",
        "30",
    ]
    if args.pre:
        command.append("--pre")
    command.append("yt-dlp")
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        after = _tool_version([sys.executable, "-m", "yt_dlp", "--version"])
        if after:
            _print(
                {
                    "status": "update_failed_using_current",
                    "version_before": before,
                    "version_after": after,
                    "pre_release_allowed": args.pre,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            return 0
        raise RuntimeError(f"yt-dlp update failed and no working version remains: {exc}")

    after = _tool_version([sys.executable, "-m", "yt_dlp", "--version"])
    if result.returncode == 0 and after:
        status = "updated" if before != after else "already_latest"
        detail = None
        return_code = 0
    elif after:
        status = "update_failed_using_current"
        lines = (result.stderr or result.stdout).strip().splitlines()
        detail = lines[-1][-500:] if lines else f"pip exited {result.returncode}"
        return_code = 0
    else:
        status = "unavailable"
        lines = (result.stderr or result.stdout).strip().splitlines()
        detail = lines[-1][-500:] if lines else f"pip exited {result.returncode}"
        return_code = 1
    _print(
        {
            "status": status,
            "version_before": before,
            "version_after": after,
            "pre_release_allowed": args.pre,
            "detail": detail,
        }
    )
    return return_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube-to-podcast",
        description=(
            "Plan-first, hash-approved YouTube to podcast publishing."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a starter config")
    init_parser.add_argument(
        "--config", type=Path, default=Path("youtube-to-podcast.toml")
    )
    init_parser.add_argument("--channel-url", required=True)
    init_parser.add_argument("--show-id", required=True)
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=command_init)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Validate local tools and credentials"
    )
    doctor_parser.add_argument(
        "--config", type=Path, default=Path("youtube-to-podcast.toml")
    )
    doctor_parser.add_argument(
        "--online",
        action="store_true",
        help="Also make a read-only Transistor show request",
    )
    doctor_parser.set_defaults(func=command_doctor)

    plan_parser = subparsers.add_parser(
        "plan", help="Prepare media and build an immutable read-only plan"
    )
    plan_parser.add_argument(
        "--config", type=Path, default=Path("youtube-to-podcast.toml")
    )
    plan_parser.set_defaults(func=command_plan)

    review_parser = subparsers.add_parser(
        "review", help="Generate and open a local visual plan review"
    )
    review_parser.add_argument(
        "--config", type=Path, default=Path("youtube-to-podcast.toml")
    )
    review_parser.add_argument("--plan", type=Path)
    review_parser.add_argument("--output", type=Path)
    review_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Generate the HTML file without opening a browser",
    )
    review_parser.set_defaults(func=command_review)

    apply_parser = subparsers.add_parser(
        "apply", help="Execute one exact, previously approved plan"
    )
    apply_parser.add_argument(
        "--config", type=Path, default=Path("youtube-to-podcast.toml")
    )
    apply_parser.add_argument("--plan", type=Path)
    apply_parser.add_argument("--approval-hash", required=True)
    apply_parser.set_defaults(func=command_apply)

    update_parser = subparsers.add_parser(
        "update-ytdlp", help="Explicitly upgrade yt-dlp"
    )
    update_parser.add_argument(
        "--pre",
        action="store_true",
        help="Allow the newest pre-release/nightly from PyPI",
    )
    update_parser.set_defaults(func=command_update_ytdlp)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, PlanError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
