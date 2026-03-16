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
"""

import sys
import os
import json
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    BarColumn,
    TaskProgressColumn,
)
from rich.table import Table
from rich import box

from codilay.config import CodiLayConfig
from codilay.scanner import Scanner
from codilay.planner import Planner
from codilay.processor import Processor
from codilay.wire_manager import WireManager
from codilay.docstore import DocStore
from codilay.state import AgentState
from codilay.llm_client import LLMClient, ALL_PROVIDERS
from codilay.git_tracker import GitTracker, ChangeType
from codilay.ui import UI
from codilay.settings import Settings

console = Console()


def common_options(fn):
    fn = click.option(
        "--config", "-c", default=None, help="Path to codilay.config.json"
    )(fn)
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
@click.pass_context
def run(ctx, target):
    """Run the documentation agent (default command)."""
    settings: Settings = ctx.obj["settings"]
    target = os.path.abspath(target)
    config_path = ctx.obj["config_path"]
    output_dir = ctx.obj["output"]
    model_override = ctx.obj["model"]
    provider = ctx.obj["provider"] or settings.default_provider
    base_url = ctx.obj["base_url"]
    verbose = ctx.obj["verbose"]

    ui = UI(console, verbose)
    ui.show_banner()

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

    ui.show_config(cfg)

    # ── Resolve paths ────────────────────────────────────────────
    if output_dir is None:
        output_dir = os.path.join(target, "codilay")

    state_path = os.path.join(output_dir, ".codilay_state.json")
    codebase_md_path = os.path.join(output_dir, "CODEBASE.md")
    os.makedirs(output_dir, exist_ok=True)

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
    scanner = Scanner(target, cfg)
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

    ui.info(
        f"Found [bold]{len(all_files)}[/bold] files "
        f"({len(md_contents)} markdown preloaded)"
    )
    if verbose:
        ui.show_file_tree(file_tree_text)

    # ── Phase 1.5: Triage ────────────────────────────────────────
    # (only on full runs or when processing all files)

    files_to_process = all_files
    triage_result = None

    if mode == "full" and cfg.triage_mode != "none":
        ui.phase("Phase 1.5 · Triage — Classifying files to save tokens")

        from codilay.triage import Triage

        triage = Triage(llm_client=llm, config=cfg)

        if cfg.triage_mode == "smart":
            with ui.spinner("LLM is classifying files (tree only, no content)…"):
                triage_result = triage.smart_triage(
                    file_tree_text, all_files, md_contents
                )
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
                ui.info(
                    f"Skipped {len(test_files)} test files "
                    f"(set triage.includeTests: true to include)"
                )

        # Estimate savings
        triage_result.token_estimate_saved = triage.estimate_tokens_saved(
            triage_result.skip, target
        )

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
        ui.info(
            f"Detected {len(changed)} changed files, re-opened {wires_reopened} wires"
        )

    elif mode == "specific":
        specific = ui.prompt_specific_files(all_files)
        if not specific:
            ui.error("No valid files selected.")
            return
        files_to_process = specific
        wire_mgr.reopen_wires_for_files(specific)
        docstore.invalidate_sections_for_files(specific)

    # ── Phase 2: Planning ────────────────────────────────────────
    if mode != "resume":
        ui.phase("Phase 2 · Planning — Determining processing order")

        planner = Planner(llm, cfg)

        with ui.spinner("LLM is analysing the file structure…"):
            plan = planner.plan(file_tree_text, md_contents, files_to_process, state)

        state.queue = plan["order"]
        state.parked = plan.get("parked", []) if mode == "full" else state.parked
        state.park_reasons = (
            plan.get("park_reasons", {}) if mode == "full" else state.park_reasons
        )
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

    # ── Phase 3: Processing Loop ─────────────────────────────────
    ui.phase("Phase 3 · Processing — Reading files and building docs")

    processor = Processor(llm, cfg, wire_mgr, docstore, state, ui)

    total_files = len(state.queue)
    processed_count = 0

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
                progress.advance(task)
                continue

            progress.update(task, description=f"Processing: {file_path}")

            try:
                content = scanner.read_file(full_path)
                if content is None:
                    ui.warn(f"Could not read (binary?): {file_path}")
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

            except Exception as e:
                ui.error(f"Error processing {file_path}: {e}")
                if verbose:
                    console.print_exception()

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
    )

    # Show LLM usage
    stats = llm.get_usage_stats()
    ui.info(
        f"LLM usage: {stats['total_calls']} calls, "
        f"{stats['total_input_tokens']:,} input tokens, "
        f"{stats['total_output_tokens']:,} output tokens"
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
):
    """Finalize documentation, write all output files, save state."""
    from codilay.processor import Processor

    ui.phase("Phase 4 · Finalize — Assembling documentation")

    processor = Processor(llm, cfg, wire_mgr, docstore, state, ui)

    with ui.spinner("Running finalization pass…"):
        processor.finalize(scanner.get_file_tree())


    open_wires = wire_mgr.get_open_wires()
    closed_wires = wire_mgr.get_closed_wires()

    # Remove stale dependency-graph / unresolved-references if they exist
    docstore.remove_section("dependency-graph")
    docstore.remove_section("unresolved-references")

    docstore.add_dependency_graph(closed_wires)
    docstore.add_unresolved_references(open_wires)

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
        "open": open_wires,
    }
    with open(links_path, "w", encoding="utf-8") as f:
        json.dump(links_data, f, indent=2)

    # ── Save final state with git info ───────────────────────────
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

    # ── Summary ──────────────────────────────────────────────────
    ui.show_summary(
        processed_count=len(state.processed),
        wires_closed=len(closed_wires),
        wires_open=len(open_wires),
        sections=len(docstore.get_section_index()),
        output_path=codebase_md_path,
        links_path=links_path,
    )

    if current_commit_short:
        ui.info(
            f"Documented at commit [cyan]{current_commit_short}[/cyan] — "
            f"next run will diff from here"
        )


# ─── Status command ───────────────────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
def status(target):
    """Show current CodiLay state for a project."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")
    state_path = os.path.join(output_dir, ".codilay_state.json")

    if not os.path.exists(state_path):
        console.print("[yellow]No CodiLay state found for this project.[/yellow]")
        console.print(f"[dim]Looked in: {state_path}[/dim]")
        return

    state = AgentState.load(state_path)

    table = Table(title="CodiLay Status", box=box.ROUNDED)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Run ID", state.run_id)
    table.add_row("Files processed", str(len(state.processed)))
    table.add_row("Open wires", str(len(state.open_wires)))
    table.add_row("Closed wires", str(len(state.closed_wires)))
    table.add_row("Documentation sections", str(len(state.section_index)))
    table.add_row("Queued (remaining)", str(len(state.queue)))
    table.add_row("Parked", str(len(state.parked)))

    # Git info
    if state.last_commit_short:
        table.add_row("Last documented commit", state.last_commit_short)
    if state.last_run:
        table.add_row("Last run", state.last_run)

    console.print(table)

    # Show git diff summary if available
    git = GitTracker(target)
    if git.is_git_repo and state.last_commit:
        if git.is_commit_valid(state.last_commit):
            diff_result = git.get_full_diff(state.last_commit)
            if diff_result and diff_result.changes:
                console.print(
                    f"\n[bold yellow]⚠ {len(diff_result.changes)} changes "
                    f"since last documented commit "
                    f"({diff_result.commits_behind} commits behind):[/bold yellow]"
                )
                for line in diff_result.summary_lines[:20]:
                    console.print(line)
                if len(diff_result.changes) > 20:
                    console.print(
                        f"  [dim]… +{len(diff_result.changes) - 20} more[/dim]"
                    )
                console.print(
                    "\n[dim]Run [bold]codilay .[/bold] to update documentation.[/dim]"
                )
            elif diff_result:
                console.print(
                    "\n[green]✓ Documentation is up to date with HEAD.[/green]"
                )
        else:
            console.print(
                f"\n[yellow]⚠ Last documented commit "
                f"{state.last_commit_short} no longer exists "
                f"(rebase/force push?).[/yellow]"
            )

    if state.open_wires:
        console.print("\n[bold]Open wires:[/bold]")
        for w in state.open_wires[:15]:
            ctx = ""
            if "[DELETED]" in w.get("context", ""):
                ctx = " [red](file deleted)[/red]"
            console.print(
                f"  [yellow]→[/yellow] {w['from']} → {w['to']} "
                f"[dim]({w['type']})[/dim]{ctx}"
            )
        if len(state.open_wires) > 15:
            console.print(f"  [dim]  … +{len(state.open_wires) - 15} more[/dim]")


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
        console.print(
            "[dim]Run [bold]codilay .[/bold] first to create documentation.[/dim]"
        )
        return

    state = AgentState.load(state_path)
    git = GitTracker(target)

    if not git.is_git_repo:
        console.print("[yellow]Not a git repository. Cannot compute diff.[/yellow]")
        return

    if not state.last_commit:
        console.print(
            "[yellow]No commit recorded in state. Run a full documentation pass first.[/yellow]"
        )
        return

    if not git.is_commit_valid(state.last_commit):
        console.print(
            f"[red]Last documented commit {state.last_commit_short} "
            f"no longer exists.[/red]"
        )
        console.print("[dim]This can happen after a rebase or force push.[/dim]")
        console.print("[dim]Run [bold]codilay .[/bold] and choose 'Full re-run'.[/dim]")
        return

    diff_result = git.get_full_diff(state.last_commit)
    if not diff_result:
        console.print("[red]Could not compute diff.[/red]")
        return

    if not diff_result.changes:
        console.print(
            f"[green]✓ No changes since commit "
            f"{state.last_commit_short}. Documentation is current.[/green]"
        )
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
                    if w.get("from") == change.path
                    or w.get("to") == change.path
                    or w.get("resolved_in") == change.path
                ]
                impact_parts.append(f"re-process, {len(affected_wires)} wires affected")
            else:
                impact_parts.append("not yet documented")

        elif change.change_type == ChangeType.DELETED:
            status_str = "[red]deleted[/red]"
            if was_processed:
                affected_wires = [
                    w for w in state.closed_wires if w.get("resolved_in") == change.path
                ]
                impact_parts.append(
                    f"section marked deleted, {len(affected_wires)} wires re-opened"
                )
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
def clean(target, yes):
    """Remove all CodiLay generated files."""
    target = os.path.abspath(target)
    output_dir = os.path.join(target, "codilay")

    files_to_remove = []
    for fname in [
        ".codilay_state.json",
        "CODEBASE.md",
        "CODEBASE.md.bak",
        "links.json",
    ]:
        path = os.path.join(output_dir, fname)
        if os.path.exists(path):
            files_to_remove.append((fname, path))

    if not files_to_remove:
        console.print("[yellow]Nothing to clean.[/yellow]")
        return

    console.print("[bold]Files to remove:[/bold]")
    for fname, path in files_to_remove:
        console.print(f"  [red]✗[/red] {path}")

    if not yes:
        confirm = click.confirm("\nProceed?", default=False)
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            return

    for fname, path in files_to_remove:
        os.remove(path)

    console.print(f"[green]Removed {len(files_to_remove)} files.[/green]")


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
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_config, f, indent=2)

    console.print(f"[green]Created config:[/green] {config_path}")
    console.print("[dim]Edit it to customize CodiLay behaviour for this project.[/dim]")


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
@click.option("--port", "-P", default=8484, help="Port to serve on")
@click.option("--host", "-H", default="127.0.0.1", help="Host to bind to")
@click.option(
    "--output", "-o", default=None, help="Output directory containing codilay files"
)
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
            f"  URL:      [bold green]http://{host}:{port}[/bold green]\n\n"
            f"[dim]Press Ctrl+C to stop.[/dim]",
            border_style="blue",
            title="serve",
        )
    )

    run_server(target, output_dir, host=host, port=port)


# ── Chat command ──────────────────────────────────────────────────────────────


@cli.command()
@click.argument("target", default=".", type=click.Path(exists=True))
@click.option("--resume", "-r", is_flag=True, help="Resume last conversation")
@click.option("--list", "-l", "list_convs", is_flag=True, help="List past conversations")
@click.option("--conversation", "-c", default=None, help="Resume a specific conversation by ID")
@click.option(
    "--output", "-o", default=None, help="Output directory containing codilay files"
)
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
                        section_id = chat_store.promote_to_doc(
                            conv_id, last_msg_id, docstore, llm
                        )
                        if section_id:
                            # Re-render CODEBASE.md
                            final_md = docstore.render_full_document()
                            with open(codebase_md, "w", encoding="utf-8") as f:
                                f.write(final_md)
                            console.print(
                                f'[green]✓ Promoted to doc section "[cyan]{section_id}[/cyan]"[/green]'
                            )
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
                        console.print(
                            f"[green]✓ Branched! Now in conversation {conv_id[:8]}…[/green]"
                        )
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
                        console.print(
                            f"[green]✓ Resumed: {match.get('title', 'Untitled')}[/green]"
                        )
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
                "show me the code", "show the code", "exactly how", "line by line",
                "implementation detail", "source code", "what does the code",
                "read the file", "look at the file", "open the file",
                "specific implementation", "actual code"
            ]
            return any(p in q for p in deep_patterns)

        force_deep = force_deep or _should_escalate_by_keyword(user_input)

        # Retrieve relevant sections
        relevant = retriever.search(user_input, top_k=5)

        # Build pinned context
        pinned_msgs = chat_store.get_pinned_messages(conv_id)
        pinned_ctx = ""
        if pinned_msgs:
            pinned_ctx = "\n\n".join(
                f"- {m['content'][:200]}" for m in pinned_msgs[:5]
            )

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
                        console.print(
                            f"[dim]{reason} — escalating to deep agent...[/dim]"
                        )
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")
                    should_deep = True # Fallback to deep agent if Doc search fails
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
                    doc_ctx = (
                        "Documentation context:\n\n"
                        + "\n---\n".join(doc_parts)
                        + "\n\n---\n\n"
                    )

                system = (
                    "You are a deep codebase analysis agent. You have access to actual "
                    "source code files. Answer the user's question with precision, "
                    "referencing specific functions, classes, line ranges, and logic. "
                    "Be thorough but concise. Use markdown formatting.\n"
                    "IMPORTANT: Respond with PLAIN TEXT markdown only. Do NOT wrap your "
                    "entire response in a JSON object or any other format."
                )
                user_prompt = (
                    f"{doc_ctx}{history_text}\n\n"
                    f"Source code:\n\n{source_context}\n\n---\n\n"
                    f"Question: {user_input}"
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
                answer = (
                    "I couldn't find relevant source files. "
                    "Try mentioning specific file or module names."
                )

        # ── Display answer ────────────────────────────────────────
        console.print()
        console.print(
            Panel(
                answer,
                title="[bold blue]CodiLay[/bold blue]"
                + (" [yellow]🔍 deep[/yellow]" if escalated else ""),
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

    console.print(
        Panel("\n".join(lines), border_style="yellow", title="memory")
    )

