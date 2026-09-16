"""CLI subcommand and runnable entrypoint for the discord-radar observer."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home
from hermes_cli.profiles import get_profile_dir, profile_exists
from agent.secret_scope import set_secret_scope, reset_secret_scope, build_profile_secret_scope

from .hook import run_discord_blocker_hook
from .observer import RADAR_ID

DEFAULT_STATE_PATH = Path("/home/hermes/.hermes/profiles/otto/cron/discord-now-state.json")
DEFAULT_TARGET_BOT = "1546329595446296658"


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Register radar-observer CLI arguments on `hermes radar-observer`."""
    subparser.add_argument(
        "--state-path",
        type=str,
        default=str(DEFAULT_STATE_PATH),
        help=f"Path to discord-now-state.json (default: {DEFAULT_STATE_PATH})",
    )
    subparser.add_argument(
        "--report-path",
        type=str,
        default=None,
        help="Optional path to write JSON observer report",
    )
    subparser.add_argument(
        "--target-bot-id",
        type=str,
        default=DEFAULT_TARGET_BOT,
        help=f"Target bot ID (e.g. Otto: {DEFAULT_TARGET_BOT})",
    )
    subparser.add_argument(
        "--observer-bot-id",
        type=str,
        default=RADAR_ID,
        help=f"Observer bot ID (Radar: {RADAR_ID})",
    )
    subparser.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Profile from which to load scoped secret if not present in ambient environment (e.g. 'otto')",
    )
    subparser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry-run evaluation without sending messages",
    )
    subparser.add_argument(
        "--disabled",
        action="store_true",
        help="Disable execution safely (returns empty report without mutations)",
    )
    subparser.set_defaults(func=radar_observer_command)


def radar_observer_command(args: argparse.Namespace) -> int:
    """Execute the blocker observer hook using CLI arguments."""
    state_path = getattr(args, "state_path", None) or str(DEFAULT_STATE_PATH)
    report_path = getattr(args, "report_path", None)
    target_bot_id = getattr(args, "target_bot_id", None) or DEFAULT_TARGET_BOT
    observer_bot_id = getattr(args, "observer_bot_id", None) or RADAR_ID
    dry_run = bool(getattr(args, "dry_run", False))
    disabled = bool(getattr(args, "disabled", False))
    profile_name = getattr(args, "profile", None)

    # If DISCORD_OBSERVER_BOT_TOKEN is not in active environment or secret scope,
    # and a profile was specified, hydrate the secret scope from that profile.
    from agent.secret_scope import get_secret
    token = get_secret("DISCORD_OBSERVER_BOT_TOKEN")
    reset_token = None
    if not token and profile_name and profile_exists(profile_name):
        pdir = get_profile_dir(profile_name)
        secrets = build_profile_secret_scope(pdir)
        reset_token = set_secret_scope(secrets)

    try:
        report = run_discord_blocker_hook(
            state_file_path=state_path,
            output_report_path=report_path,
            target_bot_id=target_bot_id,
            observer_bot_id=observer_bot_id,
            enabled=not disabled,
            dry_run=dry_run,
        )
        if report.errors:
            print(f"[discord-radar] Finished with errors: {report.errors}", file=sys.stderr)
            return 1
        print(
            f"[discord-radar] Success: evaluated {len(report.evaluated_items)}, "
            f"suggestions {len(report.suggestions)}, delivered {len(report.delivery_topics)}"
        )
        return 0
    finally:
        if reset_token is not None:
            reset_secret_scope(reset_token)


def main() -> int:
    """Direct execution entrypoint (e.g. for `python3 -m plugins.discord-radar.cli`)."""
    parser = argparse.ArgumentParser(
        prog="radar-observer",
        description="Run Discord Radar blocker observer cycle",
    )
    register_cli(parser)
    parsed = parser.parse_args()
    return radar_observer_command(parsed)


if __name__ == "__main__":
    sys.exit(main())
