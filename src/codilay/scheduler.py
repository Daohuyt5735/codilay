"""
Scheduler — auto-trigger documentation updates on a cron schedule or on
new commits to the main branch.

Usage:
    codilay schedule . --cron "0 */6 * * *"      Every 6 hours
    codilay schedule . --on-commit                On new commits to main
    codilay schedule . --cron "0 9 * * 1-5"      Weekday mornings at 9am
    codilay schedule list                         Show active schedules
    codilay schedule stop                         Stop all schedules

Implementation uses a lightweight background daemon that checks on intervals.
No external scheduler dependencies required — just threading and git polling.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rich.console import Console


# ── Cron expression parser (minimal, no dependencies) ─────────────────────────


class CronExpression:
    """
    Minimal cron parser supporting: minute hour day-of-month month day-of-week
    Supports: *, */N, N, N-M, N,M,O
    """

    def __init__(self, expression: str):
        self.expression = expression.strip()
        parts = self.expression.split()
        if len(parts) != 5:
            raise ValueError(f"Invalid cron expression: '{expression}' (need 5 fields)")

        self._minute = self._parse_field(parts[0], 0, 59)
        self._hour = self._parse_field(parts[1], 0, 23)
        self._dom = self._parse_field(parts[2], 1, 31)
        self._month = self._parse_field(parts[3], 1, 12)
        self._dow = self._parse_field(parts[4], 0, 6)

    def matches(self, dt: Optional[datetime] = None) -> bool:
        """Check if a datetime matches this cron expression."""
        if dt is None:
            dt = datetime.now()
        return (
            dt.minute in self._minute
            and dt.hour in self._hour
            and dt.day in self._dom
            and dt.month in self._month
            and dt.weekday() in self._dow  # Python: Mon=0, cron: Sun=0
        )

    def _parse_field(self, field: str, min_val: int, max_val: int) -> set:
        """Parse a single cron field into a set of valid values."""
        values = set()

        for part in field.split(","):
            part = part.strip()

            if part == "*":
                values.update(range(min_val, max_val + 1))
            elif "/" in part:
                base, step = part.split("/", 1)
                step = int(step)
                if base == "*":
                    start = min_val
                else:
                    start = int(base)
                values.update(range(start, max_val + 1, step))
            elif "-" in part:
                start, end = part.split("-", 1)
                values.update(range(int(start), int(end) + 1))
            else:
                values.add(int(part))

        return values

    def __str__(self):
        return self.expression


# ── Schedule config ───────────────────────────────────────────────────────────


class ScheduleConfig:
    """Manages schedule configuration stored in the project's codilay dir."""

    def __init__(self, output_dir: str):
        self._config_path = os.path.join(output_dir, "schedule.json")

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self._config_path):
            return {
                "enabled": False,
                "cron": None,
                "on_commit": False,
                "branch": "main",
                "last_run": None,
                "last_commit_checked": None,
                "run_count": 0,
                "created_at": None,
            }
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"enabled": False, "cron": None, "on_commit": False}

    def save(self, config: Dict[str, Any]):
        config["updated_at"] = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(self._config_path) or ".", exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def set_cron(self, cron_expr: str, branch: str = "main"):
        """Set a cron schedule."""
        # Validate the expression
        CronExpression(cron_expr)  # Raises ValueError if invalid
        config = self.load()
        config["enabled"] = True
        config["cron"] = cron_expr
        config["branch"] = branch
        config["created_at"] = config.get("created_at") or datetime.now(timezone.utc).isoformat()
        self.save(config)

    def set_on_commit(self, branch: str = "main"):
        """Enable commit-triggered updates."""
        config = self.load()
        config["enabled"] = True
        config["on_commit"] = True
        config["branch"] = branch
        config["created_at"] = config.get("created_at") or datetime.now(timezone.utc).isoformat()
        self.save(config)

    def disable(self):
        config = self.load()
        config["enabled"] = False
        self.save(config)

    def record_run(self, commit: str = ""):
        config = self.load()
        config["last_run"] = datetime.now(timezone.utc).isoformat()
        config["run_count"] = config.get("run_count", 0) + 1
        if commit:
            config["last_commit_checked"] = commit
        self.save(config)


# ── Scheduler daemon ──────────────────────────────────────────────────────────


class Scheduler:
    """
    Background scheduler that polls for cron matches or new commits
    and triggers documentation updates.
    """

    def __init__(
        self,
        target_path: str,
        output_dir: Optional[str] = None,
        verbose: bool = False,
    ):
        self.target_path = os.path.abspath(target_path)
        self.output_dir = output_dir or os.path.join(self.target_path, "codilay")
        self.verbose = verbose
        self.console = Console()
        self._running = False
        self._config = ScheduleConfig(self.output_dir)
        self._poll_interval = 60  # Check every 60 seconds

    def start(self):
        """Start the scheduler in the foreground (blocking)."""
        config = self._config.load()
        if not config.get("enabled"):
            self.console.print("[yellow]No schedule configured. Use --cron or --on-commit first.[/yellow]")
            return

        self._running = True
        cron_expr = config.get("cron")
        on_commit = config.get("on_commit", False)
        branch = config.get("branch", "main")

        cron = CronExpression(cron_expr) if cron_expr else None

        schedule_desc = []
        if cron:
            schedule_desc.append(f"Cron: [cyan]{cron_expr}[/cyan]")
        if on_commit:
            schedule_desc.append(f"On commit to [cyan]{branch}[/cyan]")

        from rich.panel import Panel

        self.console.print(
            Panel(
                f"[bold]CodiLay Scheduler[/bold]\n\n"
                f"  Project:  [cyan]{os.path.basename(self.target_path)}[/cyan]\n"
                f"  {'  '.join(schedule_desc)}\n"
                f"  Polling:  every [yellow]{self._poll_interval}s[/yellow]\n\n"
                f"[dim]Press Ctrl+C to stop.[/dim]",
                border_style="green",
                title="schedule",
            )
        )

        last_cron_minute = -1  # Prevent double-firing within the same minute

        try:
            while self._running:
                now = datetime.now()

                # Check cron
                if cron and cron.matches(now) and now.minute != last_cron_minute:
                    last_cron_minute = now.minute
                    self.console.print(f"[blue][{now.strftime('%H:%M:%S')}][/blue] Cron match — triggering update...")
                    self._trigger_update()

                # Check for new commits
                if on_commit:
                    new_commit = self._check_new_commits(branch)
                    if new_commit:
                        self.console.print(
                            f"[blue][{now.strftime('%H:%M:%S')}][/blue] "
                            f"New commit on {branch}: [cyan]{new_commit[:8]}[/cyan] — triggering update..."
                        )
                        self._trigger_update()
                        self._config.record_run(new_commit)

                time.sleep(self._poll_interval)

        except KeyboardInterrupt:
            self._running = False
            self.console.print("\n[dim]Scheduler stopped.[/dim]")

    def stop(self):
        self._running = False

    def _check_new_commits(self, branch: str) -> Optional[str]:
        """Check if there are new commits on the specified branch. Returns new HEAD or None."""
        config = self._config.load()
        last_checked = config.get("last_commit_checked")

        try:
            # Fetch latest (non-blocking)
            subprocess.run(
                ["git", "fetch", "origin", branch],
                cwd=self.target_path,
                capture_output=True,
                timeout=30,
            )

            # Get current HEAD of the branch
            result = subprocess.run(
                ["git", "rev-parse", f"origin/{branch}"],
                cwd=self.target_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                # Try without origin/ prefix for local branch
                result = subprocess.run(
                    ["git", "rev-parse", branch],
                    cwd=self.target_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            if result.returncode != 0:
                return None

            current_head = result.stdout.strip()

            if last_checked and current_head == last_checked:
                return None

            # Update last checked
            config["last_commit_checked"] = current_head
            self._config.save(config)

            # Only return if we had a previous reference (avoid triggering on first run)
            if last_checked:
                return current_head

            return None

        except (subprocess.TimeoutExpired, OSError):
            return None

    def _trigger_update(self):
        """Trigger a documentation update by invoking codilay CLI."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "codilay", self.target_path],
                cwd=self.target_path,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minute timeout
            )

            if result.returncode == 0:
                self.console.print(f"  [green]Update completed successfully.[/green]")
                self._config.record_run()
            else:
                self.console.print(f"  [red]Update failed (exit code {result.returncode})[/red]")
                if self.verbose and result.stderr:
                    self.console.print(f"  [dim]{result.stderr[:500]}[/dim]")

        except subprocess.TimeoutExpired:
            self.console.print("  [red]Update timed out (10 min limit).[/red]")
        except Exception as e:
            self.console.print(f"  [red]Update error: {e}[/red]")


# ── PID file management ──────────────────────────────────────────────────────


def write_pid_file(output_dir: str) -> str:
    """Write current PID to a file. Returns the pid file path."""
    pid_path = os.path.join(output_dir, ".scheduler.pid")
    with open(pid_path, "w") as f:
        f.write(str(os.getpid()))
    return pid_path


def read_pid_file(output_dir: str) -> Optional[int]:
    """Read the PID from the pid file. Returns None if not found or stale."""
    pid_path = os.path.join(output_dir, ".scheduler.pid")
    if not os.path.exists(pid_path):
        return None
    try:
        with open(pid_path, "r") as f:
            pid = int(f.read().strip())
        # Check if process is still running
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        # Stale pid file
        try:
            os.remove(pid_path)
        except OSError:
            pass
        return None


def remove_pid_file(output_dir: str):
    """Remove the PID file."""
    pid_path = os.path.join(output_dir, ".scheduler.pid")
    try:
        os.remove(pid_path)
    except OSError:
        pass
