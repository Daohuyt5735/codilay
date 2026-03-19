"""
CodiLay CLI — the main entry point with git-aware change tracking.

Usage:
    codilay                              Interactive menu
    codilay .                            Document current directory
    codilay /path/to/project             Document a specific project
    codilay . --provider openai          Use OpenAI
    codilay setup                        First-time setup wizard
    codilay config                       View settings
    codilay keys                         Manage API keys
    codilay status .                     Show doc status
    codilay clean .                      Remove generated files

Tools & Automation:
    codilay watch .                      Watch mode — auto-update on save
    codilay export . --for-ai            AI-optimized doc export
    codilay diff-doc .                   Doc-level diff between versions
    codilay diff-run . --since abc123f   Document changes since boundary
    codilay search . -q 'query'          Search past conversations
    codilay schedule set . '0 2 * * *'   Cron-based auto re-runs
    codilay graph .                      Filtered dependency graph

Collaboration:
    codilay team facts .                 View shared team facts
    codilay team add-fact .              Add a team fact
    codilay triage-feedback list .       View triage feedback
"""

import json
import os
from datetime import datetime, timezone

import click
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.table import Table

from codilay.config import CodiLayConfig
from codilay.docstore import DocStore
from codilay.error_tracker import ErrorTracker
from codilay.git_tracker import ChangeType, GitTracker
from codilay.llm_client import ALL_PROVIDERS, AuthenticationError, LLMClient
from codilay.parallel_orchestrator import ParallelOrchestrator
from codilay.planner import Planner
from codilay.pricing import format_cost
from codilay.processor import Processor
from codilay.scanner import Scanner
from codilay.settings import Settings
from codilay.state import AgentState
from codilay.ui import UI
from codilay.wire_bus import WireBus
from codilay.wire_manager import WireManager

console = Console()


def common_options(fn):
    fn = click.option("--config", "-c", default=None, help="Path to codilay.config.json")(fn)
    fn = click.option("--output", "-o", default=None, help="Output directory")(fn)
    fn = click.option("--model", "-m", default=None, help="LLM model override")(fn)
    fn = click.option(
        "--provider",
        "-p",
        default=None,
        type=click.Choice(ALL_PROVIDERS),
        help="LLM provider",
    )(fn)
    fn = click.option("--base-url", default=None, help="Custom LLM API base URL")(fn)
    fn = click.option("--verbose", "-v", is_flag=True, help="Verbose output")(fn)
    return fn


class CodiLayGroup(click.Group):
    """
    Custom group that resolves ambiguity between a target path and
    subcommand names.

    - ``codilay``              → interactive menu
    - ``codilay setup``        → setup subcommand
    - ``codilay .``            → auto-dispatches to ``run .``
    - ``codilay /some/path``   → auto-dispatches to ``run /some/path``
    """

    def resolve_command(self, ctx, args):
        """
        Called AFTER the group's own options have been consumed.
        ``args`` contains only the remaining positional tokens.

        If the first token is NOT a known subcommand, prepend ``run``
        so Click routes the path as an argument to the ``run`` command.
        """
        if args:
            cmd_name = args[0]
            if cmd_name not in self.commands and not cmd_name.startswith("-"):
                args = ["run"] + args
        return super().resolve_command(ctx, args)


@click.group(
    cls=CodiLayGroup,
    invoke_without_command=True,
)
@common_options
@click.pass_context
def cli(ctx, config, output, model, provider, base_url, verbose):
    """
    CodiLay — AI Agent for Codebase Documentation.

    \b
    Run with no arguments for the interactive menu, or pass a
    target path to document directly.

    \b
    Examples:
        codilay                          Interactive menu
        codilay .                        Document current directory
        codilay /path/to/project         Document a specific project
        codilay . -p openai -m gpt-4o    Use OpenAI
        codilay . -p gemini              Use Google Gemini
        codilay . -p ollama              Use local Ollama
        codilay . -p groq                Use Groq
        codilay . -p custom --base-url https://my-llm.com/v1 -m my-model
        codilay . -v                     Verbose mode
        codilay setup                    First-time setup wizard
        codilay config                   View current settings
        codilay keys                     Manage API keys

    \b
    Tools & Automation:
        codilay watch .                  Watch mode — auto-update on save
        codilay export . --for-ai        AI-optimized doc export
        codilay diff-doc .               Doc diff between versions
        codilay search . -q 'query'      Search past conversations
        codilay schedule set . CRON      Cron-based auto re-runs
        codilay graph .                  Filtered dependency graph
        codilay team facts .             View shared team knowledge
        codilay triage-feedback list .   View triage feedback
    """
    # ── Load persistent settings & inject API keys into env ────────
    settings = Settings.load()
    settings.inject_env_vars()

    ctx.ensure_object(dict)
    ctx.obj["settings"] = settings
    ctx.obj["config_path"] = config
    ctx.obj["output"] = output
    ctx.obj["model"] = model
    # Fall back to stored default if no CLI flag given
    ctx.obj["provider"] = provider
    ctx.obj["base_url"] = base_url or settings.custom_base_url
    ctx.obj["verbose"] = verbose or settings.verbose

    if ctx.invoked_subcommand is None:
        # No target and no subcommand → launch interactive menu
        ctx.invoke(interactive)


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option(
    "--scope",
    "-s",
    multiple=True,
    default=None,
    help=(
        "Restrict documentation to specific files, folders or glob patterns. "
        "Can be supplied multiple times: "
        "--scope src/auth/ --scope src/middleware/auth.py  "
        "Wires that point outside the scope are marked 'out-of-scope' rather "
        "than 'unresolved'."
    ),
)
@click.pass_context
def run(ctx, target, scope):
    """Run the documentation agent (default command)."""
    settings: Settings = ctx.obj["settings"]
    target = os.path.abspath(target)
    config_path = ctx.obj["config_path"]
    output_dir = ctx.obj["output"]
    model_override = ctx.obj["model"]
    provider = ctx.obj["provider"] or settings.default_provider
    base_url = ctx.obj["base_url"]
    verbose = ctx.obj["verbose"]

    # Normalise scope: a tuple of patterns/paths from --scope flags
    scope_patterns = list(scope) if scope else []

    ui = UI(console, verbose)
    ui.show_banner()

    # ── Error tracker for this run ────────────────────────────────
    error_tracker = ErrorTracker()

    # ── Load Config ──────────────────────────────────────────────
    cfg = CodiLayConfig.load(target, config_path)

    # Smart provider/model override:
    # - If provider is switched via CLI and no model given, reset model
    #   so the new provider's default kicks in.
    # - If neither is given, config file values are used as-is.
    if provider is not None:
        if model_override is None and provider != cfg.llm_provider:
            cfg.llm_model = None  # let provider default kick in
        cfg.llm_provider = provider
    if model_override:
        cfg.llm_model = model_override
    if base_url:
        cfg.llm_base_url = base_url

    # Apply global preferences for parallel processing.
    # Settings act as user-level defaults; project config overrides them
    # (CodiLayConfig.load already applied project config values).
    # We only override if the user has explicitly toggled parallel off in prefs.
    if not settings.parallel:
        cfg.parallel = False
    if settings.max_workers != 4:  # non-default means user changed it
        cfg.max_workers = settings.max_workers

    # Apply global large-file threshold preference if set
    if settings.large_file_threshold is not None:
        cfg.chunk_token_threshold = settings.large_file_threshold

    # Store scope patterns on the config so downstream components can see them
    cfg.scope_patterns = scope_patterns

    # Apply documentation style preferences from global settings
    cfg.response_style = settings.response_style
    cfg.detail_level = settings.detail_level
    cfg.include_examples = settings.include_examples

    # Apply reasoning preferences from global settings
    if settings.reasoning_enabled:
        cfg.thinking_budget_tokens = settings.reasoning_budget_tokens
        cfg.reasoning_effort = settings.reasoning_effort

    ui.show_config(cfg)

    # ── Resolve paths ────────────────────────────────────────────
    if output_dir is None:
        # Honour the global doc_output_location preference
        if settings.doc_output_location == "docs":
            output_dir = os.path.join(target, "docs")
        else:
            # "codilay" and "local" both write to <project>/codilay/
            output_dir = os.path.join(target, "codilay")

    state_path = os.path.join(output_dir, ".codilay_state.json")
    codebase_md_path = os.path.join(output_dir, "CODEBASE.md")
    lock_path = os.path.join(output_dir, ".codilay.lock")
    os.makedirs(output_dir, exist_ok=True)

    # ── Concurrent-run prevention ─────────────────────────────────
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as lf:
                lock_pid = lf.read().strip()
        except OSError:
            lock_pid = "unknown"
        # Check if the PID is still alive before blocking
        lock_alive = False
        try:
            if lock_pid.isdigit():
                import signal

                os.kill(int(lock_pid), 0)
                lock_alive = True
        except (OSError, ProcessLookupError):
            pass

        if lock_alive:
            console.print(
                Panel(
                    f"[bold red]Another CodiLay run is already in progress for this project.[/bold red]\n\n"
                    f"PID: [cyan]{lock_pid}[/cyan]\n"
                    f"Lock file: [dim]{lock_path}[/dim]\n\n"
                    "Wait for the other run to finish, or if it crashed, delete the lock file:\n"
                    f"  [dim]rm {lock_path}[/dim]",
                    title="[bold red]Concurrent Run Blocked[/bold red]",
                    border_style="red",
                )
            )
            return
        else:
            # Stale lock from a crashed run — remove it
            try:
                os.remove(lock_path)
                ui.warn("Removed stale lock file from a previous crashed run.")
            except OSError:
                pass

    # Write our own lock file and register cleanup on exit
    import atexit

    def _remove_lock():
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
        except OSError:
            pass

    try:
        with open(lock_path, "w") as lf:
            lf.write(str(os.getpid()))
        atexit.register(_remove_lock)
    except OSError as e:
        ui.warn(f"Could not create lock file: {e} — concurrent run protection disabled")

    # ── Git setup ────────────────────────────────────────────────
    git = GitTracker(target)
    if git.is_git_repo:
        current_commit = git.get_current_commit()
        current_commit_short = git.get_current_commit_short()
        ui.info(f"Git repo detected — HEAD: [cyan]{current_commit_short}[/cyan]")
    else:
        current_commit = None
        current_commit_short = None
        ui.info("Not a git repo — using file-based change detection")

    # ── Check existing state ─────────────────────────────────────
    existing_state = None
    mode = "full"
    diff_result = None

    if os.path.exists(state_path):
        existing_state = AgentState.load(state_path)
        is_completed = os.path.exists(codebase_md_path)

        if not is_completed or len(existing_state.queue) > 0:
            mode = ui.prompt_interrupted_run(existing_state)
        else:
            # Completed run found — check for changes
            if git.is_git_repo and existing_state.last_commit:
                if git.is_commit_valid(existing_state.last_commit):
                    diff_result = git.get_full_diff(existing_state.last_commit)

                    if diff_result and diff_result.changes:
                        mode = ui.prompt_rerun_mode_git(diff_result)
                    elif diff_result and not diff_result.changes:
                        ui.success(
                            "No changes since last documented commit "
                            f"([cyan]{existing_state.last_commit_short}[/cyan]). "
                            "Documentation is up to date!"
                        )
                        return
                    else:
                        mode = ui.prompt_rerun_mode()
                else:
                    ui.warn(
                        f"Last documented commit "
                        f"[cyan]{existing_state.last_commit_short}[/cyan] "
                        f"no longer exists (rebase/force push?)"
                    )
                    mode = ui.prompt_rerun_mode()
            else:
                mode = ui.prompt_rerun_mode()

        if mode == "quit":
            ui.info("Exiting.")
            return

        if mode == "full":
            bak = codebase_md_path + ".bak"
            if os.path.exists(codebase_md_path):
                os.rename(codebase_md_path, bak)
                ui.info(f"Archived existing doc → {bak}")
            existing_state = None

    # ── Init components ──────────────────────────────────────────
    llm = LLMClient(cfg)
    scanner = Scanner(target, cfg, output_dir=output_dir)
    wire_mgr = WireManager()
    docstore = DocStore()
    state = existing_state or AgentState(run_id=datetime.now(timezone.utc).isoformat())

    # Load existing context if not starting from scratch
    if mode != "full":
        wire_mgr.load_state(state.open_wires, state.closed_wires)
        docstore.load_from_state(state.section_index, state.section_contents)
        if mode == "resume":
            ui.info(f"Resuming run [cyan]{state.run_id}[/cyan]")

        # ── Phase 1: Bootstrap ───────────────────────────────────────
    ui.phase("Phase 1 · Bootstrap — Scanning codebase")

    with ui.spinner("Scanning files…"):
        file_tree_text = scanner.get_file_tree()
        all_files = scanner.get_all_files()
        md_contents = scanner.preload_md_files()

    ui.info(f"Found [bold]{len(all_files)}[/bold] files ({len(md_contents)} markdown preloaded)")
    if verbose:
        ui.show_file_tree(file_tree_text)

    # ── Phase 1.5: Triage ────────────────────────────────────────
    # (only on full runs or when processing all files)

    files_to_process = all_files
    triage_result = None
    added_files: set = set()  # populated in git_update mode to track newly-added files

    if mode == "full" and cfg.triage_mode != "none":
        ui.phase("Phase 1.5 · Triage — Classifying files to save tokens")

        from codilay.triage import Triage

        triage = Triage(llm_client=llm, config=cfg)

        if cfg.triage_mode == "smart":
            with ui.spinner("LLM is classifying files (tree only, no content)…"):
                triage_result = triage.smart_triage(file_tree_text, all_files, md_contents)
        else:
            with ui.spinner("Classifying files by pattern…"):
                triage_result = triage.fast_triage(all_files)

        # Apply force_include / force_skip from config
        if cfg.force_include:
            force_matched = []
            for pattern in cfg.force_include:
                force_matched.extend(triage._expand_pattern(pattern, all_files))
            if force_matched:
                triage_result.move_to_core(force_matched)
                ui.info(f"Force-included {len(force_matched)} files from config")

        if cfg.force_skip:
            force_matched = []
            for pattern in cfg.force_skip:
                force_matched.extend(triage._expand_pattern(pattern, all_files))
            if force_matched:
                triage_result.move_to_skip(force_matched)
                ui.info(f"Force-skipped {len(force_matched)} files from config")

        # Handle test files
        if not cfg.include_tests:
            test_files = [
                f
                for f in triage_result.core
                if any(
                    p in f.lower()
                    for p in [
                        "test",
                        "spec",
                        "__tests__",
                        "_test.",
                        ".test.",
                        ".spec.",
                        "test_",
                        "tests/",
                    ]
                )
            ]
            if test_files:
                triage_result.move_to_skip(test_files)
                ui.info(f"Skipped {len(test_files)} test files (set triage.includeTests: true to include)")

        # Estimate savings
        triage_result.token_estimate_saved = triage.estimate_tokens_saved(triage_result.skip, target)

        # Apply stored triage feedback (Feature 5)
        try:
            from codilay.triage_feedback import TriageFeedbackStore

            feedback_store = TriageFeedbackStore(output_dir)
            overrides_applied = feedback_store.apply_to_triage(triage_result)
            if overrides_applied > 0:
                ui.info(f"Applied {overrides_applied} triage feedback overrides")
        except Exception:
            pass  # Non-critical

        # Show results and get user confirmation
        ui.show_triage_result(triage_result, triage_result.project_type)

        # Show warnings from AI (borderline files)
        if triage_result.warnings:
            ui.show_triage_warnings(triage_result.warnings)

        review = ui.prompt_triage_review()

        if review == "quit":
            ui.info("Exiting.")
            return

        elif review == "edit":
            ui.prompt_triage_edit(triage_result)
            ui.show_triage_result(triage_result, triage_result.project_type)

        elif review == "skip_triage":
            ui.info("Skipping triage — all files will be processed")
            triage_result = None

        if triage_result:
            files_to_process = triage_result.files_to_process
            ui.success(
                f"Triage complete: [bold]{len(triage_result.core)}[/bold] core + "
                f"[bold]{len(triage_result.skim)}[/bold] skim = "
                f"[bold]{len(files_to_process)}[/bold] files to process "
                f"(skipping {len(triage_result.skip)})"
            )

    # ── Apply --scope filtering ───────────────────────────────────
    # When the user specifies --scope patterns only the matching subset of
    # files enters the queue.  The unmatched files are remembered so that
    # wires pointing to them can be labelled "out-of-scope" rather than
    # "unresolved" in the final document.
    out_of_scope_files: set = set()
    if scope_patterns:
        import fnmatch

        def _file_matches_scope(rel_path: str) -> bool:
            for pat in scope_patterns:
                # Normalise: if pattern has no wildcard and looks like a dir,
                # match any file underneath it.
                norm = pat.rstrip("/")
                if fnmatch.fnmatch(rel_path, pat):
                    return True
                if fnmatch.fnmatch(rel_path, pat.rstrip("/") + "/*"):
                    return True
                # Plain prefix match (e.g. "src/auth" matches "src/auth/foo.py")
                if rel_path.startswith(norm + "/") or rel_path == norm:
                    return True
            return False

        in_scope = [f for f in files_to_process if _file_matches_scope(f)]
        out_of_scope_files = set(f for f in all_files if not _file_matches_scope(f))

        if not in_scope:
            ui.error(f"--scope filter matched no files.  Patterns: {', '.join(scope_patterns)}")
            return

        ui.info(
            f"Scope filter active — processing [bold]{len(in_scope)}[/bold] "
            f"of {len(files_to_process)} files "
            f"({len(out_of_scope_files)} out-of-scope)"
        )
        files_to_process = in_scope

    # Store out-of-scope set on the state so the wire manager can use it
    state_out_of_scope = out_of_scope_files

    # ── Determine scope based on mode (for non-full runs) ────────

    if mode == "git_update" and diff_result:
        # Git-aware incremental — triage already happened on first run,
        # only process changed files
        ui.phase("Applying git changes to existing documentation")

        rename_count = 0
        delete_count = 0

        for change in diff_result.renamed:
            old_path = change.old_path
            new_path = change.path
            ui.info(f"  Rename: {old_path} → [bold]{new_path}[/bold]")
            wire_mgr.handle_renamed_file(old_path, new_path)
            docstore.handle_renamed_file(old_path, new_path)
            if old_path in state.processed:
                state.processed.remove(old_path)
                state.processed.append(new_path)
            if old_path in state.parked:
                state.parked.remove(old_path)
                state.parked.append(new_path)
            rename_count += 1

        for change in diff_result.deleted:
            deleted_path = change.path
            ui.info(f"  Deleted: [red]{deleted_path}[/red]")
            wire_mgr.handle_deleted_file(deleted_path)
            docstore.handle_deleted_file(deleted_path)
            if deleted_path in state.processed:
                state.processed.remove(deleted_path)
            delete_count += 1

        added_files = {c.path for c in diff_result.added}
        files_to_process = diff_result.files_to_process
        valid_files = set(all_files)
        files_to_process = [f for f in files_to_process if f in valid_files]

        all_affected = diff_result.all_affected_paths
        wires_reopened = wire_mgr.reopen_wires_for_files(all_affected)
        invalidated = docstore.invalidate_sections_for_files(files_to_process)

        for parked_file in list(state.parked):
            wires_to_parked = wire_mgr.find_wires_to(parked_file)
            if any(w.get("from") in files_to_process for w in wires_to_parked):
                files_to_process.append(parked_file)
                state.parked.remove(parked_file)
                ui.info(f"  Unparked: {parked_file}")

        ui.show_git_changes_applied(
            renames=rename_count,
            deletes=delete_count,
            invalidated=len(invalidated),
            wires_reopened=wires_reopened,
        )

        if not files_to_process:
            ui.success("All changes were structural. Doc updated!")
            _finalize_and_write(
                state,
                wire_mgr,
                docstore,
                llm,
                cfg,
                ui,
                scanner,
                target,
                output_dir,
                codebase_md_path,
                state_path,
                git,
                current_commit,
                current_commit_short,
                out_of_scope_files=state_out_of_scope,
            )
            return

        ui.info(f"[bold]{len(files_to_process)}[/bold] files to re-process")

    elif mode == "update":
        changed = scanner.get_changed_files(state.processed)
        if not changed:
            ui.success("No changed files. Documentation is up to date!")
            return
        files_to_process = changed
        wires_reopened = wire_mgr.reopen_wires_for_files(changed)
        ui.info(f"Detected {len(changed)} changed files, re-opened {wires_reopened} wires")

    elif mode == "specific":
        specific = ui.prompt_specific_files(all_files)
        if not specific:
            ui.error("No valid files selected.")
            return
        files_to_process = specific
        wire_mgr.reopen_wires_for_files(specific)
        docstore.invalidate_sections_for_files(specific)

    # ── Phase 2: Planning ────────────────────────────────────────
    if mode == "git_update":
        # For git-aware incremental updates we skip LLM planning entirely.
        # We already know the exact set of changed files from the diff.
        # Put modified files first (updating existing docs) then newly added
        # files (creating new docs) — deterministic, no LLM call needed.
        ui.phase("Phase 2 · Planning — Ordering changed files (git mode)")
        modified_first = [f for f in files_to_process if f not in added_files]
        new_after = [f for f in files_to_process if f in added_files]
        state.queue = modified_first + new_after
        # Do NOT touch state.parked — parked files from the original run remain
        # parked unless a changed file now has a wire pointing to them (already
        # handled above in the git_update block).
        ui.show_plan(state.queue, state.parked, {})
    elif mode != "resume":
        ui.phase("Phase 2 · Planning — Determining processing order")

        planner = Planner(llm, cfg)

        with ui.spinner("LLM is analysing the file structure…"):
            plan = planner.plan(file_tree_text, md_contents, files_to_process, state)

        state.queue = plan["order"]
        state.parked = plan.get("parked", []) if mode == "full" else state.parked
        state.park_reasons = plan.get("park_reasons", {}) if mode == "full" else state.park_reasons
        skeleton = plan["skeleton"]

        ui.show_plan(state.queue, state.parked, skeleton)

        if mode == "full":
            docstore.initialize_skeleton(
                skeleton.get("doc_title", "Codebase Reference"),
                skeleton.get("suggested_sections", []),
            )
    else:
        # In resume mode, we already have a queue and skeleton in docstore
        ui.phase("Phase 2 · Resuming — Skipping planning")
        ui.show_plan(state.queue, state.parked, {})

    # ── Pre-run cost estimate ─────────────────────────────────────
    _show_cost_estimate(state.queue, llm, cfg, ui)

    # ── Phase 3: Processing Loop ─────────────────────────────────
    ui.phase("Phase 3 · Processing — Reading files and building docs")

    # Wrap WireManager in thread-safe WireBus for parallel processing
    wire_bus = WireBus(wire_mgr)
    processor = Processor(llm, cfg, wire_mgr, docstore, state, ui)

    total_files = len(state.queue)
    processed_count = 0

    use_parallel = cfg.parallel and total_files > 1

    if use_parallel:
        # ── Parallel tier-based processing ───────────────────────
        ui.info(f"  Parallel processing enabled (max {cfg.max_workers} workers)")

        # Pre-load all file contents for parallel access
        file_contents = {}
        for file_path in list(state.queue):
            full_path = os.path.join(target, file_path)
            if os.path.exists(full_path):
                content = scanner.read_file(full_path)
                if content is not None:
                    file_contents[file_path] = content

        orchestrator = ParallelOrchestrator(
            processor=processor,
            wire_bus=wire_bus,
            docstore=docstore,
            state=state,
            scanner=scanner,
            target_path=target,
            ui=ui,
            max_workers=cfg.max_workers,
        )

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("({task.completed}/{task.total})"),
            console=console,
        ) as progress:
            task = progress.add_task("Processing files", total=total_files)

            def on_progress(file_path, completed, total):
                progress.update(task, description=f"Processed: {file_path}")
                progress.update(task, completed=completed, total=total)

            try:
                parallel_result = orchestrator.process_all(
                    files_to_process=list(state.queue),
                    file_contents=file_contents,
                    progress_callback=on_progress,
                )

                # Show parallel processing stats
                pstats = parallel_result["stats"]
                dep_stats = parallel_result["dep_graph_stats"]
                ui.info(
                    f"  Parallel stats: {pstats['parallel_files']} parallel, "
                    f"{pstats['sequential_files']} sequential, "
                    f"{pstats['tier_count']} tiers, "
                    f"max parallelism: {dep_stats['max_parallelism']}"
                )
                if pstats["unparked_count"] > 0:
                    ui.info(f"  ↳ {pstats['unparked_count']} files auto-unparked")

            except Exception as e:
                ui.error(f"Parallel processing failed: {e}")
                if verbose:
                    console.print_exception()
                ui.warn("Falling back to sequential processing...")
                use_parallel = False

            # Save checkpoint after parallel processing
            orchestrator.save_checkpoint(state_path)
            orchestrator.cleanup()

    if not use_parallel:
        # ── Sequential processing (original loop / fallback) ─────
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("({task.completed}/{task.total})"),
            console=console,
        ) as progress:
            task = progress.add_task("Processing files", total=total_files)

            while state.queue:
                file_path = state.queue.pop(0)
                full_path = os.path.join(target, file_path)

                if not os.path.exists(full_path):
                    ui.warn(f"File not found, skipping: {file_path}")
                    error_tracker.skipped(
                        what=f"File not found: {file_path}",
                        why="File was deleted or moved after scan",
                        action="Skipped — run will continue",
                        file=file_path,
                    )
                    progress.advance(task)
                    continue

                progress.update(task, description=f"Processing: {file_path}")

                try:
                    content = scanner.read_file(full_path)
                    if content is None:
                        # Try to distinguish binary vs permission error
                        try:
                            with open(full_path, "rb") as _f:
                                _f.read(16)
                            why = "File appears to be binary"
                        except PermissionError:
                            why = "Permission denied"
                        except OSError as _oe:
                            why = str(_oe)
                        ui.warn(f"Could not read: {file_path}")
                        error_tracker.skipped(
                            what=f"Could not read: {file_path}",
                            why=why,
                            action="File skipped — run will continue",
                            file=file_path,
                        )
                        progress.advance(task)
                        continue

                    result = processor.process_file(file_path, content)

                    # Track processed files (avoid duplicates on re-runs)
                    if file_path not in state.processed:
                        state.processed.append(file_path)
                    processed_count += 1

                    # Store file hash for future diffing
                    file_hash = scanner.get_file_hash(full_path)
                    if file_hash:
                        state.file_hashes[file_path] = file_hash

                    # Check for unparked files
                    if result and result.get("unpark"):
                        for up in result["unpark"]:
                            if up in state.parked:
                                state.parked.remove(up)
                                state.queue.append(up)
                                total_files += 1
                                progress.update(task, total=total_files)
                                ui.info(f"  ↳ Unparked: {up}")

                    # Save after every file (crash recovery)
                    state.open_wires = wire_mgr.get_open_wires()
                    state.closed_wires = wire_mgr.get_closed_wires()
                    state.section_index = docstore.get_section_index()
                    state.section_contents = docstore.get_section_contents()
                    state.save(state_path)

                except AuthenticationError as e:
                    # Auth errors require user action — pause with a clear message
                    console.print(
                        Panel(
                            f"[bold red]API authentication failed.[/bold red]\n\n"
                            f"{e}\n\n"
                            "Progress has been saved. Fix your API key and resume:\n"
                            "  [cyan]codilay keys set[/cyan]\n"
                            "  [cyan]codilay .[/cyan]  (will offer to resume)",
                            title="[bold red]Authentication Error — Run Paused[/bold red]",
                            border_style="red",
                        )
                    )
                    error_tracker.critical(
                        what="API authentication failed",
                        why=str(e),
                        action="Run paused — fix key with `codilay keys set` then resume",
                    )
                    state.open_wires = wire_mgr.get_open_wires()
                    state.closed_wires = wire_mgr.get_closed_wires()
                    state.section_index = docstore.get_section_index()
                    state.section_contents = docstore.get_section_contents()
                    state.save(state_path)
                    break  # Stop processing — state is saved for resume

                except Exception as e:
                    ui.error(f"Error processing {file_path}: {e}")
                    if verbose:
                        console.print_exception()
                    error_tracker.warning(
                        what=f"Failed to process {file_path}",
                        why=str(e),
                        action="File parked — run continued without it",
                        file=file_path,
                    )
                    if file_path not in state.parked:
                        state.parked.append(file_path)
                        state.park_reasons[file_path] = str(e)

                progress.advance(task)

    # ── Phase 3b: Parked files ───────────────────────────────────
    if state.parked:
        ui.phase("Phase 3b · Processing parked files with available context")
        for parked_file in list(state.parked):
            full_path = os.path.join(target, parked_file)
            if not os.path.exists(full_path):
                continue
            content = scanner.read_file(full_path)
            if content is None:
                continue
            try:
                processor.process_file(parked_file, content)
                if parked_file not in state.processed:
                    state.processed.append(parked_file)
                state.parked.remove(parked_file)

                file_hash = scanner.get_file_hash(full_path)
                if file_hash:
                    state.file_hashes[parked_file] = file_hash

                state.open_wires = wire_mgr.get_open_wires()
                state.closed_wires = wire_mgr.get_closed_wires()
                state.section_index = docstore.get_section_index()
                state.section_contents = docstore.get_section_contents()
                state.save(state_path)
            except Exception as e:
                ui.warn(f"Could not process parked file {parked_file}: {e}")

    # ── Phase 4: Finalize ────────────────────────────────────────
    _finalize_and_write(
        state,
        wire_mgr,
        docstore,
        llm,
        cfg,
        ui,
        scanner,
        target,
        output_dir,
        codebase_md_path,
        state_path,
        git,
        current_commit,
        current_commit_short,
        out_of_scope_files=state_out_of_scope,
        error_tracker=error_tracker,
    )

    # Show LLM usage
    stats = llm.get_usage_stats()
    cost = stats.get("estimated_cost_usd", 0.0)
    cost_str = f"  estimated cost: {format_cost(cost)}" if cost > 0 else ""
    ui.info(
        f"LLM usage: {stats['total_calls']} calls, "
        f"{stats['total_input_tokens']:,} input tokens, "
        f"{stats['total_output_tokens']:,} output tokens{cost_str}"
    )


def _show_cost_estimate(queue: list, llm, cfg, ui) -> None:
    """Show a rough cost estimate for the queued files before processing starts.

    Uses average token assumptions (500 input + 200 output per file).  The
    real cost can be higher for large files or lower when many files are
    skipped/parked, but this gives the user an order-of-magnitude preview.
    """
    from codilay.pricing import estimate_cost, format_cost

    n = len(queue)
    if n == 0:
        return

    # Heuristic: ~500 input tokens + ~200 output tokens per file on average.
    # Input includes the file content + accumulated doc context; output is the
    # new section JSON.  These are conservative estimates.
    est_input = n * 500
    est_output = n * 200

    cost = estimate_cost(llm.model, est_input, est_output)
    if cost <= 0:
        # Model not in pricing table — skip the estimate silently
        return

    # Build a one-liner hint so it doesn't interrupt the flow
    ui.info(
        f"Estimated cost for {n} file{'s' if n != 1 else ''}: "
        f"[bold]{format_cost(cost)}[/bold]  "
        f"[dim](rough — actual varies by file size and model)[/dim]"
    )


def _finalize_and_write(
    state,
    wire_mgr,
    docstore,
    llm,
    cfg,
    ui,
    scanner,
    target,
    output_dir,
    codebase_md_path,
    state_path,
    git,
    current_commit,
    current_commit_short,
    out_of_scope_files=None,
    error_tracker=None,
):
    """Finalize documentation, write all output files, save state."""
    from codilay.processor import Processor

    ui.phase("Phase 4 · Finalize — Assembling documentation")

    processor = Processor(llm, cfg, wire_mgr, docstore, state, ui)

    with ui.spinner("Running finalization pass…"):
        processor.finalize(scanner.get_file_tree())

    open_wires = wire_mgr.get_open_wires()
    closed_wires = wire_mgr.get_closed_wires()

    # Classify open wires: "out-of-scope" vs genuinely "unresolved"
    # A wire is out-of-scope when its target file is in the out_of_scope set.
    out_of_scope_set = out_of_scope_files or set()
    out_of_scope_wires = []
    unresolved_wires = []
    for w in open_wires:
        target_file = w.get("to", "")
        if target_file in out_of_scope_set:
            w = dict(w)  # copy so we don't mutate the original
            w["status"] = "out-of-scope"
            out_of_scope_wires.append(w)
        else:
            unresolved_wires.append(w)

    # Remove stale dependency-graph / unresolved-references if they exist
    docstore.remove_section("dependency-graph")
    docstore.remove_section("unresolved-references")
    docstore.remove_section("out-of-scope-references")

    docstore.add_dependency_graph(closed_wires)
    docstore.add_unresolved_references(unresolved_wires)

    # Only add out-of-scope section when there are scoped-out wires
    if out_of_scope_wires:
        docstore.add_out_of_scope_references(out_of_scope_wires)

    # Write CODEBASE.md
    final_md = docstore.render_full_document()
    with open(codebase_md_path, "w", encoding="utf-8") as f:
        f.write(final_md)

    # Write links.json
    links_path = os.path.join(output_dir, "links.json")
    links_data = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "project": os.path.basename(target),
        "documented_commit": current_commit,
        "documented_commit_short": current_commit_short,
        "closed": closed_wires,
        "open": unresolved_wires,
        "out_of_scope": out_of_scope_wires,
    }
    with open(links_path, "w", encoding="utf-8") as f:
        json.dump(links_data, f, indent=2)

    # ── Save final state with git info ───────────────────────────
    # Persist all open wires (both unresolved and out-of-scope) in state
    state.open_wires = open_wires
    state.closed_wires = closed_wires
    state.section_index = docstore.get_section_index()
    state.section_contents = docstore.get_section_contents()
    state.last_commit = current_commit
    state.last_commit_short = current_commit_short
    state.last_run = datetime.now(timezone.utc).isoformat()

    # Hash all currently processed files for fallback diffing
    for file_path in state.processed:
        full_path = os.path.join(target, file_path)
        file_hash = scanner.get_file_hash(full_path)
        if file_hash:
            state.file_hashes[file_path] = file_hash

    state.save(state_path)

    # ── Save doc snapshot for diff-doc (Feature 4) ───────────────
    try:
        from codilay.doc_differ import DocVersionStore

        version_store = DocVersionStore(os.path.dirname(codebase_md_path))
        commit_msg = ""
        if git and current_commit:
            commit_msg = git.get_commit_message(current_commit) or ""

        version_store.save_snapshot(
            section_index=state.section_index,
            section_contents=state.section_contents,
            closed_wires=closed_wires,
            open_wires=open_wires,
            run_id=state.run_id,
            commit=current_commit or "",
            commit_msg=commit_msg,
        )
    except Exception:
        pass  # Non-critical — don't block the run

    # ── Error panel + summary ─────────────────────────────────────
    if error_tracker is not None:
        ui.show_error_panel(error_tracker)

    stats = llm.get_usage_stats()
    cost = stats.get("estimated_cost_usd", 0.0)

    ui.show_summary(
        processed_count=len(state.processed),
        wires_closed=len(closed_wires),
        wires_open=len(open_wires),
        sections=len(docstore.get_section_index()),
        output_path=codebase_md_path,
        links_path=links_path,
        error_tracker=error_tracker,
        cost_usd=cost,
    )

    if current_commit_short:
        ui.info(f"Documented at commit [cyan]{current_commit_short}[/cyan] — next run will diff from here")

    ui.show_next_steps(codebase_md_path, target)


# ─── Status command ───────────────────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
def status(target):
    """Show current CodiLay state for a project."""
    import math
    from datetime import timezone as _tz

    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")
    state_path = os.path.join(output_dir, ".codilay_state.json")

    if not os.path.exists(state_path):
        console.print(
            Panel(
                "[yellow]No CodiLay documentation found for this project.[/yellow]\n\n"
                "Get started:\n"
                f"  [bold cyan]codilay {target}[/bold cyan]",
                border_style="yellow",
                title="[bold yellow]Not Documented[/bold yellow]",
            )
        )
        return

    state = AgentState.load(state_path)

    # ── Human-readable age ────────────────────────────────────────
    age_str = "unknown"
    age_days = None
    if state.last_run:
        try:
            last_run_dt = datetime.fromisoformat(state.last_run.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = now - last_run_dt
            age_days = delta.days
            hours = math.floor(delta.seconds / 3600)
            if age_days == 0:
                age_str = f"{hours}h ago" if hours > 0 else "just now"
            elif age_days == 1:
                age_str = "yesterday"
            elif age_days < 7:
                age_str = f"{age_days} days ago"
            elif age_days < 30:
                age_str = f"{age_days // 7} week{'s' if age_days >= 14 else ''} ago"
            else:
                age_str = f"{age_days // 30} month{'s' if age_days >= 60 else ''} ago"
        except (ValueError, TypeError):
            pass

    # ── Staleness indicator ───────────────────────────────────────
    git = GitTracker(target)
    diff_result = None
    changes_count = 0
    if git.is_git_repo and state.last_commit:
        if git.is_commit_valid(state.last_commit):
            diff_result = git.get_full_diff(state.last_commit)
            if diff_result and diff_result.changes:
                changes_count = len(diff_result.changes)

    incomplete = bool(state.queue)

    if incomplete:
        health_badge = "[bold orange3]⚠  INCOMPLETE RUN[/bold orange3]"
        health_hint = f"{len(state.queue)} files still queued — resume with [bold cyan]codilay {target}[/bold cyan]"
    elif changes_count > 50:
        health_badge = "[bold red]✗  STALE[/bold red]"
        health_hint = f"{changes_count} file changes since last run — docs are significantly out of date"
    elif changes_count > 10:
        health_badge = "[bold yellow]~  DRIFTED[/bold yellow]"
        health_hint = f"{changes_count} file changes since last run"
    elif changes_count > 0:
        health_badge = "[bold dim]○  MINOR DRIFT[/bold dim]"
        health_hint = f"{changes_count} file change{'s' if changes_count != 1 else ''} since last run"
    else:
        health_badge = "[bold green]✓  UP TO DATE[/bold green]"
        health_hint = "Documentation matches HEAD"

    # ── Main status table ─────────────────────────────────────────
    table = Table(title="CodiLay Status", box=box.ROUNDED)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold white")

    table.add_row("Health", health_badge)
    table.add_row("Last documented", f"{age_str}  [dim]({state.last_run[:19] if state.last_run else '—'})[/dim]")
    table.add_row("Files documented", str(len(state.processed)))
    table.add_row("Doc sections", str(len(state.section_index)))
    table.add_row(
        "Wires",
        f"[green]{len(state.closed_wires)} closed[/green]  [dim]·[/dim]  [yellow]{len(state.open_wires)} open[/yellow]",
    )
    if state.parked:
        table.add_row(
            "Parked files", f"[yellow]{len(state.parked)}[/yellow]  [dim](skipped — retry with context)[/dim]"
        )
    if state.last_commit_short:
        table.add_row("Documented at commit", f"[cyan]{state.last_commit_short}[/cyan]")

    console.print(table)
    console.print(f"  {health_hint}")

    # ── Git change summary ────────────────────────────────────────
    if diff_result and diff_result.changes:
        console.print()
        console.print(
            f"  [bold yellow]{changes_count} file{'s' if changes_count != 1 else ''} changed[/bold yellow]"
            f"  [dim]({diff_result.commits_behind} commit{'s' if diff_result.commits_behind != 1 else ''} behind HEAD)[/dim]"
        )
        for line in diff_result.summary_lines[:15]:
            console.print(line)
        if changes_count > 15:
            console.print(
                f"  [dim]… +{changes_count - 15} more — run [bold]codilay diff {target}[/bold] for full list[/dim]"
            )
    elif diff_result and not diff_result.changes and not incomplete:
        console.print(
            f"\n  [green]✓[/green]  No changes since last documented commit [cyan]{state.last_commit_short}[/cyan]"
        )

    # ── Open wires (actionable view) ─────────────────────────────
    if state.open_wires:
        console.print()
        console.print(f"  [bold]Open wires[/bold] [dim]({len(state.open_wires)} unresolved references):[/dim]")
        for w in state.open_wires[:10]:
            deleted = "[DELETED]" in w.get("context", "")
            suffix = "  [red](target file deleted)[/red]" if deleted else ""
            console.print(
                f"    [yellow]→[/yellow]  [dim]{w['from']}[/dim] [yellow]→[/yellow] "
                f"[bold]{w['to']}[/bold]  [dim]({w['type']})[/dim]{suffix}"
            )
        if len(state.open_wires) > 10:
            console.print(
                f"    [dim]… +{len(state.open_wires) - 10} more"
                f" — open wires resolve when the referenced files are documented[/dim]"
            )

    # ── Feature status ────────────────────────────────────────────
    feature_rows = []

    snapshots_dir = os.path.join(output_dir, "doc_snapshots")
    if os.path.isdir(snapshots_dir):
        snapshot_count = len([f for f in os.listdir(snapshots_dir) if f.endswith(".json")])
        if snapshot_count > 0:
            feature_rows.append(("Doc snapshots", str(snapshot_count)))

    feedback_path = os.path.join(output_dir, "triage_feedback.json")
    if os.path.exists(feedback_path):
        try:
            with open(feedback_path, "r", encoding="utf-8") as f:
                feedback_data = json.load(f)
            fb_count = len(feedback_data.get("entries", []))
            if fb_count > 0:
                feature_rows.append(("Triage feedback entries", str(fb_count)))
        except (json.JSONDecodeError, KeyError):
            pass

    team_path = os.path.join(output_dir, "team_memory.json")
    if os.path.exists(team_path):
        try:
            with open(team_path, "r", encoding="utf-8") as f:
                team_data = json.load(f)
            facts = len(team_data.get("facts", []))
            decisions = len(team_data.get("decisions", []))
            conventions = len(team_data.get("conventions", []))
            total = facts + decisions + conventions
            if total > 0:
                feature_rows.append(("Team memory", f"{facts} facts, {decisions} decisions, {conventions} conventions"))
        except (json.JSONDecodeError, KeyError):
            pass

    search_index_path = os.path.join(output_dir, "search_index.json")
    if os.path.exists(search_index_path):
        try:
            with open(search_index_path, "r", encoding="utf-8") as f:
                search_data = json.load(f)
            indexed_convs = len(search_data.get("documents", []))
            if indexed_convs > 0:
                feature_rows.append(("Search index", f"{indexed_convs} conversations indexed"))
        except (json.JSONDecodeError, KeyError):
            pass

    schedule_path = os.path.join(output_dir, "schedule.json")
    if os.path.exists(schedule_path):
        try:
            with open(schedule_path, "r", encoding="utf-8") as f:
                sched_data = json.load(f)
            if sched_data.get("enabled"):
                cron_expr = sched_data.get("cron_expression", "unknown")
                feature_rows.append(("Active schedule", f"cron: {cron_expr}"))
        except (json.JSONDecodeError, KeyError):
            pass

    chat_dir = os.path.join(output_dir, "chat")
    if os.path.isdir(chat_dir):
        conv_count = len([f for f in os.listdir(chat_dir) if f.endswith(".json")])
        if conv_count > 0:
            feature_rows.append(("Chat conversations", str(conv_count)))

    if feature_rows:
        console.print()
        feat_table = Table(title="Features", box=box.ROUNDED)
        feat_table.add_column("Feature", style="cyan")
        feat_table.add_column("Status", style="bold white")
        for name, value in feature_rows:
            feat_table.add_row(name, value)
        console.print(feat_table)

    # ── Next steps ────────────────────────────────────────────────
    console.print()
    next_steps = []
    if incomplete:
        next_steps.append(
            f"[bold cyan]codilay {target}[/bold cyan]  — resume incomplete run ({len(state.queue)} files left)"
        )
    elif changes_count > 0:
        next_steps.append(f"[bold cyan]codilay {target}[/bold cyan]  — update docs ({changes_count} changed files)")
    else:
        next_steps.append(f"[bold cyan]codilay serve {target}[/bold cyan]  — browse documentation")
        next_steps.append(f"[bold cyan]codilay chat {target}[/bold cyan]   — ask questions about the code")

    console.print(
        Panel(
            "\n".join(f"  {s}" for s in next_steps),
            title="[bold]Next steps[/bold]",
            border_style="cyan",
            padding=(0, 1),
        )
    )


# ─── Diff command (new — show what would change) ─────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
def diff(target):
    """Show what has changed since the last CodiLay run."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")
    state_path = os.path.join(output_dir, ".codilay_state.json")

    if not os.path.exists(state_path):
        console.print("[yellow]No previous CodiLay run found.[/yellow]")
        console.print("[dim]Run [bold]codilay .[/bold] first to create documentation.[/dim]")
        return

    state = AgentState.load(state_path)
    git = GitTracker(target)

    if not git.is_git_repo:
        console.print("[yellow]Not a git repository. Cannot compute diff.[/yellow]")
        return

    if not state.last_commit:
        console.print("[yellow]No commit recorded in state. Run a full documentation pass first.[/yellow]")
        return

    if not git.is_commit_valid(state.last_commit):
        console.print(f"[red]Last documented commit {state.last_commit_short} no longer exists.[/red]")
        console.print("[dim]This can happen after a rebase or force push.[/dim]")
        console.print("[dim]Run [bold]codilay .[/bold] and choose 'Full re-run'.[/dim]")
        return

    diff_result = git.get_full_diff(state.last_commit)
    if not diff_result:
        console.print("[red]Could not compute diff.[/red]")
        return

    if not diff_result.changes:
        console.print(f"[green]✓ No changes since commit {state.last_commit_short}. Documentation is current.[/green]")
        return

    # Header
    console.print(
        Panel(
            f"[bold]Changes since last documentation run[/bold]\n"
            f"Base: [cyan]{diff_result.base_commit[:8]}[/cyan]  →  "
            f"HEAD: [cyan]{diff_result.head_commit[:8]}[/cyan]  "
            f"({diff_result.commits_behind} commits)",
            border_style="blue",
        )
    )

    # Changes table
    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("Status", style="bold", width=10)
    table.add_column("File")
    table.add_column("Impact", style="dim")

    for change in diff_result.changes:
        # Determine impact
        impact_parts = []
        was_processed = change.path in state.processed
        old_processed = change.old_path in state.processed if change.old_path else False

        if change.change_type == ChangeType.ADDED:
            status_str = "[green]added[/green]"
            impact_parts.append("new file — will be documented")

        elif change.change_type == ChangeType.MODIFIED:
            status_str = "[yellow]modified[/yellow]"
            if was_processed:
                # Count wires that would be affected
                affected_wires = [
                    w
                    for w in state.closed_wires
                    if w.get("from") == change.path or w.get("to") == change.path or w.get("resolved_in") == change.path
                ]
                impact_parts.append(f"re-process, {len(affected_wires)} wires affected")
            else:
                impact_parts.append("not yet documented")

        elif change.change_type == ChangeType.DELETED:
            status_str = "[red]deleted[/red]"
            if was_processed:
                affected_wires = [w for w in state.closed_wires if w.get("resolved_in") == change.path]
                impact_parts.append(f"section marked deleted, {len(affected_wires)} wires re-opened")
            else:
                impact_parts.append("was not documented")

        elif change.change_type == ChangeType.RENAMED:
            status_str = "[cyan]renamed[/cyan]"
            impact_parts.append(f"from {change.old_path}")
            if old_processed:
                impact_parts.append("paths updated + re-processed")

        else:
            status_str = change.change_type.value
            impact_parts.append("")

        file_display = change.path
        if change.change_type == ChangeType.RENAMED:
            file_display = f"{change.old_path} → {change.path}"

        table.add_row(status_str, file_display, ", ".join(impact_parts))

    console.print(table)

    # Summary
    console.print()
    console.print(
        f"  [bold]{len(diff_result.files_to_process)}[/bold] files to process, "
        f"[bold]{len(diff_result.deleted)}[/bold] deletions to handle"
    )
    console.print("\n[dim]Run [bold]codilay .[/bold] to update documentation.[/dim]")

    # Show commits
    if diff_result.commit_messages:
        console.print(f"\n[bold]Commits ({diff_result.commits_behind}):[/bold]")
        for msg in diff_result.commit_messages[:15]:
            console.print(f"  [dim]{msg}[/dim]")
        if len(diff_result.commit_messages) > 15:
            extra = len(diff_result.commit_messages) - 15
            console.print(f"  [dim]… +{extra} more[/dim]")


# ─── Clean command ────────────────────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option("--all", "clean_all", is_flag=True, help="Also remove chat history and audit reports")
def clean(target, yes, clean_all):
    """Remove CodiLay generated files.

    By default removes: state, CODEBASE.md, links.  Chat history and audit
    reports are preserved unless --all is passed.
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    def _file_size_str(path: str) -> str:
        try:
            size = os.path.getsize(path)
            if size < 1024:
                return f"{size} B"
            if size < 1024 * 1024:
                return f"{size / 1024:.1f} KB"
            return f"{size / 1024 / 1024:.1f} MB"
        except OSError:
            return "?"

    # Core generated files
    core_candidates = [
        ".codilay_state.json",
        ".codilay_state.json.bak.1",
        ".codilay_state.json.bak.2",
        ".codilay_state.json.bak.3",
        ".codilay.lock",
        "CODEBASE.md",
        "CODEBASE.md.bak",
        "links.json",
    ]

    files_to_remove = []
    total_bytes = 0
    for fname in core_candidates:
        path = os.path.join(output_dir, fname)
        if os.path.exists(path):
            size = os.path.getsize(path) if os.path.exists(path) else 0
            total_bytes += size
            files_to_remove.append((fname, path, _file_size_str(path)))

    if not files_to_remove and not clean_all:
        console.print("[yellow]Nothing to clean.[/yellow]")
        return

    # Chat history + audits (preserved by default)
    chat_dir = os.path.join(output_dir, "chat")
    chat_count = 0
    chat_bytes = 0
    if os.path.isdir(chat_dir):
        for f in os.listdir(chat_dir):
            fp = os.path.join(chat_dir, f)
            if os.path.isfile(fp):
                chat_count += 1
                chat_bytes += os.path.getsize(fp)

    audits_dir = os.path.join(output_dir, "audits")
    audit_count = 0
    audit_bytes = 0
    if os.path.isdir(audits_dir):
        for f in os.listdir(audits_dir):
            fp = os.path.join(audits_dir, f)
            if os.path.isfile(fp):
                audit_count += 1
                audit_bytes += os.path.getsize(fp)

    # Build display
    console.print("[bold]Files to remove:[/bold]")
    for fname, path, size_str in files_to_remove:
        is_backup = ".bak." in fname
        style = "dim red" if is_backup else "red"
        label = "  (state backup)" if is_backup else ""
        console.print(f"  [{style}]✗[/{style}] {fname}  [dim]{size_str}[/dim]{label}")

    if total_bytes > 0:
        if total_bytes < 1024 * 1024:
            total_str = f"{total_bytes / 1024:.1f} KB"
        else:
            total_str = f"{total_bytes / 1024 / 1024:.1f} MB"
        console.print(f"\n  Total to free: [bold]{total_str}[/bold]")

    # Warn about preserved items
    preserved = []
    if chat_count > 0 and not clean_all:
        chat_sz = f"{chat_bytes / 1024:.0f} KB"
        preserved.append(
            f"  [dim]Chat history: {chat_count} conversation{'s' if chat_count != 1 else ''} ({chat_sz}) — "
            f"preserved (use [bold]--all[/bold] to remove)[/dim]"
        )
    if audit_count > 0 and not clean_all:
        audit_sz = f"{audit_bytes / 1024:.0f} KB"
        preserved.append(
            f"  [dim]Audit reports: {audit_count} report{'s' if audit_count != 1 else ''} ({audit_sz}) — "
            f"preserved (use [bold]--all[/bold] to remove)[/dim]"
        )
    if preserved:
        console.print("\n[bold]Preserved (not deleted):[/bold]")
        for p in preserved:
            console.print(p)

    if not files_to_remove and not clean_all:
        return

    if not yes:
        confirm = click.confirm("\nProceed?", default=False)
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            return

    removed = 0
    for fname, path, _ in files_to_remove:
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            console.print(f"  [yellow]⚠[/yellow]  Could not remove {fname}: {e}")

    if clean_all:
        import shutil

        if os.path.isdir(chat_dir):
            shutil.rmtree(chat_dir)
            removed += chat_count
        if os.path.isdir(audits_dir):
            shutil.rmtree(audits_dir)
            removed += audit_count

    console.print(f"\n[green]✓[/green]  Removed {removed} file{'s' if removed != 1 else ''}.")
    console.print(f"[dim]Run [bold]codilay {target}[/bold] to generate fresh documentation.[/dim]")


# ─── Init command ─────────────────────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
def init(target):
    """Create a codilay.config.json in the target directory."""
    target = os.path.abspath(target)
    config_path = os.path.join(target, "codilay.config.json")

    if os.path.exists(config_path):
        console.print(f"[yellow]Config already exists: {config_path}[/yellow]")
        return

    # ── Doc output location ───────────────────────────────────────
    console.print()
    console.print("[bold]Where should generated docs be stored?[/bold]\n")
    console.print(
        (
            "  [bold cyan][1][/bold cyan]  codilay/CODEBASE.md   "
            "[dim]— commit docs, gitignore chat/state    (recommended)[/dim]"
        )
    )
    console.print(
        "  [bold cyan][2][/bold cyan]  docs/CODEBASE.md      [dim]— docs in docs/, codilay/ fully gitignored[/dim]"
    )
    console.print("  [bold cyan][3][/bold cyan]  gitignore everything  [dim]— local tool only, nothing committed[/dim]")
    console.print()

    loc_raw = click.prompt(
        "Select",
        type=click.Choice(["1", "2", "3"]),
        default="1",
        show_choices=False,
    )
    doc_location_map = {"1": "codilay", "2": "docs", "3": "local"}
    doc_location = doc_location_map[loc_raw]

    default_config = {
        "ignore": [
            "**/*.test.*",
            "**/*.spec.*",
            "**/__tests__/**",
            "coverage/",
            "dist/",
            "build/",
        ],
        "notes": "",
        "instructions": "",
        "entryHint": "",
        "llm": {
            "model": "claude-sonnet-4-20250514",
            "maxTokensPerCall": 4096,
        },
        "watch": {
            "debounce_seconds": 2.0,
            "extra_ignore": [],
        },
        "export": {
            "default_format": "compact",
            "max_tokens": 100000,
        },
        "schedule": {
            "enabled": False,
            "cron": "",
            "on_commit": False,
            "branch": "main",
        },
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_config, f, indent=2)

    console.print(f"\n[green]Created config:[/green] {config_path}")

    # ── Write .gitignore entries ──────────────────────────────────
    _write_gitignore_for_doc_location(target, doc_location, console)

    console.print()
    console.print("[dim]Tip: New config sections available:[/dim]")
    console.print("[dim]  • [bold]watch[/bold]    — debounce settings for watch mode[/dim]")
    console.print("[dim]  • [bold]export[/bold]   — default format and token limits[/dim]")
    console.print("[dim]  • [bold]schedule[/bold] — cron and on-commit auto re-runs[/dim]")


def _write_gitignore_for_doc_location(target: str, doc_location: str, cons) -> None:
    """
    Write appropriate .gitignore entries based on the chosen doc output
    location.  Three scenarios:

    "codilay"  → codilay/ stays in repo; only internal state dirs are ignored
    "docs"     → codilay/ entirely ignored; docs/ is already tracked
    "local"    → entire codilay/ and docs/CODEBASE.md are ignored
    """
    gitignore_path = os.path.join(target, ".gitignore")

    # Lines we might need to add, keyed by scenario
    MARKER = "# CodiLay — auto-generated"

    if doc_location == "codilay":
        new_lines = [
            MARKER,
            "# Commit codilay/CODEBASE.md and codilay/links.json",
            "# Ignore internal state / personal data",
            "codilay/chat/",
            "codilay/memory/",
            "codilay/team/",
            "codilay/history/",
            "codilay/doc_snapshots/",
            "codilay/search_index.json",
            "codilay/triage_feedback.json",
            "codilay/schedule.json",
            "codilay/.scheduler.pid",
            "codilay/.codilay_state.json",
            "",
        ]
        msg = (
            "[green]✓[/green] .gitignore updated — "
            "[bold]codilay/CODEBASE.md[/bold] and [bold]codilay/links.json[/bold] will be committed; "
            "chat/state/memory dirs are ignored."
        )

    elif doc_location == "docs":
        new_lines = [
            MARKER,
            "# CodiLay operational state — fully ignored",
            "codilay/",
            "",
        ]
        msg = (
            "[green]✓[/green] .gitignore updated — "
            "[bold]codilay/[/bold] is fully ignored. "
            "Docs will be written to [bold]docs/CODEBASE.md[/bold] (already tracked)."
        )

    else:  # "local"
        new_lines = [
            MARKER,
            "# CodiLay — local only, nothing committed",
            "codilay/",
            "docs/CODEBASE.md",
            "",
        ]
        msg = (
            "[green]✓[/green] .gitignore updated — "
            "[bold]codilay/[/bold] and [bold]docs/CODEBASE.md[/bold] are fully ignored."
        )

    # Read existing .gitignore (or start empty)
    existing = ""
    if os.path.exists(gitignore_path):
        with open(gitignore_path, "r", encoding="utf-8") as f:
            existing = f.read()

    # Only append if the marker isn't already there
    if MARKER in existing:
        cons.print("[dim]⚠  .gitignore already contains CodiLay entries — skipping.[/dim]")
        return

    separator = "\n" if existing and not existing.endswith("\n") else ""
    addition = separator + "\n".join(new_lines) + "\n"

    with open(gitignore_path, "a", encoding="utf-8") as f:
        f.write(addition)

    cons.print(msg)


# ─── Interactive menu ─────────────────────────────────────────────────────────


@cli.command(hidden=True)
@click.pass_context
def interactive(ctx):
    """Launch the interactive application menu."""
    from codilay.menu import main_menu

    settings: Settings = ctx.obj["settings"]
    result = main_menu(settings)

    if result and result.get("action") == "run":
        # Re-inject env vars in case keys were added during the menu session
        settings.inject_env_vars()

        ctx.obj["provider"] = result.get("provider") or settings.default_provider
        ctx.obj["model"] = result.get("model")
        ctx.obj["base_url"] = result.get("base_url") or settings.custom_base_url
        ctx.obj["verbose"] = result.get("verbose", settings.verbose)
        ctx.invoke(run, target=result["target"])

    elif result and result.get("action") == "chat":
        settings.inject_env_vars()
        ctx.invoke(chat, target=result["target"])

    elif result and result.get("action") == "serve":
        ctx.invoke(serve, target=result["target"])

    elif result and result.get("action") == "watch":
        settings.inject_env_vars()
        ctx.obj["provider"] = settings.default_provider
        ctx.obj["model"] = settings.default_model
        ctx.obj["base_url"] = settings.custom_base_url
        ctx.obj["verbose"] = settings.verbose
        ctx.invoke(watch, target=result["target"])

    elif result and result.get("action") == "export":
        ctx.invoke(
            export_cmd,
            target=result["target"],
            fmt=result.get("format", "markdown"),
        )

    elif result and result.get("action") == "diff-doc":
        ctx.invoke(diff_doc, target=result["target"])

    elif result and result.get("action") == "search":
        ctx.invoke(search_cmd, target=result["target"], query=result.get("query", ""))

    elif result and result.get("action") == "schedule-status":
        # Show schedule status via the schedule_status subcommand
        ctx.invoke(schedule_status, target=result["target"])

    elif result and result.get("action") == "graph":
        ctx.invoke(graph_cmd, target=result["target"])

    elif result and result.get("action") == "team":
        console.print(
            "[dim]Use the CLI directly for team memory commands:[/dim]\n"
            "  [bold]codilay team facts " + result["target"] + "[/bold]\n"
            "  [bold]codilay team add-fact " + result["target"] + "[/bold]\n"
            "  [bold]codilay team decisions " + result["target"] + "[/bold]\n"
            "  [bold]codilay team conventions " + result["target"] + "[/bold]\n"
        )

    elif result and result.get("action") == "triage-feedback":
        console.print(
            "[dim]Use the CLI directly for triage feedback commands:[/dim]\n"
            "  [bold]codilay triage-feedback list " + result["target"] + "[/bold]\n"
            "  [bold]codilay triage-feedback add " + result["target"] + " <file>[/bold]\n"
            "  [bold]codilay triage-feedback hint " + result["target"] + " <file>[/bold]\n"
        )

    elif result and result.get("action") == "audit":
        ctx.invoke(audit_command, target=result["target"], audit_type=result["type"], mode=result["mode"])

    elif result and result.get("action") == "annotate":
        settings.inject_env_vars()
        # Per-run model/config-model overrides from the run-config screen
        if result.get("model") is not None:
            ctx.obj["model"] = result["model"]
        if "use_config_model" in result:
            # Temporarily patch settings for this invocation
            settings.annotate_use_config_model = result["use_config_model"]
        ctx.invoke(
            annotate,
            target=result["target"],
            level=result.get("level", settings.annotate_level),
            dry_run=result.get("dry_run", True),
        )


# ─── Setup wizard ─────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def setup(ctx):
    """Run the first-time setup wizard (configure API keys, provider, model)."""
    from codilay.menu import _menu_setup

    settings: Settings = ctx.obj["settings"]
    _menu_setup(settings)


# ─── Config viewer ────────────────────────────────────────────────────────────


@cli.command("config")
@click.pass_context
def show_config(ctx):
    """View all current CodiLay settings."""
    from codilay.menu import _menu_view_settings

    settings: Settings = ctx.obj["settings"]
    _menu_view_settings(settings)


# ─── Key management ──────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def keys(ctx):
    """Manage stored API keys (add, view, remove)."""
    from codilay.menu import _menu_api_keys

    settings: Settings = ctx.obj["settings"]
    _menu_api_keys(settings)


# ─── Serve command (Web UI) ──────────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--port", "-P", default=None, type=int, help="Port to serve on (overrides preference)")
@click.option("--host", "-H", default="127.0.0.1", help="Host to bind to")
@click.option("--output", "-o", default=None, help="Output directory containing codilay files")
def serve(target, port, host, output):
    """Launch the CodiLay web UI for browsing documentation.

    \b
    Examples:
        codilay serve .                  Serve current project on port 8484
        codilay serve /path/to/project   Serve a specific project
        codilay serve . --port 9000      Custom port
    """
    target = os.path.abspath(target)
    output_dir = output or os.path.join(target, "codilay")

    # Resolve port and auto-open from settings when not explicitly provided
    from codilay.settings import Settings

    settings = Settings.load()
    settings.inject_env_vars()

    effective_port = port if port is not None else settings.web_ui_port
    auto_open = settings.web_ui_auto_open_browser

    # Quick validation
    codebase_md = os.path.join(output_dir, "CODEBASE.md")
    if not os.path.exists(codebase_md):
        console.print(
            f"[red]No documentation found at {output_dir}[/red]\n"
            f"[dim]Run [bold]codilay {target}[/bold] first to generate docs.[/dim]"
        )
        return

    try:
        from codilay.server import run_server
    except ImportError as e:
        console.print(
            f"[red]Missing dependencies for web UI: {e}[/red]\n"
            f"[dim]Install with: [bold]pip install codilay[serve][/bold][/dim]"
        )
        return

    console.print(
        Panel(
            f"[bold]CodiLay Web UI[/bold]\n\n"
            f"  Project:  [cyan]{os.path.basename(target)}[/cyan]\n"
            f"  URL:      [bold green]http://{host}:{effective_port}[/bold green]\n\n"
            f"[dim]Press Ctrl+C to stop.[/dim]",
            border_style="blue",
            title="serve",
        )
    )

    if auto_open:
        import threading
        import webbrowser

        def _open_browser():
            import time

            time.sleep(1.0)  # Give the server a moment to start
            webbrowser.open(f"http://{host}:{effective_port}")

        t = threading.Thread(target=_open_browser, daemon=True)
        t.start()

    run_server(target, output_dir, host=host, port=effective_port)


# ── Chat command ──────────────────────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--resume", "-r", is_flag=True, help="Resume last conversation")
@click.option("--list", "-l", "list_convs", is_flag=True, help="List past conversations")
@click.option("--conversation", "-c", default=None, help="Resume a specific conversation by ID")
@click.option("--output", "-o", default=None, help="Output directory containing codilay files")
@click.pass_context
def chat(ctx, target, resume, list_convs, conversation, output):
    """Start an interactive chat about your codebase.

    \\b
    Examples:
        codilay chat .                    Start new chat
        codilay chat . --resume           Resume last conversation
        codilay chat . --list             List past conversations
        codilay chat . -c CONV_ID         Resume specific conversation
    """
    target = os.path.abspath(target)
    output_dir = output or os.path.join(target, "codilay")

    # Validate docs exist
    codebase_md = os.path.join(output_dir, "CODEBASE.md")
    if not os.path.exists(codebase_md):
        console.print(
            f"[red]No documentation found at {output_dir}[/red]\n"
            f"[dim]Run [bold]codilay {target}[/bold] first to generate docs.[/dim]"
        )
        return

    from codilay.chatstore import ChatStore, make_message
    from codilay.retriever import Retriever

    chat_store = ChatStore(output_dir)

    # ── List mode ─────────────────────────────────────────────────
    if list_convs:
        convs = chat_store.list_conversations()
        if not convs:
            console.print("[dim]No past conversations.[/dim]")
            return
        table = Table(title="Past Conversations", box=box.ROUNDED, border_style="blue")
        table.add_column("ID", style="dim", max_width=12)
        table.add_column("Title", style="cyan")
        table.add_column("Messages", justify="right")
        table.add_column("Last Active", style="dim")
        for c in convs:
            table.add_row(
                c["id"][:8] + "…",
                c.get("title", "Untitled"),
                str(c.get("message_count", 0)),
                c.get("updated_at", "?")[:16],
            )
        console.print(table)
        return

    # ── Load agent state + retriever ──────────────────────────────
    state_path = os.path.join(output_dir, ".codilay_state.json")
    if not os.path.exists(state_path):
        alt = os.path.join(output_dir, ".codylay_state.json")
        if os.path.exists(alt):
            state_path = alt
    state = AgentState.load(state_path)
    retriever = Retriever(state.section_index, state.section_contents)

    # ── LLM setup ─────────────────────────────────────────────────
    settings = ctx.obj["settings"]
    settings.inject_env_vars()
    cfg = CodiLayConfig(target_path=target)
    cfg.llm_provider = ctx.obj["provider"] or settings.default_provider
    cfg.llm_model = ctx.obj.get("model") or settings.default_model
    base_url = ctx.obj.get("base_url") or settings.custom_base_url
    if base_url:
        cfg.llm_base_url = base_url

    llm = LLMClient(cfg)

    # ── Resolve or create conversation ────────────────────────────
    conv_id = None
    if conversation:
        conv = chat_store.get_conversation(conversation)
        if conv is None:
            console.print(f"[red]Conversation {conversation} not found.[/red]")
            return
        conv_id = conv["id"]
        console.print(f"[dim]Resuming: {conv.get('title', 'Untitled')}[/dim]")
    elif resume:
        convs = chat_store.list_conversations()
        if convs:
            conv_id = convs[0]["id"]
            console.print(f"[dim]Resuming: {convs[0].get('title', 'Untitled')}[/dim]")
        else:
            console.print("[dim]No previous conversations. Starting fresh.[/dim]")

    if conv_id is None:
        conv = chat_store.create_conversation()
        conv_id = conv["id"]

    # ── Memory context ────────────────────────────────────────────
    memory_ctx = chat_store.build_memory_context()

    # ── Header ────────────────────────────────────────────────────
    mem = chat_store.load_memory()
    facts_count = len(mem.get("facts", []))
    prefs_count = len(mem.get("preferences", {}))

    console.print(
        Panel(
            f"[bold]CodiLay Chat[/bold] · [cyan]{os.path.basename(target)}[/cyan]\n"
            f"Memory: [yellow]{facts_count} facts[/yellow] · "
            f"[yellow]{prefs_count} preferences[/yellow]\n"
            f"[dim]Type /help for commands · /quit to exit[/dim]",
            border_style="blue",
            title="chat",
        )
    )

    force_deep = False
    last_msg_id = None

    # ── Chat loop ─────────────────────────────────────────────────
    while True:
        try:
            user_input = console.input("\n[bold green]You:[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/dim]")
            break

        if not user_input:
            continue

        # ── Handle slash commands ─────────────────────────────────
        if user_input.startswith("/"):
            cmd_parts = user_input[1:].split(None, 1)
            cmd = cmd_parts[0].lower()
            cmd_arg = cmd_parts[1] if len(cmd_parts) > 1 else ""

            if cmd in ("quit", "exit", "q"):
                # Extract memory before leaving
                try:
                    n = chat_store.extract_and_store_memory(conv_id, llm)
                    if n > 0:
                        console.print(f"[dim]💾 Saved {n} facts to memory.[/dim]")
                except Exception:
                    pass
                console.print("[dim]Goodbye![/dim]")
                break

            elif cmd == "help":
                _chat_help(console)
                continue

            elif cmd == "pin":
                msg_idx = cmd_arg or "last"
                if msg_idx == "last" and last_msg_id:
                    chat_store.pin_message(conv_id, last_msg_id, True)
                    console.print("[green]✓ Pinned — this answer will persist across chats[/green]")
                else:
                    console.print("[dim]No message to pin. Chat first![/dim]")
                continue

            elif cmd == "unpin":
                if last_msg_id:
                    chat_store.pin_message(conv_id, last_msg_id, False)
                    console.print("[green]✓ Unpinned[/green]")
                continue

            elif cmd == "promote":
                if last_msg_id:
                    console.print("[dim]Promoting to documentation...[/dim]")
                    try:
                        from codilay.docstore import DocStore

                        docstore = DocStore()
                        docstore.load_from_state(state.section_index, state.section_contents)
                        section_id = chat_store.promote_to_doc(conv_id, last_msg_id, docstore, llm)
                        if section_id:
                            # Re-render CODEBASE.md
                            final_md = docstore.render_full_document()
                            with open(codebase_md, "w", encoding="utf-8") as f:
                                f.write(final_md)
                            console.print(f'[green]✓ Promoted to doc section "[cyan]{section_id}[/cyan]"[/green]')
                        else:
                            console.print("[red]Could not promote (LLM error).[/red]")
                    except Exception as e:
                        console.print(f"[red]Promotion failed: {e}[/red]")
                else:
                    console.print("[dim]No message to promote.[/dim]")
                continue

            elif cmd == "export":
                md = chat_store.export_markdown(conv_id)
                if md:
                    export_dir = os.path.join(output_dir, "chat", "exports")
                    os.makedirs(export_dir, exist_ok=True)
                    conv = chat_store.get_conversation(conv_id)
                    title = conv.get("title", "chat") if conv else "chat"
                    from codilay.chatstore import _slugify

                    fname = f"{_slugify(title)}.md"
                    fpath = os.path.join(export_dir, fname)
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(md)
                    console.print(f"[green]✓ Exported to {fpath}[/green]")
                continue

            elif cmd == "branch":
                if last_msg_id:
                    branch = chat_store.branch_conversation(conv_id, last_msg_id)
                    if branch:
                        conv_id = branch["id"]
                        console.print(f"[green]✓ Branched! Now in conversation {conv_id[:8]}…[/green]")
                    else:
                        console.print("[red]Could not branch.[/red]")
                continue

            elif cmd == "memory":
                if cmd_arg == "clear":
                    chat_store.clear_memory()
                    memory_ctx = ""
                    console.print("[green]✓ Memory cleared.[/green]")
                else:
                    _show_memory(console, chat_store.load_memory())
                continue

            elif cmd == "deep":
                force_deep = True
                console.print("[dim]Next answer will read source files directly.[/dim]")
                continue

            elif cmd == "history":
                convs = chat_store.list_conversations()
                if not convs:
                    console.print("[dim]No past conversations.[/dim]")
                else:
                    for c in convs[:10]:
                        console.print(
                            f"  [dim]{c['id'][:8]}…[/dim] "
                            f"[cyan]{c.get('title', 'Untitled')}[/cyan] "
                            f"[dim]({c.get('message_count', 0)} msgs)[/dim]"
                        )
                continue

            elif cmd == "resume":
                if cmd_arg:
                    # Try to match partial ID
                    convs = chat_store.list_conversations()
                    match = None
                    for c in convs:
                        if c["id"].startswith(cmd_arg):
                            match = c
                            break
                    if match:
                        conv_id = match["id"]
                        console.print(f"[green]✓ Resumed: {match.get('title', 'Untitled')}[/green]")
                    else:
                        console.print(f"[red]No conversation matching '{cmd_arg}'[/red]")
                else:
                    console.print("[dim]Usage: /resume <id_prefix>[/dim]")
                continue

            elif cmd == "new":
                # Extract memory from current conversation
                try:
                    n = chat_store.extract_and_store_memory(conv_id, llm)
                    if n > 0:
                        console.print(f"[dim]💾 Saved {n} facts from previous chat.[/dim]")
                except Exception:
                    pass
                conv = chat_store.create_conversation()
                conv_id = conv["id"]
                memory_ctx = chat_store.build_memory_context()
                console.print("[green]✓ Started new conversation.[/green]")
                continue

            else:
                console.print(f"[dim]Unknown command: /{cmd}. Type /help for commands.[/dim]")
                continue

        # ── Normal question flow ──────────────────────────────────
        user_msg = make_message("user", user_input)
        chat_store.add_message(conv_id, user_msg)

        # ── Check if we should go deep immediately ────────────────
        def _should_escalate_by_keyword(question: str) -> bool:
            q = question.lower()
            deep_patterns = [
                "show me the code",
                "show the code",
                "exactly how",
                "line by line",
                "implementation detail",
                "source code",
                "what does the code",
                "read the file",
                "look at the file",
                "open the file",
                "specific implementation",
                "actual code",
            ]
            return any(p in q for p in deep_patterns)

        force_deep = force_deep or _should_escalate_by_keyword(user_input)

        # Retrieve relevant sections
        relevant = retriever.search(user_input, top_k=5)

        # Build pinned context
        pinned_msgs = chat_store.get_pinned_messages(conv_id)
        pinned_ctx = ""
        if pinned_msgs:
            pinned_ctx = "\n\n".join(f"- {m['content'][:200]}" for m in pinned_msgs[:5])

        # Build conversation history
        chat_context = chat_store.build_chat_context(conv_id, max_messages=10)
        history_text = ""
        if len(chat_context) > 1:
            history_lines = []
            for cm in chat_context[:-1]:
                role = cm["role"].capitalize()
                content = cm["content"][:300]
                history_lines.append(f"{role}: {content}")
            history_text = "\n".join(history_lines[-6:])

        # Should we go deep?
        should_deep = force_deep
        force_deep = False  # Reset after use

        answer = ""
        sources = []
        escalated = False

        if not should_deep:
            if relevant:
                # Build doc context
                from codilay.prompts import chat_system_prompt, chat_user_prompt

                context_parts = [sec.formatted for sec in relevant]
                doc_context = "\n\n---\n\n".join(context_parts)

                system = chat_system_prompt(
                    memory_context=memory_ctx,
                    pinned_context=pinned_ctx,
                )
                user = chat_user_prompt(
                    question=user_input,
                    doc_context=doc_context,
                    conversation_history=history_text,
                )

                try:
                    with console.status("[dim]Thinking...[/dim]"):
                        raw_text = llm._raw_call_with_rate_limit(system, user, json_mode=False)

                    # Parse confidence
                    confidence = 0.5
                    lines = raw_text.strip().split("\n")
                    answer_lines = []
                    for line in lines:
                        if line.strip().startswith("CONFIDENCE:"):
                            try:
                                confidence = float(line.strip().split("CONFIDENCE:")[1].strip())
                            except (ValueError, IndexError):
                                pass
                        else:
                            answer_lines.append(line)

                    answer = "\n".join(answer_lines).strip()
                    sources = [sec.section_id for sec in relevant]

                    if not answer or confidence < 0.7:
                        should_deep = True
                        reason = "Doc context insufficient" if answer else "No clear answer from docs"
                        console.print(f"[dim]{reason} — escalating to deep agent...[/dim]")
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
                    should_deep = True  # Fallback to deep agent if Doc search fails
            else:
                should_deep = True
                console.print("[dim]No relevant documentation found — searching source code...[/dim]")

        if should_deep:
            # Deep agent — reads actual source files
            escalated = True
            file_candidates = set(retriever.get_source_files(user_input, top_k=5))
            for sec in relevant:
                if sec.file:
                    file_candidates.add(sec.file)

            file_contents = {}
            for fpath in list(file_candidates)[:5]:
                full = os.path.join(target, fpath)
                if os.path.exists(full) and os.path.isfile(full):
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as fh:
                            content = fh.read()
                        if len(content) > 15000:
                            content = content[:15000] + "\n\n... [truncated]"
                        file_contents[fpath] = content
                    except Exception:
                        pass

            if file_contents:
                source_parts = []
                for fpath, content in file_contents.items():
                    source_parts.append(f"### File: {fpath}\n```\n{content}\n```")
                source_context = "\n\n".join(source_parts)

                doc_ctx = ""
                if relevant:
                    doc_parts = [sec.formatted for sec in relevant[:3]]
                    doc_ctx = "Documentation context:\n\n" + "\n---\n".join(doc_parts) + "\n\n---\n\n"

                system = (
                    "You are a deep codebase analysis agent. You have access to actual "
                    "source code files. Answer the user's question with precision, "
                    "referencing specific functions, classes, line ranges, and logic. "
                    "Be thorough but concise. Use markdown formatting.\n"
                    "IMPORTANT: Respond with PLAIN TEXT markdown only. Do NOT wrap your "
                    "entire response in a JSON object or any other format."
                )
                user_prompt = (
                    f"{doc_ctx}{history_text}\n\nSource code:\n\n{source_context}\n\n---\n\nQuestion: {user_input}"
                )

                try:
                    with console.status("[dim]Reading source files...[/dim]"):
                        answer = llm._raw_call_with_rate_limit(system, user_prompt)
                    answer = answer.strip()
                    sources = list(file_contents.keys())
                except Exception as e:
                    console.print(f"[red]Deep analysis failed: {e}[/red]")
                    continue
            else:
                answer = "I couldn't find relevant source files. Try mentioning specific file or module names."

        # ── Display answer ────────────────────────────────────────
        console.print()
        console.print(
            Panel(
                answer,
                title="[bold blue]CodiLay[/bold blue]" + (" [yellow]🔍 deep[/yellow]" if escalated else ""),
                border_style="blue",
                padding=(1, 2),
            )
        )
        if sources:
            src_text = ", ".join(str(s) for s in sources[:5])
            console.print(f"[dim]Sources: {src_text}[/dim]")

        # ── Persist assistant message ─────────────────────────────
        asst_msg = make_message(
            "assistant",
            answer,
            sources=sources,
            escalated=escalated,
        )
        chat_store.add_message(conv_id, asst_msg)
        last_msg_id = asst_msg["id"]

        # Track topic
        if relevant:
            chat_store.track_topic(relevant[0].title)


def _chat_help(console):
    """Display chat command help."""
    console.print(
        Panel(
            "[bold]Chat Commands[/bold]\n\n"
            "  [cyan]/help[/cyan]          Show this help\n"
            "  [cyan]/pin[/cyan]           Pin the last answer (persists across chats)\n"
            "  [cyan]/unpin[/cyan]         Unpin the last answer\n"
            "  [cyan]/promote[/cyan]       Promote last answer to CODEBASE.md\n"
            "  [cyan]/export[/cyan]        Export conversation to markdown\n"
            "  [cyan]/branch[/cyan]        Branch conversation from last message\n"
            "  [cyan]/deep[/cyan]          Force next answer from source code\n"
            "  [cyan]/memory[/cyan]        View stored memory facts\n"
            "  [cyan]/memory clear[/cyan]  Clear all memory\n"
            "  [cyan]/history[/cyan]       List past conversations\n"
            "  [cyan]/resume ID[/cyan]     Resume a past conversation\n"
            "  [cyan]/new[/cyan]           Start a fresh conversation\n"
            "  [cyan]/quit[/cyan]          Exit chat",
            border_style="blue",
            title="help",
        )
    )


def _show_memory(console, memory):
    """Display cross-session memory."""
    facts = memory.get("facts", [])
    prefs = memory.get("preferences", {})
    topics = memory.get("frequent_topics", {})

    if not facts and not prefs and not topics:
        console.print("[dim]Memory is empty.[/dim]")
        return

    lines = []
    if facts:
        lines.append("[bold]Facts:[/bold]")
        for f in facts:
            cat = f.get("category", "general")
            lines.append(f"  [{cat}] {f['fact']}")

    if prefs:
        lines.append("\n[bold]Preferences:[/bold]")
        for k, v in prefs.items():
            lines.append(f"  {k}: {v}")

    if topics:
        sorted_topics = sorted(topics.items(), key=lambda x: x[1], reverse=True)
        lines.append("\n[bold]Frequent Topics:[/bold]")
        for t, c in sorted_topics[:10]:
            lines.append(f"  {t} ({c}×)")

    console.print(Panel("\n".join(lines), border_style="yellow", title="memory"))


# ─── Watch command (Feature 1) ───────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--debounce", "-d", default=None, type=float, help="Debounce delay in seconds (overrides preference)")
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def watch(target, debounce, verbose):
    """Watch for file changes and auto-update documentation.

    \b
    Examples:
        codilay watch .                  Watch current directory
        codilay watch . --debounce 5     5 second debounce
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    # Validate that docs exist
    codebase_md = os.path.join(output_dir, "CODEBASE.md")
    if not os.path.exists(codebase_md):
        console.print(
            f"[red]No documentation found at {output_dir}[/red]\n"
            f"[dim]Run [bold]codilay {target}[/bold] first to generate docs.[/dim]"
        )
        return

    try:
        from codilay.watcher import HAS_WATCHDOG, Watcher
    except ImportError:
        console.print(
            "[red]Could not import watcher module.[/red]\n"
            "[dim]Install with: [bold]pip install codilay[watch][/bold][/dim]"
        )
        return

    if not HAS_WATCHDOG:
        console.print(
            "[red]watchdog is not installed.[/red]\n"
            "[dim]Install with: [bold]pip install watchdog[/bold] "
            "or [bold]pip install codilay[watch][/bold][/dim]"
        )
        return

    # Load settings and project config
    from codilay.settings import Settings

    settings = Settings.load()
    settings.inject_env_vars()

    # --debounce CLI flag wins; fall back to user preference
    effective_debounce = debounce if debounce is not None else settings.watch_debounce_seconds

    cfg = CodiLayConfig.load(target)
    ignore_patterns = cfg.ignore_patterns if cfg.ignore_patterns else None

    # watch_extensions: None means use the built-in defaults
    watch_extensions = settings.watch_extensions if settings.watch_extensions else None

    console.print(
        Panel(
            f"[bold]CodiLay Watch Mode[/bold]\n\n"
            f"  Project:   [cyan]{os.path.basename(target)}[/cyan]\n"
            f"  Debounce:  [cyan]{effective_debounce}s[/cyan]\n\n"
            f"[dim]Watching for file changes. Press Ctrl+C to stop.[/dim]",
            border_style="blue",
            title="watch",
        )
    )

    watcher = Watcher(
        target_path=target,
        output_dir=output_dir,
        debounce=effective_debounce,
        ignore_patterns=ignore_patterns,
        verbose=verbose,
        watch_extensions=watch_extensions,
        auto_open_ui=settings.watch_auto_open_ui,
        ui_port=settings.web_ui_port,
    )
    watcher.start()


# ─── Export command (Feature 3) ──────────────────────────────────────────────


@cli.command("export")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option(
    "--format", "-f", "fmt", default=None, type=click.Choice(["markdown", "xml", "json"]), help="Export format"
)
@click.option("--max-tokens", "-t", default=None, type=int, help="Token budget limit")
@click.option("--no-graph", is_flag=True, help="Exclude dependency graph")
@click.option("--include-unresolved", is_flag=True, help="Include unresolved references")
@click.option("--output-file", "-o", default=None, help="Write to file instead of stdout")
@click.option("--interactive", "-i", is_flag=True, help="Interactive export with LLM-guided customization")
@click.option("--query", "-q", default=None, help="Natural language query for export customization")
@click.option("--preset", "-p", default=None, help="Use a named preset (e.g., 'structure', 'api-surface')")
@click.option("--list-presets", is_flag=True, help="Show available presets and exit")
def export_cmd(
    target, fmt, max_tokens, no_graph, include_unresolved, output_file, interactive, query, preset, list_presets
):
    """Export documentation in a compact, AI-friendly format.

    \b
    Basic usage:
        codilay export .                          Markdown export to stdout
        codilay export . --format xml             XML format
        codilay export . -f json -t 8000          JSON with 8k token budget
        codilay export . -o context.md            Write to file

    \b
    Interactive and customized exports:
        codilay export . --interactive            LLM-guided export customization
        codilay export . --query "file structure and linkage only"
        codilay export . --preset structure       Use built-in preset
        codilay export . --list-presets           Show available presets
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    # List presets and exit if requested
    if list_presets:
        from codilay.interactive_export import show_presets
        from codilay.settings import Settings

        try:
            settings = Settings.load()
            custom_presets = getattr(settings, "export_presets", None)
        except Exception:
            custom_presets = None

        show_presets(custom_presets)
        return

    # Determine export mode
    spec = None

    if interactive:
        # Interactive mode - LLM conversation
        from codilay.config import CodiLayConfig
        from codilay.interactive_export import interactive_export_flow
        from codilay.llm_client import LLMClient
        from codilay.settings import Settings

        try:
            settings = Settings.load()
            config = CodiLayConfig()
            config.llm_provider = settings.default_provider
            config.llm_model = settings.get_effective_model()
            llm_client = LLMClient(config)
        except Exception as e:
            console.print(f"[red]Failed to initialize LLM client: {e}[/red]")
            return

        try:
            spec = interactive_export_flow(output_dir, llm_client)
        except KeyboardInterrupt:
            console.print("\n[yellow]Export cancelled[/yellow]")
            return
        except Exception as e:
            console.print(f"[red]Interactive export failed: {e}[/red]")
            return

    elif query:
        # Query mode - LLM translates query to spec
        import json as json_lib

        from codilay.config import CodiLayConfig
        from codilay.exporter import AIExporter
        from codilay.interactive_export import estimate_tokens, query_llm_for_spec
        from codilay.llm_client import LLMClient
        from codilay.settings import Settings
        from codilay.state import AgentState

        try:
            settings = Settings.load()
            config = CodiLayConfig()
            config.llm_provider = settings.default_provider
            config.llm_model = settings.get_effective_model()
            llm_client = LLMClient(config)
        except Exception as e:
            console.print(f"[red]Failed to initialize LLM client: {e}[/red]")
            return

        try:
            # Load state to get available sections
            state_path = os.path.join(output_dir, ".codilay_state.json")
            if not os.path.exists(state_path):
                console.print(f"[red]No state found at {state_path}[/red]")
                return

            state = AgentState.load(state_path)
            available_sections = list(state.section_index.keys())

            console.print(f"[dim]Interpreting query: '{query}'[/dim]")
            spec = query_llm_for_spec(query, available_sections, llm_client)

            # Load exporter for token estimation
            links_path = os.path.join(output_dir, "links.json")
            closed_wires = []
            open_wires = []
            project_name = ""

            if os.path.exists(links_path):
                with open(links_path, "r", encoding="utf-8") as f:
                    links = json_lib.load(f)
                closed_wires = links.get("closed", [])
                open_wires = links.get("open", [])
                project_name = links.get("project", "")

            exporter = AIExporter(
                section_index=state.section_index,
                section_contents=state.section_contents,
                closed_wires=closed_wires,
                open_wires=open_wires,
                project_name=project_name,
            )

            est_tokens, char_count = estimate_tokens(spec, exporter)
            console.print(f"[green]✓[/green] {spec.summary}")
            console.print(f"[dim]Estimated size: ~{est_tokens:,} tokens ({char_count:,} chars)[/dim]")
        except Exception as e:
            console.print(f"[red]Query interpretation failed: {e}[/red]")
            return

    elif preset:
        # Preset mode
        from codilay.export_spec import get_preset
        from codilay.settings import Settings

        try:
            settings = Settings.load()
            custom_presets = getattr(settings, "export_presets", None)
        except Exception:
            custom_presets = None

        spec = get_preset(preset, custom_presets)
        if spec is None:
            console.print(f"[red]Unknown preset: {preset}[/red]")
            console.print("\nUse --list-presets to see available presets")
            return

        console.print(f"[green]Using preset:[/green] {spec.summary}")

    # Export using spec or traditional parameters
    import json as json_lib

    from codilay.exporter import AIExporter, export_for_ai
    from codilay.state import AgentState

    try:
        if spec:
            # Spec-based export
            state_path = os.path.join(output_dir, ".codilay_state.json")
            if not os.path.exists(state_path):
                console.print(f"[red]No state found at {state_path}[/red]")
                return

            state = AgentState.load(state_path)

            links_path = os.path.join(output_dir, "links.json")
            closed_wires = []
            open_wires = []
            project_name = ""

            if os.path.exists(links_path):
                with open(links_path, "r", encoding="utf-8") as f:
                    links = json_lib.load(f)
                closed_wires = links.get("closed", [])
                open_wires = links.get("open", [])
                project_name = links.get("project", "")

            exporter = AIExporter(
                section_index=state.section_index,
                section_contents=state.section_contents,
                closed_wires=closed_wires,
                open_wires=open_wires,
                project_name=project_name,
            )

            result = exporter.export(spec=spec)
        else:
            # Traditional export
            result = export_for_ai(
                output_dir=output_dir,
                fmt=fmt,
                max_tokens=max_tokens,
                include_graph=not no_graph,
            )
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return
    except Exception as e:
        console.print(f"[red]Export failed: {e}[/red]")
        return

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(result)
        console.print(f"[green]Exported to {output_file}[/green] ({len(result):,} chars)")
    else:
        console.print(result)


# ─── Doc Diff command (Feature 4) ────────────────────────────────────────────


@cli.command("diff-doc")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--json-output", is_flag=True, help="Output as JSON")
def diff_doc(target, json_output):
    """Show what changed in the documentation between the last two runs.

    \b
    Unlike `codilay diff` which shows git file changes, this shows
    section-level content changes in the generated documentation.

    \b
    Examples:
        codilay diff-doc .                Show doc changelog
        codilay diff-doc . --json-output  Machine-readable output
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.doc_differ import DocVersionStore

    store = DocVersionStore(output_dir)
    snapshots = store.list_snapshots()

    if len(snapshots) < 2:
        console.print(
            "[yellow]Need at least 2 documentation snapshots to show a diff.[/yellow]\n"
            "[dim]Run [bold]codilay .[/bold] at least twice to compare versions.[/dim]"
        )
        return

    result = store.diff_latest()
    if result is None:
        console.print("[red]Could not compute documentation diff.[/red]")
        return

    if json_output:
        console.print(json.dumps(result.to_dict(), indent=2))
        return

    if not result.has_changes:
        console.print("[green]No documentation content changes between the last two runs.[/green]")
        return

    # Header
    console.print(
        Panel(
            f"[bold]Documentation Changelog[/bold]\n"
            f"From: [dim]{result.old_run_time or 'unknown'}[/dim]  →  "
            f"To: [dim]{result.new_run_time or 'unknown'}[/dim]\n"
            f"Sections: {result.sections_delta:+d} | "
            f"Wires closed: {result.new_closed_wires:+d} | "
            f"Wires opened: {result.new_open_wires:+d}",
            border_style="blue",
        )
    )

    # Added sections
    if result.added_sections:
        console.print(f"\n[bold green]+ {len(result.added_sections)} sections added:[/bold green]")
        for sc in result.added_sections:
            console.print(f"  [green]+[/green] {sc.title} [dim]({sc.section_id})[/dim]")

    # Removed sections
    if result.removed_sections:
        console.print(f"\n[bold red]- {len(result.removed_sections)} sections removed:[/bold red]")
        for sc in result.removed_sections:
            console.print(f"  [red]-[/red] {sc.title} [dim]({sc.section_id})[/dim]")

    # Modified sections
    if result.modified_sections:
        console.print(f"\n[bold yellow]~ {len(result.modified_sections)} sections modified:[/bold yellow]")
        for sc in result.modified_sections:
            console.print(f"  [yellow]~[/yellow] {sc.title} [dim]({sc.section_id})[/dim]")
            if sc.summary:
                console.print(f"    {sc.summary}")
            # Show a few diff lines
            for dl in sc.diff_lines[:5]:
                if dl.startswith("+"):
                    console.print(f"    [green]{dl}[/green]")
                elif dl.startswith("-"):
                    console.print(f"    [red]{dl}[/red]")
            if len(sc.diff_lines) > 5:
                console.print(f"    [dim]… +{len(sc.diff_lines) - 5} more lines[/dim]")

    console.print(f"\n[bold]Total: {result.total_section_changes} section changes[/bold]")


# ─── Diff-run command ─────────────────────────────────────────────────────────


@cli.command("diff-run")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--since", help="Boundary commit, tag, or date (YYYY-MM-DD)")
@click.option("--since-branch", help="Compare against a branch (e.g., main)")
@click.option("--update-doc", is_flag=True, help="Update CODEBASE.md with changes")
@common_options
def diff_run(target, since, since_branch, update_doc, config, output, model, provider, base_url, verbose):
    """Document only changes since a specific boundary.

    \b
    Produces a focused change report instead of full codebase documentation.
    Analyzes what changed (diffs for modified files, full content for new files)
    and generates an impact analysis.

    \b
    Boundary options:
        --since abc123f          Commit hash
        --since v2.1.0           Tag
        --since 2024-03-01       Date
        --since-branch main      Branch (finds merge base)

    \b
    Examples:
        codilay diff-run . --since abc123f
        codilay diff-run . --since v2.1.0
        codilay diff-run . --since-branch main
        codilay diff-run . --since 2024-03-01
        codilay diff-run . --since-branch feature --update-doc
    """
    from codilay.change_report import ChangeReportGenerator
    from codilay.diff_analyzer import DiffAnalyzer
    from codilay.llm_client import LLMClient
    from codilay.prompts import diff_run_analysis_prompt, diff_run_system_prompt

    if not since and not since_branch:
        console.print(
            "[red]Error:[/red] Must specify either --since or --since-branch\n"
            "[dim]Examples:[/dim]\n"
            "  codilay diff-run . --since abc123f\n"
            "  codilay diff-run . --since-branch main"
        )
        return

    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")
    os.makedirs(output_dir, exist_ok=True)

    ui = UI(console, verbose)

    # ── Git validation ────────────────────────────────────────────
    ui.phase("Analyzing changes with diff-run")

    analyzer = DiffAnalyzer(target)
    if not analyzer.is_git_repo:
        console.print("[red]Error:[/red] Target directory is not a git repository.")
        return

    # ── Resolve boundary ──────────────────────────────────────────
    with ui.spinner("Resolving boundary..."):
        boundary_result = analyzer.resolve_boundary(since=since, since_branch=since_branch)

    if not boundary_result:
        console.print(
            f"[red]Error:[/red] Could not resolve boundary: "
            f"{since or since_branch}\n"
            "[dim]Make sure the commit/tag/branch exists and the date format is YYYY-MM-DD[/dim]"
        )
        return

    base_commit, boundary_type = boundary_result
    boundary_ref = since or since_branch or base_commit

    console.print(
        f"[green]Boundary resolved:[/green] {boundary_ref} [dim]({boundary_type}, commit {base_commit[:8]})[/dim]"
    )

    # ── Analyze changes ───────────────────────────────────────────
    with ui.spinner("Extracting diffs and changes..."):
        diff_result = analyzer.analyze(since=since, since_branch=since_branch)

    if not diff_result:
        console.print("[red]Error:[/red] Failed to analyze changes.")
        return

    if not diff_result.file_diffs:
        console.print("[green]No changes detected since the boundary.[/green]")
        return

    # Show summary
    console.print(
        f"\n[bold]Changes detected:[/bold]\n"
        f"  [green]+{len(diff_result.added_files)} added[/green]\n"
        f"  [yellow]~{len(diff_result.modified_files)} modified[/yellow]\n"
        f"  [red]-{len(diff_result.deleted_files)} deleted[/red]\n"
        f"  [cyan]→{len(diff_result.renamed_files)} renamed[/cyan]\n"
        f"  [dim]{diff_result.commits_count} commits[/dim]"
    )

    # ── Load config and LLM ───────────────────────────────────────
    cfg = CodiLayConfig.load(target, config)

    # Apply CLI overrides
    if provider:
        cfg.llm_provider = provider
    if model:
        cfg.llm_model = model
    if base_url:
        cfg.llm_base_url = base_url

    llm = LLMClient(cfg)

    # ── Prepare LLM analysis ──────────────────────────────────────
    ui.phase("Analyzing impact with LLM")

    # Load existing doc sections for modified files (if available)
    # TODO: For now, we skip this optimization. In the future, we could load
    # the existing CODEBASE.md sections to provide "before" context to the LLM.
    existing_sections = {}
    section_index = []

    # Format file diffs for LLM
    added_files = []
    for f in diff_result.added_files:
        added_files.append({"path": f.path, "content": f.full_content or ""})

    modified_files = []
    for f in diff_result.modified_files:
        modified_files.append({"path": f.path, "diff": f.diff_content or ""})

    deleted_files = []
    for f in diff_result.deleted_files:
        deleted_files.append({"path": f.path})

    renamed_files = []
    for f in diff_result.renamed_files:
        renamed_files.append(
            {
                "path": f.path,
                "old_path": f.old_path or "",
                "diff": f.diff_content or "",
            }
        )

    # Build prompts
    sys_prompt = diff_run_system_prompt(
        cfg,
        response_style=getattr(cfg, "response_style", "technical"),
        detail_level=getattr(cfg, "detail_level", "standard"),
    )

    user_prompt = diff_run_analysis_prompt(
        boundary_ref=boundary_ref,
        boundary_type=boundary_type,
        commits_count=diff_result.commits_count,
        commit_messages=diff_result.commit_messages,
        added_files=added_files,
        modified_files=modified_files,
        deleted_files=deleted_files,
        renamed_files=renamed_files,
        existing_sections=existing_sections,
        section_index=section_index,
    )

    # Call LLM
    with ui.spinner("Generating change analysis..."):
        result = llm.call(sys_prompt, user_prompt)

    if "error" in result:
        console.print(f"[red]LLM error:[/red] {result.get('error')}")
        return

    # ── Generate report ───────────────────────────────────────────
    ui.phase("Writing change report")

    report_gen = ChangeReportGenerator(output_dir)
    report_path = report_gen.generate_report(
        analysis_result=result,
        boundary_ref=boundary_ref,
        boundary_type=boundary_type,
        commits_count=diff_result.commits_count,
        commit_messages=diff_result.commit_messages,
    )

    console.print(f"\n[green]✓ Change report generated:[/green] [cyan]{report_path}[/cyan]")

    # ── Update main doc (optional) ────────────────────────────────
    if update_doc:
        console.print("\n[dim]Updating CODEBASE.md with changes...[/dim]")
        # TODO: Implement docstore integration to patch existing sections
        # and add new sections from the diff-run analysis
        console.print("[yellow]Note:[/yellow] --update-doc integration is coming soon.")

    # ── Show LLM usage ────────────────────────────────────────────
    stats = llm.get_usage_stats()
    ui.info(
        f"LLM usage: {stats['total_calls']} calls, "
        f"{stats['total_input_tokens']:,} input tokens, "
        f"{stats['total_output_tokens']:,} output tokens"
    )


# ─── Triage Feedback command (Feature 5) ─────────────────────────────────────


@cli.group("triage-feedback")
def triage_feedback_group():
    """Manage triage feedback to improve file classification.

    \b
    Examples:
        codilay triage-feedback add src/utils.py skip core "Important utility"
        codilay triage-feedback list
        codilay triage-feedback clear
    """
    pass


@triage_feedback_group.command("add")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("file_path")
@click.argument("original", type=click.Choice(["core", "skim", "skip"]))
@click.argument("corrected", type=click.Choice(["core", "skim", "skip"]))
@click.option("--reason", "-r", default="", help="Reason for the correction")
@click.option("--pattern", is_flag=True, help="Treat file_path as a glob pattern")
def triage_feedback_add(target, file_path, original, corrected, reason, pattern):
    """Record a triage correction for a file or pattern."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.triage_feedback import TriageFeedbackStore

    store = TriageFeedbackStore(output_dir)
    entry = store.add_feedback(file_path, original, corrected, reason=reason, is_pattern=pattern)
    console.print(
        f"[green]Recorded:[/green] {entry.file_path}: "
        f"{entry.original_category} → [bold]{entry.corrected_category}[/bold]"
    )
    if reason:
        console.print(f"  [dim]Reason: {reason}[/dim]")


@triage_feedback_group.command("list")
@click.argument("target", default=".", type=click.Path(exists=True))
def triage_feedback_list(target):
    """Show all stored triage feedback."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.triage_feedback import TriageFeedbackStore

    store = TriageFeedbackStore(output_dir)
    entries = store.list_feedback()

    if not entries:
        console.print("[dim]No triage feedback recorded.[/dim]")
        return

    table = Table(title="Triage Feedback", box=box.ROUNDED)
    table.add_column("File / Pattern", style="cyan")
    table.add_column("Original", style="red")
    table.add_column("Corrected", style="green")
    table.add_column("Reason", style="dim")
    table.add_column("Type", style="dim")

    for e in entries:
        table.add_row(
            e.file_path,
            e.original_category,
            e.corrected_category,
            e.reason or "-",
            "pattern" if e.is_pattern else "file",
        )

    console.print(table)

    hints = store.get_project_hints()
    if hints:
        console.print("\n[bold]Project hints:[/bold]")
        for pt, hint in hints.items():
            console.print(f"  [cyan]{pt}[/cyan]: {hint}")


@triage_feedback_group.command("hint")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("project_type")
@click.argument("hint")
def triage_feedback_hint(target, project_type, hint):
    """Set a triage hint for a project type.

    \b
    Example:
        codilay triage-feedback hint . django "Always include migration files"
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.triage_feedback import TriageFeedbackStore

    store = TriageFeedbackStore(output_dir)
    store.set_project_hint(project_type, hint)
    console.print(f"[green]Hint set for [cyan]{project_type}[/cyan]:[/green] {hint}")


@triage_feedback_group.command("clear")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def triage_feedback_clear(target, yes):
    """Clear all triage feedback."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.triage_feedback import TriageFeedbackStore

    store = TriageFeedbackStore(output_dir)

    if not yes:
        count = len(store.list_feedback())
        if count == 0:
            console.print("[dim]No feedback to clear.[/dim]")
            return
        if not click.confirm(f"Clear {count} feedback entries?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return

    store.clear_feedback()
    console.print("[green]All triage feedback cleared.[/green]")


@triage_feedback_group.command("remove")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("file_path")
def triage_feedback_remove(target, file_path):
    """Remove feedback for a specific file or pattern."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.triage_feedback import TriageFeedbackStore

    store = TriageFeedbackStore(output_dir)
    if store.remove_feedback(file_path):
        console.print(f"[green]Removed feedback for {file_path}[/green]")
    else:
        console.print(f"[yellow]No feedback found for {file_path}[/yellow]")


# ─── Graph command (Feature 7) ───────────────────────────────────────────────


@cli.command("graph")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--wire-type", "-w", multiple=True, help="Filter by wire type (can repeat)")
@click.option("--layer", "-l", multiple=True, help="Filter by layer/directory")
@click.option("--module", "-M", multiple=True, help="Filter by module/file")
@click.option("--exclude", "-x", multiple=True, help="Exclude files matching pattern")
@click.option(
    "--direction",
    "-d",
    default="both",
    type=click.Choice(["incoming", "outgoing", "both"]),
    help="Edge direction filter",
)
@click.option("--min-connections", default=0, type=int, help="Minimum connections to show a node")
@click.option("--json-output", is_flag=True, help="Output as JSON")
@click.option("--list-filters", is_flag=True, help="Show available filter values")
def graph_cmd(target, wire_type, layer, module, exclude, direction, min_connections, json_output, list_filters):
    """View and filter the dependency graph.

    \b
    Examples:
        codilay graph .                              Full graph
        codilay graph . --list-filters               Show available filter values
        codilay graph . -w import -w call             Only imports and calls
        codilay graph . -l src/services               Only files in src/services
        codilay graph . --min-connections 3           Hide isolated nodes
        codilay graph . --json-output                 Machine-readable output
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")
    links_path = os.path.join(output_dir, "links.json")

    if not os.path.exists(links_path):
        console.print(
            f"[red]No links.json found at {output_dir}[/red]\n"
            f"[dim]Run [bold]codilay {target}[/bold] first to generate docs.[/dim]"
        )
        return

    with open(links_path, "r", encoding="utf-8") as f:
        links = json.load(f)

    from codilay.graph_filter import GraphFilter, GraphFilterOptions

    gf = GraphFilter(
        closed_wires=links.get("closed", []),
        open_wires=links.get("open", []),
    )

    if list_filters:
        available = gf.get_available_filters()
        console.print("[bold]Available filter values:[/bold]\n")
        console.print("[cyan]Wire types:[/cyan]")
        for wt in available.get("wire_types", []):
            console.print(f"  {wt}")
        console.print("\n[cyan]Layers (directories):[/cyan]")
        for ly in available.get("layers", []):
            console.print(f"  {ly}")
        console.print(f"\n[cyan]Files:[/cyan] {len(available.get('files', []))} total")
        return

    options = GraphFilterOptions(
        wire_types=list(wire_type) if wire_type else None,
        layers=list(layer) if layer else None,
        modules=list(module) if module else None,
        exclude_files=list(exclude) if exclude else None,
        direction=direction,
        min_connections=min_connections,
    )

    result = gf.filter(options)

    if json_output:
        console.print(json.dumps(result.to_dict(), indent=2))
        return

    # Display summary
    console.print(
        Panel(
            f"[bold]Dependency Graph[/bold]\n"
            f"  Nodes: [cyan]{len(result.nodes)}[/cyan] | "
            f"Edges: [cyan]{len(result.edges)}[/cyan] | "
            f"Filtered: {result.filtered_wires}/{result.total_wires} wires",
            border_style="blue",
        )
    )

    if not result.edges:
        console.print("[dim]No edges match the current filters.[/dim]")
        return

    # Show edges
    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("Source", style="cyan", max_width=40)
    table.add_column("→", style="dim", width=2)
    table.add_column("Target", style="green", max_width=40)
    table.add_column("Type", style="yellow")

    for edge in result.edges[:50]:
        table.add_row(edge.source, "→", edge.target, edge.wire_type)

    console.print(table)

    if len(result.edges) > 50:
        console.print(f"[dim]… +{len(result.edges) - 50} more edges (use --json-output to see all)[/dim]")

    # Show nodes with most connections
    if result.nodes:
        top_nodes = sorted(result.nodes, key=lambda n: n.incoming + n.outgoing, reverse=True)[:10]
        console.print("\n[bold]Most connected nodes:[/bold]")
        for node in top_nodes:
            console.print(
                f"  [cyan]{node.path}[/cyan] — {node.incoming} in, {node.outgoing} out [dim]({node.layer})[/dim]"
            )


# ─── Team Memory command (Feature 8) ─────────────────────────────────────────


@cli.group("team")
def team_group():
    """Manage shared team knowledge base.

    \b
    Examples:
        codilay team facts .
        codilay team add-fact . "The auth module uses JWT tokens" --category architecture
        codilay team decisions .
        codilay team conventions .
    """
    pass


@team_group.command("facts")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--category", "-c", default=None, help="Filter by category")
def team_facts(target, category):
    """List all team facts."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    facts = tm.list_facts(category=category)

    if not facts:
        console.print("[dim]No team facts recorded.[/dim]")
        return

    table = Table(title="Team Facts", box=box.ROUNDED)
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Fact", style="cyan")
    table.add_column("Category", style="yellow")
    table.add_column("Votes", justify="right")
    table.add_column("Author", style="dim")

    for f in facts:
        votes = f.get("votes_up", 0) - f.get("votes_down", 0)
        vote_str = f"[green]+{votes}[/green]" if votes >= 0 else f"[red]{votes}[/red]"
        table.add_row(
            f["id"][:8],
            f["fact"],
            f.get("category", "general"),
            vote_str,
            f.get("author", "-"),
        )

    console.print(table)


@team_group.command("add-fact")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("fact")
@click.option("--category", "-c", default="general", help="Fact category")
@click.option("--author", "-a", default="", help="Author name")
@click.option("--tag", "-t", multiple=True, help="Tags (can repeat)")
def team_add_fact(target, fact, category, author, tag):
    """Add a fact to the team knowledge base."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    entry = tm.add_fact(fact, category=category, author=author, tags=list(tag) if tag else None)
    console.print(f"[green]Fact added:[/green] {entry['fact']} [dim]({entry['id'][:8]})[/dim]")


@team_group.command("vote")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("fact_id")
@click.argument("direction", type=click.Choice(["up", "down"]))
def team_vote(target, fact_id, direction):
    """Vote on a fact (up or down)."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    # Match partial ID
    all_facts = tm.list_facts()
    full_id = None
    for f in all_facts:
        if f["id"].startswith(fact_id):
            full_id = f["id"]
            break

    if not full_id:
        console.print(f"[red]No fact matching '{fact_id}'[/red]")
        return

    if tm.vote_fact(full_id, direction):
        console.print(f"[green]Voted {direction} on fact {fact_id[:8]}[/green]")
    else:
        console.print("[red]Could not vote on fact[/red]")


@team_group.command("decisions")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option(
    "--status", "-s", default=None, type=click.Choice(["active", "superseded", "deprecated"]), help="Filter by status"
)
def team_decisions(target, status):
    """List team decisions."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    decisions = tm.list_decisions(status=status)

    if not decisions:
        console.print("[dim]No team decisions recorded.[/dim]")
        return

    table = Table(title="Team Decisions", box=box.ROUNDED)
    table.add_column("ID", style="dim", max_width=8)
    table.add_column("Title", style="cyan")
    table.add_column("Status", style="yellow")
    table.add_column("Author", style="dim")
    table.add_column("Date", style="dim")

    for d in decisions:
        table.add_row(
            d["id"][:8],
            d["title"],
            d.get("status", "active"),
            d.get("author", "-"),
            d.get("created_at", "")[:10],
        )

    console.print(table)


@team_group.command("add-decision")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("title")
@click.argument("description")
@click.option("--author", "-a", default="", help="Author name")
@click.option("--file", "-f", "related_files", multiple=True, help="Related files (can repeat)")
def team_add_decision(target, title, description, author, related_files):
    """Record a team decision."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    entry = tm.add_decision(
        title, description, author=author, related_files=list(related_files) if related_files else None
    )
    console.print(f"[green]Decision recorded:[/green] {entry['title']} [dim]({entry['id'][:8]})[/dim]")


@team_group.command("conventions")
@click.argument("target", default=".", type=click.Path(exists=True))
def team_conventions(target):
    """List team conventions."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    conventions = tm.list_conventions()

    if not conventions:
        console.print("[dim]No team conventions recorded.[/dim]")
        return

    for c in conventions:
        console.print(f"\n[bold cyan]{c['name']}[/bold cyan]")
        console.print(f"  {c['description']}")
        if c.get("examples"):
            for ex in c["examples"]:
                console.print(f"    [dim]• {ex}[/dim]")


@team_group.command("add-convention")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("name")
@click.argument("description")
@click.option("--example", "-e", multiple=True, help="Examples (can repeat)")
@click.option("--author", "-a", default="", help="Author name")
def team_add_convention(target, name, description, example, author):
    """Add a coding convention."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    entry = tm.add_convention(name, description, examples=list(example) if example else None, author=author)
    console.print(f"[green]Convention added:[/green] {entry['name']}")


@team_group.command("annotate")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("file_path")
@click.argument("note")
@click.option("--author", "-a", default="", help="Author name")
@click.option("--lines", "-l", default=None, help="Line range (e.g., '10-25')")
def team_annotate(target, file_path, note, author, lines):
    """Add a note about a specific file."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    tm.add_annotation(file_path, note, author=author, line_range=lines)
    console.print(f"[green]Annotation added for [cyan]{file_path}[/cyan][/green]")


@team_group.command("annotations")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--file", "-f", "file_path", default=None, help="Filter by file")
def team_annotations(target, file_path):
    """List file annotations."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    annotations = tm.get_annotations(file_path=file_path)

    if not annotations:
        console.print("[dim]No annotations found.[/dim]")
        return

    for a in annotations:
        line_info = f" (lines {a['line_range']})" if a.get("line_range") else ""
        console.print(f"  [cyan]{a['file_path']}{line_info}[/cyan]: {a['note']} [dim]— {a.get('author', 'anon')}[/dim]")


@team_group.command("users")
@click.argument("target", default=".", type=click.Path(exists=True))
def team_users(target):
    """List registered team members."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    users = tm.list_users()

    if not users:
        console.print("[dim]No team members registered.[/dim]")
        return

    for u in users:
        display = u.get("display_name", u["username"])
        console.print(f"  [cyan]{display}[/cyan] [dim](@{u['username']})[/dim]")


@team_group.command("add-user")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("username")
@click.option("--display-name", "-n", default="", help="Display name")
def team_add_user(target, username, display_name):
    """Register a team member."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.team_memory import TeamMemory

    tm = TeamMemory(output_dir)
    user = tm.register_user(username, display_name=display_name)
    console.print(f"[green]User registered:[/green] @{user['username']}")


# ─── Search command (Feature 9) ──────────────────────────────────────────────


@cli.command("search")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.argument("query")
@click.option("--top", "-k", default=10, type=int, help="Number of results")
@click.option("--role", "-r", default=None, type=click.Choice(["user", "assistant"]), help="Filter by message role")
@click.option("--conversation", "-c", default=None, help="Limit to specific conversation ID")
@click.option("--rebuild", is_flag=True, help="Rebuild the search index before searching")
def search_cmd(target, query, top, role, conversation, rebuild):
    """Search across all past conversations.

    \b
    Examples:
        codilay search . "authentication flow"
        codilay search . "error handling" --role assistant
        codilay search . "database" -k 5
        codilay search . "deploy" --rebuild
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.search import ConversationSearch

    searcher = ConversationSearch(output_dir)

    if rebuild or not searcher.load_index():
        with console.status("[dim]Building search index...[/dim]"):
            searcher.build_index()

    results = searcher.search(
        query=query,
        top_k=top,
        role_filter=role,
        conv_id_filter=conversation,
    )

    if not results.results:
        console.print(f"[dim]No results for '{query}'.[/dim]")
        console.print(
            f"[dim]Searched {results.total_conversations_searched} conversations, "
            f"{results.total_messages_searched} messages.[/dim]"
        )
        return

    console.print(
        f"\n[bold]Search results for '{query}'[/bold] "
        f"({len(results.results)} hits from "
        f"{results.total_conversations_searched} conversations)\n"
    )

    for i, r in enumerate(results.results, 1):
        role_badge = "[green]You[/green]" if r.role == "user" else "[blue]CodiLay[/blue]"
        deep_badge = " [yellow]deep[/yellow]" if r.escalated else ""
        console.print(
            f"  [bold]{i}.[/bold] {role_badge}{deep_badge} "
            f"[dim]({r.conversation_title})[/dim] "
            f"[dim]score: {r.score:.2f}[/dim]"
        )
        console.print(f"     {r.snippet}")
        console.print()


# ─── Schedule command (Feature 10) ───────────────────────────────────────────


@cli.group("schedule")
def schedule_group():
    """Schedule automatic documentation updates.

    \b
    Examples:
        codilay schedule set . --cron "0 2 * * *"    Daily at 2 AM
        codilay schedule set . --on-commit            On new commits
        codilay schedule status .
        codilay schedule start .
        codilay schedule stop .
    """
    pass


@schedule_group.command("set")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--cron", default=None, help="Cron expression (5 fields)")
@click.option("--on-commit", is_flag=True, help="Trigger on new commits")
@click.option("--branch", "-b", default="main", help="Branch to monitor")
def schedule_set(target, cron, on_commit, branch):
    """Configure a schedule for automatic documentation updates."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")
    os.makedirs(output_dir, exist_ok=True)

    from codilay.scheduler import ScheduleConfig

    config = ScheduleConfig(output_dir)

    if cron:
        try:
            config.set_cron(cron, branch=branch)
            console.print(f"[green]Cron schedule set:[/green] [cyan]{cron}[/cyan] on branch [cyan]{branch}[/cyan]")
        except ValueError as e:
            console.print(f"[red]Invalid cron expression: {e}[/red]")
            return
    elif on_commit:
        config.set_on_commit(branch=branch)
        console.print(f"[green]Commit-triggered schedule set[/green] on branch [cyan]{branch}[/cyan]")
    else:
        console.print("[yellow]Specify --cron or --on-commit[/yellow]")
        return


@schedule_group.command("disable")
@click.argument("target", default=".", type=click.Path(exists=True))
def schedule_disable(target):
    """Disable the schedule."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.scheduler import ScheduleConfig

    config = ScheduleConfig(output_dir)
    config.disable()
    console.print("[green]Schedule disabled.[/green]")


@schedule_group.command("status")
@click.argument("target", default=".", type=click.Path(exists=True))
def schedule_status(target):
    """Show current schedule configuration."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.scheduler import ScheduleConfig, read_pid_file

    config = ScheduleConfig(output_dir)
    cfg = config.load()

    if not cfg.get("enabled", False):
        console.print("[dim]No active schedule.[/dim]")
        return

    mode = cfg.get("mode", "disabled")
    console.print("[bold]Schedule:[/bold] [green]enabled[/green]")
    console.print(f"  Mode: [cyan]{mode}[/cyan]")
    if mode == "cron":
        console.print(f"  Expression: [cyan]{cfg.get('cron', '')}[/cyan]")
    console.print(f"  Branch: [cyan]{cfg.get('branch', 'main')}[/cyan]")

    if cfg.get("last_run"):
        console.print(f"  Last run: [dim]{cfg['last_run']}[/dim]")
    if cfg.get("last_commit"):
        console.print(f"  Last commit: [dim]{cfg['last_commit'][:8]}[/dim]")

    pid = read_pid_file(output_dir)
    if pid:
        console.print(f"  Scheduler PID: [cyan]{pid}[/cyan] [green](running)[/green]")
    else:
        console.print("  Scheduler: [yellow]not running[/yellow]")


@schedule_group.command("start")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--verbose", "-v", is_flag=True, help="Verbose output")
def schedule_start(target, verbose):
    """Start the scheduler (runs in foreground).

    \b
    Use with a process manager (systemd, pm2, tmux) for background operation.
    """
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.scheduler import ScheduleConfig, Scheduler

    config = ScheduleConfig(output_dir)
    cfg = config.load()

    if not cfg.get("enabled", False):
        console.print(
            "[yellow]No schedule configured.[/yellow]\n[dim]Run [bold]codilay schedule set[/bold] first.[/dim]"
        )
        return

    console.print(
        Panel(
            f"[bold]CodiLay Scheduler[/bold]\n\n"
            f"  Project:  [cyan]{os.path.basename(target)}[/cyan]\n"
            f"  Mode:     [cyan]{cfg.get('mode', 'unknown')}[/cyan]\n"
            f"  Branch:   [cyan]{cfg.get('branch', 'main')}[/cyan]\n\n"
            f"[dim]Press Ctrl+C to stop.[/dim]",
            border_style="blue",
            title="scheduler",
        )
    )

    scheduler = Scheduler(target_path=target, output_dir=output_dir, verbose=verbose)
    scheduler.start()


@schedule_group.command("stop")
@click.argument("target", default=".", type=click.Path(exists=True))
def schedule_stop(target):
    """Stop a running scheduler by PID."""
    import signal

    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    from codilay.scheduler import read_pid_file, remove_pid_file

    pid = read_pid_file(output_dir)
    if not pid:
        console.print("[dim]No scheduler running (no PID file found).[/dim]")
        return

    try:
        os.kill(pid, signal.SIGTERM)
        remove_pid_file(output_dir)
        console.print(f"[green]Sent SIGTERM to scheduler (PID {pid}).[/green]")
    except ProcessLookupError:
        remove_pid_file(output_dir)
        console.print(f"[yellow]Scheduler (PID {pid}) was not running. Cleaned up PID file.[/yellow]")
    except PermissionError:
        console.print(f"[red]Permission denied sending signal to PID {pid}.[/red]")


@cli.command("audit")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option(
    "--type", "-t", "audit_type", required=True, help="Type of audit (e.g. security, performance, architecture)"
)
@click.option(
    "--mode", "-m", type=click.Choice(["passive", "active"]), default="passive", help="Audit mode: passive or active"
)
@click.pass_context
def audit_command(ctx, target, audit_type, mode):
    """Run an AI audit against the generated CodiLay documentation."""
    target = os.path.abspath(target)
    output_dir = ctx.obj.get("output", os.path.join(target, "codilay"))
    config_path = ctx.obj.get("config_path")

    settings = ctx.obj.get("settings", Settings.load())
    cfg = CodiLayConfig.load(target, config_path)

    from codilay.audit_manager import AuditManager
    from codilay.ui import UI

    ui = UI(console, False)
    ui.phase(f"Running {audit_type.upper()} Audit in {mode.upper()} mode")

    # Load state
    state_path = os.path.join(output_dir, ".codilay_state.json")
    if not os.path.exists(state_path):
        ui.error("No CodiLay state found. Please run 'codilay .' first.")
        return

    state = AgentState.load(state_path)

    # Set up LLM
    from codilay.llm_client import LLMClient

    llm = LLMClient(cfg)

    scanner = None
    if mode == "active":
        scanner = Scanner(target, cfg, output_dir=output_dir)

    audit_mgr = AuditManager(llm_client=llm, output_dir=output_dir)

    with ui.spinner("Auditing codebase..."):
        result = audit_mgr.run_audit(
            audit_type=audit_type,
            mode=mode,
            section_contents=state.section_contents,
            open_wires=state.open_wires,
            closed_wires=state.closed_wires,
            target_path=target,
            scanner=scanner,
        )

    ui.success(f"Audit complete! Report saved to {result['report_path']}")


# ─── Annotate command ──────────────────────────────────────────────────────────


@cli.command("annotate")
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option(
    "--scope",
    "-s",
    multiple=True,
    default=None,
    help="Restrict annotation to specific files or folders. Can be supplied multiple times.",
)
@click.option(
    "--exclude",
    "-e",
    multiple=True,
    default=None,
    help="Exclude these paths or glob patterns. Can be supplied multiple times.",
)
@click.option(
    "--level",
    "-l",
    type=click.Choice(["docstrings", "inline", "full"]),
    default="docstrings",
    show_default=True,
    help="Annotation level: docstrings only, inline only, or both.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview annotations without writing any files.")
@click.option("--rollback", "rollback_id", default=None, help="Undo a previous annotation run by its run ID.")
@click.option("--no-git-check", is_flag=True, default=False, help="Skip the git working tree clean check.")
@click.pass_context
def annotate(ctx, target, scope, exclude, level, dry_run, rollback_id, no_git_check):
    """Add documentation comments to source files using CodiLay's wire knowledge.

    \b
    CodiLay reads each source file and writes language-appropriate docstrings and
    comments back into the code, enriched with wire connection data (what calls
    what, what depends on what).

    \b
    Safety guards (configurable in settings):
      • Requires a clean git working tree (use --no-git-check to bypass)
      • --dry-run shows a preview diff without touching any files
      • Backups are saved for rollback: codilay annotate . --rollback <run-id>

    \b
    Examples:
        codilay annotate .                       Annotate whole project
        codilay annotate . --dry-run             Preview only
        codilay annotate . --scope src/auth/     Annotate one folder
        codilay annotate . --level full          Docstrings + inline
        codilay annotate . --rollback 20240314_120000  Undo a run
    """
    from codilay.annotator import Annotator, check_git_clean
    from codilay.scanner import Scanner
    from codilay.state import AgentState
    from codilay.ui import UI

    target = os.path.abspath(target)
    settings: Settings = ctx.obj["settings"]
    config_path = ctx.obj.get("config_path")
    output_dir = ctx.obj.get("output") or os.path.join(target, "codilay")

    ui = UI(console, ctx.obj.get("verbose", False))
    ui.show_banner()

    cfg = CodiLayConfig.load(target, config_path)
    provider = ctx.obj.get("provider") or settings.default_provider
    model_override = ctx.obj.get("model")
    base_url = ctx.obj.get("base_url")

    if provider:
        cfg.llm_provider = provider
    if model_override:
        # CLI flag always wins
        cfg.llm_model = model_override
    elif settings.annotate_model:
        # Dedicated annotate model in settings takes next priority
        cfg.llm_model = settings.annotate_model
    elif not settings.annotate_use_config_model:
        # Config file model is suppressed — use the global default_model the user selected
        cfg.llm_model = settings.default_model
    # else: annotate_use_config_model=True → keep whatever CodiLayConfig loaded
    if base_url:
        cfg.llm_base_url = base_url

    # Apply reasoning settings
    if settings.reasoning_enabled:
        cfg.thinking_budget_tokens = settings.reasoning_budget_tokens
        cfg.reasoning_effort = settings.reasoning_effort

    # ── Rollback mode ─────────────────────────────────────────────
    if rollback_id:
        llm = LLMClient(cfg)
        annotator = Annotator(llm, settings, ui, target, output_dir)
        annotator.rollback(rollback_id)
        return

    # ── Git clean check ───────────────────────────────────────────
    if settings.annotate_require_git_clean and not no_git_check and not dry_run:
        is_clean, status_msg = check_git_clean(target)
        if not is_clean:
            ui.error(
                f"Working tree is not clean ({status_msg}).\n"
                "Commit or stash your changes before annotating, or use --no-git-check to bypass.\n"
                "Without a clean tree you cannot easily undo CodiLay's annotations."
            )
            return

    # ── Load state for wire data ──────────────────────────────────
    state_path = os.path.join(output_dir, ".codilay_state.json")
    closed_wires = []
    section_contents = {}
    if os.path.exists(state_path):
        state = AgentState.load(state_path)
        closed_wires = state.closed_wires
        section_contents = state.section_contents
        ui.info(f"Loaded wire data: {len(closed_wires)} closed wires")
    else:
        ui.warn("No CodiLay state found — wire connections won't be included in annotations. Run 'codilay .' first.")

    # ── Scan files ────────────────────────────────────────────────
    scanner = Scanner(target, cfg, output_dir=output_dir)
    all_files = scanner.get_all_files()

    if not all_files:
        ui.error("No files found in target directory.")
        return

    scope_list = list(scope) if scope else []
    exclude_list = list(exclude) if exclude else []

    # ── Run annotation ────────────────────────────────────────────
    llm = LLMClient(cfg)
    annotator = Annotator(llm, settings, ui, target, output_dir)

    annotator.run(
        files=all_files,
        level=level,
        dry_run=dry_run,
        scope=scope_list or None,
        exclude=exclude_list or None,
        wire_data=closed_wires,
        section_contents=section_contents,
    )
