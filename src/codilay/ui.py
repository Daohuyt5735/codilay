"""UI — rich terminal interface for CodiLay with git awareness."""

import fnmatch
from contextlib import contextmanager

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from codilay.error_tracker import ErrorTracker, Severity
from codilay.pricing import format_cost


class UI:
    def __init__(self, console: Console, verbose: bool = False):
        self.console = console
        self.verbose = verbose

    def show_banner(self):
        banner = r"""
 ██████╗  ██████╗ ██████╗ ██╗██╗      █████╗ ██╗   ██╗
██╔════╝ ██╔═══██╗██╔══██╗██║██║     ██╔══██╗╚██╗ ██╔╝
██║      ██║   ██║██║  ██║██║██║     ███████║  ╚████╔╝
██║      ██║   ██║██║  ██║██║██║     ██╔══██║   ╚██╔╝
╚██████╗ ╚██████╔╝██████╔╝██║███████╗██║  ██║    ██║
 ╚═════╝  ╚═════╝ ╚═════╝ ╚═╝╚══════╝╚═╝  ╚═╝     ╚═╝
"""
        self.console.print(
            Panel(
                f"[bold cyan]{banner}[/bold cyan]\n  [dim]AI Agent for Codebase Documentation[/dim]",
                border_style="cyan",
                padding=(0, 2),
            )
        )

    def show_config(self, config):
        table = Table(title="Configuration", box=box.SIMPLE, show_header=False)
        table.add_column("Key", style="cyan", width=20)
        table.add_column("Value", style="white")

        table.add_row("Target", config.target_path)
        table.add_row("LLM Provider", config.llm_provider)
        table.add_row("LLM Model", config.llm_model or "[dim]auto (provider default)[/dim]")
        table.add_row("Max Tokens/Call", str(config.max_tokens_per_call))
        if getattr(config, "llm_base_url", None):
            table.add_row("Base URL", config.llm_base_url)
        if config.notes:
            short = config.notes[:80] + ("…" if len(config.notes) > 80 else "")
            table.add_row("Notes", short)
        if config.instructions:
            short = config.instructions[:80] + ("…" if len(config.instructions) > 80 else "")
            table.add_row("Instructions", short)
        if config.entry_hint:
            table.add_row("Entry Hint", config.entry_hint)
        table.add_row("Ignore Patterns", f"{len(config.ignore_patterns)} custom patterns")

        self.console.print(table)
        self.console.print()

    def phase(self, title: str):
        self.console.print()
        self.console.print(
            Panel(
                f"[bold white]{title}[/bold white]",
                border_style="blue",
                padding=(0, 2),
            )
        )

    def info(self, msg: str):
        self.console.print(f"  [blue]ℹ[/blue]  {msg}")

    def success(self, msg: str):
        self.console.print(f"  [green]✓[/green]  {msg}")

    def warn(self, msg: str):
        self.console.print(f"  [yellow]⚠[/yellow]  {msg}")

    def error(self, msg: str):
        self.console.print(f"  [red]✗[/red]  {msg}")

    def debug(self, msg: str):
        if self.verbose:
            self.console.print(f"  [dim]{msg}[/dim]")

    def file_processed(
        self,
        path: str,
        new_section: str = None,
        wires_closed: int = 0,
        wires_opened: int = 0,
    ):
        parts = [f"  [green]✓[/green] [bold]{path}[/bold]"]
        details = []
        if new_section:
            details.append(f"section: [cyan]{new_section}[/cyan]")
        if wires_closed:
            details.append(f"[green]↓{wires_closed} closed[/green]")
        if wires_opened:
            details.append(f"[yellow]↑{wires_opened} opened[/yellow]")
        if details:
            parts.append(" (" + ", ".join(details) + ")")
        self.console.print("".join(parts))

    @contextmanager
    def spinner(self, text: str):
        with self.console.status(f"[bold blue]{text}[/bold blue]", spinner="dots"):
            yield

    def show_file_tree(self, tree_text: str):
        self.console.print(
            Panel(
                tree_text,
                title="[bold]File Tree[/bold]",
                border_style="dim",
                padding=(1, 2),
            )
        )

    def show_plan(self, queue: list, parked: list, skeleton: dict):
        self.console.print()

        table = Table(title="Processing Queue", box=box.SIMPLE_HEAVY)
        table.add_column("#", style="dim", width=4)
        table.add_column("File", style="white")

        display_count = min(len(queue), 20)
        for i, f in enumerate(queue[:display_count]):
            table.add_row(str(i + 1), f)
        if len(queue) > display_count:
            table.add_row("…", f"(+{len(queue) - display_count} more files)")

        self.console.print(table)

        if parked:
            self.console.print(f"\n  [yellow]Parked ({len(parked)}):[/yellow]")
            for f in parked[:10]:
                self.console.print(f"    [dim]• {f}[/dim]")

        if skeleton:
            title = skeleton.get("doc_title", "Untitled")
            sections = skeleton.get("suggested_sections", [])
            self.console.print(f"\n  [cyan]Doc Title:[/cyan] {title}")
            if sections:
                self.console.print(f"  [cyan]Sections:[/cyan] {', '.join(sections)}")

    # ── Git-aware re-run prompt ──────────────────────────────────

    def prompt_rerun_mode_git(self, diff_result) -> str:
        """
        Git-aware re-run prompt that shows exactly what changed.
        diff_result is a GitDiffResult object.
        """
        self.console.print()

        # Build the change summary
        change_lines = diff_result.summary_lines
        change_text = "\n".join(change_lines) if change_lines else "  [dim]No changes detected[/dim]"

        # Commit info
        commits_text = ""
        if diff_result.commits_behind > 0:
            commits_text = f"\n\n[dim]Commits since last doc ({diff_result.commits_behind}):[/dim]\n"
            for msg in diff_result.commit_messages[:10]:
                commits_text += f"  [dim]{msg}[/dim]\n"
            if len(diff_result.commit_messages) > 10:
                extra = len(diff_result.commit_messages) - 10
                commits_text += f"  [dim]… +{extra} more commits[/dim]\n"

        panel_text = (
            f"[bold]CodiLay found existing documentation.[/bold]\n"
            f"Last documented commit: [cyan]{diff_result.base_commit[:8]}[/cyan] "
            f"({diff_result.commits_behind} commits behind HEAD)\n"
            f"\n[bold]Changed files since last run:[/bold]\n"
            f"{change_text}"
            f"{commits_text}\n"
            f"What would you like to do?\n\n"
            f"  [cyan][1][/cyan] Update based on git changes  "
            f"[dim](recommended — {len(diff_result.files_to_process)} files)[/dim]\n"
            f"  [cyan][2][/cyan] Process specific files\n"
            f"  [cyan][3][/cyan] Full re-run from scratch\n"
            f"  [cyan][q][/cyan] Quit"
        )

        self.console.print(
            Panel(
                panel_text,
                border_style="yellow",
                title="[bold yellow]Existing Documentation Found[/bold yellow]",
            )
        )

        choice = Prompt.ask("Choice", choices=["1", "2", "3", "q"], default="1")
        return {"1": "git_update", "2": "specific", "3": "full", "q": "quit"}.get(choice, "quit")

    def prompt_interrupted_run(self, state, cost_so_far: float = 0.0) -> str:
        """Prompt user when an interrupted run is detected.

        Shows what was saved, the cost spent, and options to resume vs re-run.
        """
        self.console.print()

        processed = len(state.processed)
        remaining = len(state.queue)
        total = processed + remaining
        parked = len(state.parked)

        cost_line = ""
        if cost_so_far > 0:
            cost_line = f"  • Spent so far:  [yellow]{format_cost(cost_so_far)}[/yellow]\n"

        failed_line = ""
        if parked:
            failed_line = f"  • Parked files: [yellow]{parked}[/yellow] (will retry with context)\n"

        panel_text = (
            f"[bold]CodiLay detected an incomplete run.[/bold]\n\n"
            f"  • Processed:    [green]{processed}[/green] files saved\n"
            f"  • Remaining:    [yellow]{remaining}[/yellow] files to go\n"
            f"  • Total planned: {total} files\n"
            f"{failed_line}"
            f"{cost_line}\n"
            f"Resuming will pick up where the run stopped — no re-processing of\n"
            f"already-documented files and [bold]no extra cost[/bold] for those.\n\n"
            "What would you like to do?\n\n"
            "  [cyan][1][/cyan] Resume from checkpoint  [dim](recommended)[/dim]\n"
            "  [cyan][2][/cyan] Full re-run from scratch\n"
            "  [cyan][q][/cyan] Quit"
        )

        self.console.print(
            Panel(
                panel_text,
                border_style="orange3",
                title="[bold orange3]Incomplete Run Found[/bold orange3]",
            )
        )

        choice = Prompt.ask("Choice", choices=["1", "2", "q"], default="1")
        return {"1": "resume", "2": "full", "q": "quit"}.get(choice, "quit")

    def prompt_rerun_mode(self) -> str:
        """Fallback re-run prompt when git is not available."""
        self.console.print()
        self.console.print(
            Panel(
                "[bold]CodiLay found an existing doc for this project.[/bold]\n\n"
                "What would you like to do?\n\n"
                "  [cyan][1][/cyan] Update changed files only  "
                "[dim](recommended)[/dim]\n"
                "  [cyan][2][/cyan] Process specific files\n"
                "  [cyan][3][/cyan] Full re-run from scratch\n"
                "  [cyan][q][/cyan] Quit",
                border_style="yellow",
                title="[bold yellow]Existing Documentation Found[/bold yellow]",
            )
        )
        choice = Prompt.ask("Choice", choices=["1", "2", "3", "q"], default="1")
        return {"1": "update", "2": "specific", "3": "full", "q": "quit"}.get(choice, "quit")

    def prompt_specific_files(self, all_files: list) -> list:
        self.console.print("\nEnter file paths (one per line, empty line to finish):")
        self.console.print("[dim]You can also use glob patterns like 'src/*.py'[/dim]")

        files = []
        while True:
            line = Prompt.ask("  File", default="")
            if not line:
                break
            if "*" in line or "?" in line:
                matched = [f for f in all_files if fnmatch.fnmatch(f, line)]
                files.extend(matched)
                self.console.print(f"  [dim]Matched {len(matched)} files[/dim]")
            elif line in all_files:
                files.append(line)
            else:
                matches = [f for f in all_files if line in f]
                if matches:
                    self.console.print(f"  [dim]Did you mean: {', '.join(matches[:5])}?[/dim]")
                    files.extend(matches)
                else:
                    self.console.print(f"  [yellow]File not found: {line}[/yellow]")

        return list(set(files))

    def show_git_changes_applied(
        self,
        renames: int,
        deletes: int,
        invalidated: int,
        wires_reopened: int,
    ):
        """Show summary of git change processing."""
        self.console.print()
        table = Table(
            title="Git Changes Applied",
            box=box.SIMPLE,
            show_header=False,
        )
        table.add_column("Action", style="cyan")
        table.add_column("Count", style="bold white")

        if renames:
            table.add_row("Files renamed (paths updated)", str(renames))
        if deletes:
            table.add_row("Files deleted (sections marked)", str(deletes))
        if invalidated:
            table.add_row("Sections invalidated", str(invalidated))
        if wires_reopened:
            table.add_row("Wires re-opened", str(wires_reopened))

        self.console.print(table)

    def show_summary(
        self,
        processed_count: int,
        wires_closed: int,
        wires_open: int,
        sections: int,
        output_path: str,
        links_path: str,
        error_tracker: "ErrorTracker | None" = None,
        cost_usd: float = 0.0,
    ):
        self.console.print()

        table = Table(
            title="[bold green]Documentation Complete[/bold green]",
            box=box.DOUBLE,
        )
        table.add_column("Metric", style="cyan", width=25)
        table.add_column("Value", style="bold white")

        table.add_row("Files processed", str(processed_count))
        table.add_row("Documentation sections", str(sections))
        table.add_row("Wires closed (resolved)", f"[green]{wires_closed}[/green]")
        table.add_row(
            "Wires open (unresolved)",
            f"[yellow]{wires_open}[/yellow]" if wires_open else "[green]0[/green]",
        )
        if cost_usd > 0:
            table.add_row("Estimated cost", format_cost(cost_usd))
        table.add_row("Output", output_path)
        table.add_row("Links", links_path)

        self.console.print(table)
        self.console.print()
        self.console.print(f"  [bold green]✓[/bold green] Documentation written to [bold]{output_path}[/bold]")

        # One-line error/warning summary beneath the table
        if error_tracker is not None:
            counts = error_tracker.counts()
            warnings = counts.get(Severity.WARNING, 0)
            skipped = counts.get(Severity.SKIPPED, 0)
            errors = counts.get(Severity.CRITICAL, 0)

            parts = []
            if errors:
                parts.append(f"[red]{errors} error{'s' if errors != 1 else ''}[/red]")
            if warnings:
                parts.append(f"[yellow]{warnings} warning{'s' if warnings != 1 else ''}[/yellow]")
            if skipped:
                parts.append(f"[dim]{skipped} skipped[/dim]")

            if parts:
                summary_line = "  " + "  ".join(parts)
                if error_tracker.has_issues():
                    summary_line += "  [dim]— run `codilay status` for details[/dim]"
                self.console.print(summary_line)

        self.console.print()

    def show_next_steps(self, output_path: str, target: str):
        """Show actionable next-step commands after a successful run."""
        self.console.print()
        self.console.print(
            Panel(
                "  [bold cyan][1][/bold cyan]  Browse docs      [dim]codilay serve .[/dim]\n"
                "  [bold cyan][2][/bold cyan]  Chat with code   [dim]codilay chat .[/dim]\n"
                "  [bold cyan][3][/bold cyan]  Export for AI    [dim]codilay export . --format compact[/dim]\n"
                "  [bold cyan][4][/bold cyan]  Auto-update      [dim]codilay watch .[/dim]\n"
                "  [bold cyan][5][/bold cyan]  Keep up to date  [dim]codilay schedule set . '0 2 * * *'[/dim]\n"
                f"  [bold cyan][6][/bold cyan]  Commit docs      [dim]git add codilay/CODEBASE.md && git commit[/dim]",
                title="[bold]What's next?[/bold]",
                border_style="green",
                padding=(0, 2),
            )
        )

    def show_error_panel(self, error_tracker: "ErrorTracker"):
        """Display a persistent error/warning panel if the run had any issues."""
        if error_tracker.is_empty():
            return

        counts = error_tracker.counts()
        errors = counts.get(Severity.CRITICAL, 0)
        warnings = counts.get(Severity.WARNING, 0)
        skipped = counts.get(Severity.SKIPPED, 0)

        if not errors and not warnings and not skipped:
            return

        self.console.print()

        # Build the panel body
        lines = []
        for entry in error_tracker.entries:
            if entry.severity == Severity.CRITICAL:
                badge = "[bold red]CRITICAL[/bold red]"
            elif entry.severity == Severity.WARNING:
                badge = "[bold yellow]WARNING[/bold yellow]"
            elif entry.severity == Severity.SKIPPED:
                badge = "[bold dim]SKIPPED[/bold dim]"
            else:
                badge = "[dim]INFO[/dim]"

            file_part = f" [dim]{entry.file}[/dim]" if entry.file else ""
            lines.append(f"  {badge}{file_part}")
            lines.append(f"    What:   {entry.what}")
            if entry.why:
                lines.append(f"    Why:    [dim]{entry.why}[/dim]")
            if entry.action:
                lines.append(f"    Action: [cyan]{entry.action}[/cyan]")
            lines.append("")

        border = "red" if errors else "yellow"
        title_parts = []
        if errors:
            title_parts.append(f"[red]{errors} error{'s' if errors != 1 else ''}[/red]")
        if warnings:
            title_parts.append(f"[yellow]{warnings} warning{'s' if warnings != 1 else ''}[/yellow]")
        if skipped:
            title_parts.append(f"[dim]{skipped} skipped[/dim]")
        title = "  ".join(title_parts)

        self.console.print(
            Panel(
                "\n".join(lines).rstrip(),
                title=f"[bold]Run Issues — {title}[/bold]",
                border_style=border,
                padding=(0, 1),
            )
        )
        # ── Triage display methods ───────────────────────────────────

    def show_triage_result(self, triage_result, project_type: str):
        """Display the triage classification for user review."""
        self.console.print()

        # Project type
        self.console.print(f"  [cyan]Project type:[/cyan] [bold]{project_type}[/bold]")

        if triage_result.reasoning:
            self.console.print(f"  [dim]{triage_result.reasoning}[/dim]")

        self.console.print()

        # Skip summary — group by top-level directory
        if triage_result.skip:
            skip_dirs = {}
            skip_files = []
            for f in triage_result.skip:
                parts = f.split("/")
                if len(parts) > 1:
                    top_dir = parts[0] + "/"
                    skip_dirs[top_dir] = skip_dirs.get(top_dir, 0) + 1
                else:
                    skip_files.append(f)

            self.console.print(f"  [red]SKIP[/red] [dim](generated/platform — {len(triage_result.skip)} files):[/dim]")
            for dir_name, count in sorted(skip_dirs.items(), key=lambda x: -x[1])[:15]:
                self.console.print(f"    [dim]✗ {dir_name}[/dim] [dim]({count} files)[/dim]")
            for f in skip_files[:5]:
                self.console.print(f"    [dim]✗ {f}[/dim]")
            if len(skip_dirs) > 15 or len(skip_files) > 5:
                self.console.print("    [dim]… and more[/dim]")

        # Skim summary
        if triage_result.skim:
            self.console.print(
                f"\n  [yellow]SKIM[/yellow] [dim](config/metadata — {len(triage_result.skim)} files):[/dim]"
            )
            for f in triage_result.skim[:10]:
                self.console.print(f"    [dim]~ {f}[/dim]")
            if len(triage_result.skim) > 10:
                self.console.print(f"    [dim]… +{len(triage_result.skim) - 10} more[/dim]")

        # Core summary
        if triage_result.core:
            core_dirs = {}
            for f in triage_result.core:
                parts = f.split("/")
                if len(parts) > 1:
                    top_dir = parts[0] + "/"
                    core_dirs[top_dir] = core_dirs.get(top_dir, 0) + 1
                else:
                    core_dirs[f] = 1

            self.console.print(f"\n  [green]CORE[/green] [dim](will document — {len(triage_result.core)} files):[/dim]")
            for dir_name, count in sorted(core_dirs.items(), key=lambda x: -x[1])[:15]:
                self.console.print(f"    [green]✓[/green] {dir_name} [dim]({count} files)[/dim]")
            if len(core_dirs) > 15:
                self.console.print("    [dim]… and more[/dim]")

        # Token savings estimate
        if triage_result.token_estimate_saved > 0:
            saved_k = triage_result.token_estimate_saved / 1000
            self.console.print(
                f"\n  [green]Estimated tokens saved:[/green] "
                f"~{saved_k:.0f}K tokens by skipping {len(triage_result.skip)} files"
            )

    def show_triage_warnings(self, warnings):
        """Display warnings from triage (AI suggestions or borderline files)."""
        if not warnings:
            return
        self.console.print()
        self.console.print(
            Panel(
                "[bold yellow]Some files may require review:[/bold yellow]",
                border_style="yellow",
                title="[bold yellow]Warnings[/bold yellow]",
                padding=(0, 2),
            )
        )
        for warning in warnings:
            self.console.print(f"  [yellow]⚠[/yellow] {warning}")
        self.console.print()

    def prompt_triage_review(self) -> str:
        """
        Ask user to confirm or edit triage classification.
        Returns: 'proceed', 'edit', or 'skip_triage'
        """
        self.console.print()
        self.console.print(
            Panel(
                "  [cyan][y][/cyan] Proceed with this classification  "
                "[dim](recommended)[/dim]\n"
                "  [cyan][e][/cyan] Edit — move files between categories\n"
                "  [cyan][a][/cyan] Skip triage — process ALL files\n"
                "  [cyan][q][/cyan] Quit",
                border_style="cyan",
                title="[bold cyan]Review File Classification[/bold cyan]",
            )
        )

        choice = Prompt.ask("Choice", choices=["y", "e", "a", "q"], default="y")
        return {
            "y": "proceed",
            "e": "edit",
            "a": "skip_triage",
            "q": "quit",
        }.get(choice, "proceed")

    def prompt_triage_edit(self, triage_result) -> None:
        """Interactive editor for triage classifications."""
        import fnmatch

        self.console.print()
        self.console.print("[bold]Edit file classifications[/bold]")
        self.console.print(
            "[dim]Commands: 'core <pattern>' | 'skip <pattern>' | 'skim <pattern>' | 'list <category>' | 'done'[/dim]"
        )
        self.console.print("[dim]Patterns: exact path or glob (e.g., 'ios/AppDelegate.swift' or 'ios/*.swift')[/dim]")

        all_files = triage_result.core + triage_result.skim + triage_result.skip

        while True:
            cmd = Prompt.ask("\n  Edit", default="done")
            cmd = cmd.strip()

            if cmd == "done" or cmd == "":
                break

            parts = cmd.split(None, 1)
            if len(parts) < 2 and parts[0] != "done":
                if parts[0] == "list":
                    self.console.print("[dim]Usage: list core|skim|skip[/dim]")
                    continue
                self.console.print("[dim]Usage: core|skip|skim <pattern> | list <category> | done[/dim]")
                continue

            action = parts[0].lower()
            pattern = parts[1] if len(parts) > 1 else ""

            if action == "list":
                cat = pattern.lower()
                if cat == "core":
                    files = triage_result.core
                elif cat == "skim":
                    files = triage_result.skim
                elif cat == "skip":
                    files = triage_result.skip
                else:
                    self.console.print("[dim]list core|skim|skip[/dim]")
                    continue
                for f in files[:30]:
                    self.console.print(f"    {f}")
                if len(files) > 30:
                    self.console.print(f"    [dim]… +{len(files) - 30} more[/dim]")
                continue

            # Match files
            if "*" in pattern or "?" in pattern:
                matched = [f for f in all_files if fnmatch.fnmatch(f, pattern)]
            elif pattern.endswith("/"):
                matched = [f for f in all_files if f.startswith(pattern)]
            else:
                matched = [f for f in all_files if f == pattern or f.startswith(pattern + "/")]

            if not matched:
                self.console.print(f"  [yellow]No files matched: {pattern}[/yellow]")
                continue

            if action == "core":
                triage_result.move_to_core(matched)
                self.console.print(f"  [green]→ Moved {len(matched)} files to CORE[/green]")
            elif action == "skip":
                triage_result.move_to_skip(matched)
                self.console.print(f"  [red]→ Moved {len(matched)} files to SKIP[/red]")
            elif action == "skim":
                triage_result.move_to_skim(matched)
                self.console.print(f"  [yellow]→ Moved {len(matched)} files to SKIM[/yellow]")
            else:
                self.console.print("[dim]Unknown action. Use: core, skip, skim, list, done[/dim]")
