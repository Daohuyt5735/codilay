"""
CodiLay Interactive Menu — the "application" experience.

When the user runs `codilay` with no arguments and no target, they get a
beautiful interactive menu to set up, configure, and run documentation tasks.
"""

import os
import sys
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.text import Text
from rich import box

from codilay.settings import (
    Settings, PROVIDER_META, DEFAULT_MODELS, SETTINGS_DIR, SETTINGS_FILE,
)

console = Console()


# ── Pretty Helpers ────────────────────────────────────────────────────────────

LOGO = r"""[bold cyan]
   ██████╗ ██████╗ ██████╗ ██╗██╗      █████╗ ██╗   ██╗
  ██╔════╝██╔═══██╗██╔══██╗██║██║     ██╔══██╗╚██╗ ██╔╝
  ██║     ██║   ██║██║  ██║██║██║     ███████║ ╚████╔╝
  ██║     ██║   ██║██║  ██║██║██║     ██╔══██║  ╚██╔╝
  ╚██████╗╚██████╔╝██████╔╝██║███████╗██║  ██║   ██║
   ╚═════╝ ╚═════╝ ╚═════╝ ╚═╝╚══════╝╚═╝  ╚═╝   ╚═╝
[/bold cyan]"""

TAGLINE = "[dim]AI Agent for Codebase Documentation[/dim]"


def _clear():
    """Clear the terminal screen."""
    os.system("cls" if os.name == "nt" else "clear")


def _header(subtitle: str = ""):
    """Print the CodiLay header."""
    console.print(LOGO)
    console.print(f"  {TAGLINE}")
    if subtitle:
        console.print(f"  [bold yellow]{subtitle}[/bold yellow]")
    console.print()


def _pause():
    """Wait for the user to press Enter."""
    console.print()
    Prompt.ask("[dim]Press Enter to continue[/dim]", default="")


def _back_hint():
    """Print a hint about going back."""
    console.print("  [dim]Enter [bold]0[/bold] or [bold]b[/bold] at any prompt to go back[/dim]\n")


def _is_back(value: str) -> bool:
    """Check if the user wants to go back."""
    return value.strip().lower() in ("0", "b", "back", "q", "quit", "cancel")


def _int_prompt_with_back(label: str, low: int, high: int, default: int = 1) -> Optional[int]:
    """
    Ask for an integer in [low..high].  Return None if the user enters
    0 / b / back / q to cancel.
    """
    while True:
        raw = Prompt.ask(label, default=str(default))
        if _is_back(raw):
            return None
        try:
            val = int(raw)
            if low <= val <= high:
                return val
            console.print(f"  [yellow]Please enter a number between {low} and {high} (or 0 to go back)[/yellow]")
        except ValueError:
            console.print(f"  [yellow]Please enter a number between {low} and {high} (or 0 to go back)[/yellow]")


# ── Main Menu ─────────────────────────────────────────────────────────────────

def main_menu(settings: Settings) -> Optional[dict]:
    """
    Show the main interactive menu.

    Returns a dict describing what action to take, or None to exit.
    """
    while True:
        _clear()
        _header()

        # Status bar
        prov = settings.default_provider
        model = settings.get_effective_model()
        label = PROVIDER_META.get(prov, {}).get("label", prov)
        has_key = settings.has_provider_configured(prov)

        status_style = "green" if has_key else "red"
        status_icon = "✓" if has_key else "✗"
        status_text = "Ready" if has_key else "API key missing"

        console.print(Panel(
            f"  Provider: [bold]{label}[/bold]  │  "
            f"Model: [bold]{model or 'not set'}[/bold]  │  "
            f"Status: [{status_style}]{status_icon} {status_text}[/{status_style}]",
            border_style="cyan",
            title="[bold]Current Configuration[/bold]",
            title_align="left",
        ))
        console.print()

        # Menu items
        menu = Table(show_header=False, box=None, padding=(0, 2))
        menu.add_column("key", style="bold cyan", width=6, justify="right")
        menu.add_column("action")

        menu.add_row("[1]", "📝  Document a codebase")
        menu.add_row("[2]", "⚙️   Setup / First-time configuration")
        menu.add_row("[3]", "🔑  Manage API keys")
        menu.add_row("[4]", "🤖  Change provider & model")
        menu.add_row("[5]", "🔧  Preferences")
        menu.add_row("[6]", "📊  View current settings")
        menu.add_row("[7]", "💬  Chat with your codebase")
        menu.add_row("[8]", "❓  Help")
        menu.add_row("[0]", "🚪  Exit")

        console.print(menu)
        console.print()

        choice = Prompt.ask(
            "[bold cyan]Select an option[/bold cyan]",
            choices=["0", "1", "2", "3", "4", "5", "6", "7", "8"],
            default="1",
        )

        if choice == "0":
            console.print("\n[dim]Goodbye! 👋[/dim]\n")
            return None

        elif choice == "1":
            result = _menu_document(settings)
            if result:
                return result

        elif choice == "2":
            _menu_setup(settings)

        elif choice == "3":
            _menu_api_keys(settings)

        elif choice == "4":
            _menu_provider_model(settings)

        elif choice == "5":
            _menu_preferences(settings)

        elif choice == "6":
            _menu_view_settings(settings)

        elif choice == "7":
            result = _menu_chat(settings)
            if result:
                return result

        elif choice == "8":
            _menu_help()


# ── 1. Document a codebase ────────────────────────────────────────────────────

def _menu_document(settings: Settings) -> Optional[dict]:
    """Prompt the user for a target path and return a run action."""
    _clear()
    _header("Document a Codebase")
    _back_hint()

    prov = settings.default_provider
    if not settings.has_provider_configured(prov):
        console.print(
            f"[red]⚠  No API key configured for {PROVIDER_META.get(prov, {}).get('label', prov)}.[/red]"
        )
        console.print("[dim]Go to [bold]Setup[/bold] or [bold]Manage API Keys[/bold] first.[/dim]\n")
        _pause()
        return None

    target = Prompt.ask(
        "Path to the codebase [dim](0 to go back)[/dim]",
        default=".",
    )

    if _is_back(target):
        return None

    target = os.path.abspath(target)

    if not os.path.isdir(target):
        console.print(f"[red]Not a valid directory: {target}[/red]")
        _pause()
        return None

    console.print(f"\n[bold]Target:[/bold] {target}")
    console.print(f"[bold]Provider:[/bold] {settings.default_provider}")
    console.print(f"[bold]Model:[/bold] {settings.get_effective_model()}")
    console.print()

    if Confirm.ask("Start documentation?", default=True):
        return {
            "action": "run",
            "target": target,
            "provider": settings.default_provider,
            "model": settings.default_model,
            "base_url": settings.custom_base_url,
            "verbose": settings.verbose,
        }

    return None


# ── 2. Setup (first-time) ────────────────────────────────────────────────────

def _menu_setup(settings: Settings):
    """Guided first-time setup wizard."""
    _clear()
    _header("First-Time Setup")

    console.print(
        Panel(
            "[bold]Welcome to CodiLay! 🎉[/bold]\n\n"
            "This wizard will help you configure CodiLay.\n"
            "Your settings will be stored persistently at:\n"
            f"  [cyan]{SETTINGS_FILE}[/cyan]\n\n"
            "You won't need to export API keys every time anymore!\n\n"
            "[dim]Enter 0 at any prompt to cancel and go back.[/dim]",
            border_style="green",
        )
    )
    console.print()

    # Step 1: Choose provider
    console.print("[bold]Step 1 of 3:[/bold] Choose your default LLM provider\n")

    providers = list(PROVIDER_META.keys())
    provider_table = Table(show_header=True, box=box.SIMPLE)
    provider_table.add_column("#", style="bold cyan", width=4)
    provider_table.add_column("Provider", style="bold")
    provider_table.add_column("Default Model", style="dim")
    provider_table.add_column("Requires Key", style="dim")

    for i, prov in enumerate(providers, 1):
        meta = PROVIDER_META[prov]
        needs_key = "Yes" if meta.get("env_key") else "No (local)"
        default_m = DEFAULT_MODELS.get(prov, "—")
        provider_table.add_row(str(i), meta["label"], default_m or "—", needs_key)

    console.print(provider_table)
    console.print()

    idx = _int_prompt_with_back(
        "Select provider [dim](0 to cancel)[/dim]",
        low=1, high=len(providers), default=1,
    )
    if idx is None:
        return  # back to main menu

    chosen_provider = providers[idx - 1]
    settings.default_provider = chosen_provider
    console.print(f"\n[green]✓[/green] Provider set to [bold]{PROVIDER_META[chosen_provider]['label']}[/bold]\n")

    # Step 2: API Key
    meta = PROVIDER_META[chosen_provider]
    if meta.get("env_key"):
        console.print("[bold]Step 2 of 3:[/bold] Enter your API key\n")

        existing_key = settings.get_api_key(chosen_provider)
        if existing_key:
            console.print(f"  Current key: [dim]{Settings.mask_key(existing_key)}[/dim]")
            if not Confirm.ask("  Replace it?", default=False):
                console.print("[green]✓[/green] Keeping existing key\n")
            else:
                result = _prompt_api_key(settings, chosen_provider)
                if result == "back":
                    return  # back to main menu
        else:
            result = _prompt_api_key(settings, chosen_provider)
            if result == "back":
                return  # back to main menu
    else:
        console.print(f"[bold]Step 2 of 3:[/bold] [green]✓[/green] No API key needed for {meta['label']}\n")

    # Step 3: Model
    console.print("[bold]Step 3 of 3:[/bold] Choose your model\n")
    default_m = DEFAULT_MODELS.get(chosen_provider, "")
    console.print(f"  Default: [bold]{default_m}[/bold]")
    custom_model = Prompt.ask(
        "  Model [dim](Enter for default, 0 to cancel)[/dim]",
        default="",
    )

    if _is_back(custom_model):
        return  # back to main menu

    if custom_model:
        settings.default_model = custom_model
        console.print(f"[green]✓[/green] Model set to [bold]{custom_model}[/bold]\n")
    else:
        settings.default_model = None
        console.print(f"[green]✓[/green] Using default model: [bold]{default_m}[/bold]\n")

    settings.save()

    console.print(Panel(
        "[bold green]Setup complete! 🎉[/bold green]\n\n"
        "Your configuration has been saved. You can now:\n"
        "  • Run [bold]codilay .[/bold] to document a codebase\n"
        "  • Come back here anytime to change settings\n\n"
        "No more exporting API keys! 🔑",
        border_style="green",
    ))
    _pause()


def _prompt_api_key(settings: Settings, provider: str) -> Optional[str]:
    """
    Prompt for and store an API key.
    Returns 'back' if user cancels, None on success.
    """
    meta = PROVIDER_META[provider]
    key = Prompt.ask(
        f"  Enter your {meta['label']} API key [dim](0 to cancel)[/dim]",
        password=True,
    )
    if _is_back(key):
        return "back"
    if key.strip():
        settings.set_api_key(provider, key.strip())
        console.print(f"[green]✓[/green] API key saved securely\n")
    else:
        console.print("[yellow]⚠  No key entered[/yellow]\n")
    return None


# ── 3. Manage API Keys ───────────────────────────────────────────────────────

def _menu_api_keys(settings: Settings):
    """View, add, edit, or remove API keys."""
    while True:
        _clear()
        _header("Manage API Keys")

        console.print(
            "[dim]API keys are stored in ~/.codilay/settings.json. "
            "They persist across terminal sessions.[/dim]\n"
        )

        # Show current keys
        table = Table(title="Stored API Keys", box=box.ROUNDED)
        table.add_column("#", style="bold cyan", width=4)
        table.add_column("Provider", style="bold")
        table.add_column("Key", style="dim")
        table.add_column("Status")

        providers_with_keys = [p for p in PROVIDER_META if PROVIDER_META[p].get("env_key")]

        for i, prov in enumerate(providers_with_keys, 1):
            meta = PROVIDER_META[prov]
            key = settings.api_keys.get(prov, "")
            env_key = settings.get_api_key(prov) if not key else None

            if key:
                masked = Settings.mask_key(key)
                status = "[green]✓ Stored[/green]"
            elif env_key:
                masked = Settings.mask_key(env_key)
                status = "[yellow]~ From env[/yellow]"
            else:
                masked = "—"
                status = "[red]✗ Not set[/red]"

            marker = " ←" if prov == settings.default_provider else ""
            table.add_row(str(i), f"{meta['label']}{marker}", masked, status)

        console.print(table)
        console.print()

        console.print("  [bold cyan][a][/bold cyan] Add / update a key")
        console.print("  [bold cyan][r][/bold cyan] Remove a key")
        console.print("  [bold cyan][b][/bold cyan] Back to main menu")
        console.print()

        choice = Prompt.ask("Select", choices=["a", "r", "b"], default="b")

        if choice == "b":
            return

        elif choice == "a":
            console.print()
            idx = _int_prompt_with_back(
                f"Which provider (1-{len(providers_with_keys)}, 0 to cancel)",
                low=1, high=len(providers_with_keys),
            )
            if idx is None:
                continue  # back to key list
            prov = providers_with_keys[idx - 1]
            _prompt_api_key(settings, prov)

        elif choice == "r":
            console.print()
            idx = _int_prompt_with_back(
                f"Which provider (1-{len(providers_with_keys)}, 0 to cancel)",
                low=1, high=len(providers_with_keys),
            )
            if idx is None:
                continue  # back to key list
            prov = providers_with_keys[idx - 1]
            if Confirm.ask(
                f"  Remove key for [bold]{PROVIDER_META[prov]['label']}[/bold]?",
                default=False,
            ):
                settings.remove_api_key(prov)
                console.print(f"[green]✓[/green] Removed key for {PROVIDER_META[prov]['label']}")
            else:
                console.print("[dim]Cancelled.[/dim]")
            _pause()


# ── 4. Provider & Model ──────────────────────────────────────────────────────

def _menu_provider_model(settings: Settings):
    """Change the default provider and model."""
    _clear()
    _header("Change Provider & Model")
    _back_hint()

    providers = list(PROVIDER_META.keys())

    table = Table(show_header=True, box=box.SIMPLE)
    table.add_column("#", style="bold cyan", width=4)
    table.add_column("Provider", style="bold")
    table.add_column("Default Model", style="dim")
    table.add_column("Status")

    for i, prov in enumerate(providers, 1):
        meta = PROVIDER_META[prov]
        default_m = DEFAULT_MODELS.get(prov, "—")
        configured = settings.has_provider_configured(prov)
        status = "[green]✓ Ready[/green]" if configured else "[red]✗ Key needed[/red]"
        marker = "  [bold yellow]← current[/bold yellow]" if prov == settings.default_provider else ""
        table.add_row(str(i), f"{meta['label']}{marker}", default_m or "—", status)

    console.print(table)
    console.print()

    idx = _int_prompt_with_back(
        "Select provider [dim](0 to go back)[/dim]",
        low=1, high=len(providers),
        default=providers.index(settings.default_provider) + 1,
    )
    if idx is None:
        return  # back to main menu

    chosen = providers[idx - 1]
    settings.default_provider = chosen

    # Model
    default_m = DEFAULT_MODELS.get(chosen, "")
    current_m = settings.default_model if settings.default_model else default_m
    console.print(f"\n  Current model: [bold]{current_m}[/bold]")
    new_model = Prompt.ask(
        "  New model [dim](Enter to keep, 0 to cancel)[/dim]", default=""
    )

    if _is_back(new_model):
        # Revert provider change since user cancelled
        return

    if new_model:
        settings.default_model = new_model
    else:
        settings.default_model = None   # use provider default

    # Custom base URL for 'custom' provider
    if chosen == "custom":
        console.print()
        current_url = settings.custom_base_url or "not set"
        console.print(f"  Current base URL: [bold]{current_url}[/bold]")
        new_url = Prompt.ask(
            "  Base URL [dim](Enter to keep, 0 to cancel)[/dim]",
            default=settings.custom_base_url or "",
        )
        if _is_back(new_url):
            return
        if new_url:
            settings.custom_base_url = new_url

    settings.save()

    final_model = settings.get_effective_model()
    console.print(
        f"\n[green]✓[/green] Provider: [bold]{PROVIDER_META[chosen]['label']}[/bold]"
        f"  Model: [bold]{final_model}[/bold]"
    )
    _pause()


# ── 5. Preferences ───────────────────────────────────────────────────────────

def _menu_preferences(settings: Settings):
    """Tweak global CodiLay preferences."""
    while True:
        _clear()
        _header("Preferences")

        console.print("[bold]Current settings:[/bold]\n")
        console.print(f"  [bold cyan][1][/bold cyan] Verbose output:      [bold]{'Yes' if settings.verbose else 'No'}[/bold]")
        console.print(f"  [bold cyan][2][/bold cyan] Triage mode:         [bold]{settings.triage_mode}[/bold]  [dim](smart | fast | none)[/dim]")
        console.print(f"  [bold cyan][3][/bold cyan] Include test files:  [bold]{'Yes' if settings.include_tests else 'No'}[/bold]")
        console.print(f"  [bold cyan][4][/bold cyan] Max tokens per call: [bold]{settings.max_tokens_per_call}[/bold]")
        console.print()
        console.print("  [bold cyan][0][/bold cyan] ← Back to main menu")
        console.print()

        choice = Prompt.ask(
            "Which setting to change?",
            choices=["0", "1", "2", "3", "4"],
            default="0",
        )

        if choice == "0":
            return

        elif choice == "1":
            settings.verbose = not settings.verbose
            settings.save()
            state = "ON" if settings.verbose else "OFF"
            console.print(f"\n[green]✓[/green] Verbose output: [bold]{state}[/bold]")
            _pause()

        elif choice == "2":
            modes = ["smart", "fast", "none"]
            current_idx = modes.index(settings.triage_mode) if settings.triage_mode in modes else 0
            next_idx = (current_idx + 1) % len(modes)
            settings.triage_mode = modes[next_idx]
            settings.save()
            console.print(f"\n[green]✓[/green] Triage mode: [bold]{settings.triage_mode}[/bold]")
            _pause()

        elif choice == "3":
            settings.include_tests = not settings.include_tests
            settings.save()
            state = "ON" if settings.include_tests else "OFF"
            console.print(f"\n[green]✓[/green] Include test files: [bold]{state}[/bold]")
            _pause()

        elif choice == "4":
            raw = Prompt.ask(
                f"  Max tokens per LLM call [dim](currently {settings.max_tokens_per_call}, 0 to cancel)[/dim]",
                default=str(settings.max_tokens_per_call),
            )
            if _is_back(raw):
                continue
            try:
                tokens = int(raw)
                settings.max_tokens_per_call = max(1024, min(tokens, 32768))
                settings.save()
                console.print(f"\n[green]✓[/green] Max tokens: [bold]{settings.max_tokens_per_call}[/bold]")
            except ValueError:
                console.print("[yellow]Invalid number[/yellow]")
            _pause()


# ── 6. View Settings ─────────────────────────────────────────────────────────

def _menu_view_settings(settings: Settings):
    """Show a full summary of current configuration."""
    _clear()
    _header("Current Settings")

    # General
    table = Table(title="Configuration", box=box.ROUNDED, title_style="bold cyan")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="bold")

    prov = settings.default_provider
    table.add_row("Default provider", PROVIDER_META.get(prov, {}).get("label", prov))
    table.add_row("Default model", settings.get_effective_model() or "—")
    table.add_row("Verbose", "Yes" if settings.verbose else "No")
    table.add_row("Triage mode", settings.triage_mode)
    table.add_row("Include tests", "Yes" if settings.include_tests else "No")
    table.add_row("Max tokens/call", str(settings.max_tokens_per_call))
    if settings.custom_base_url:
        table.add_row("Custom base URL", settings.custom_base_url)
    table.add_row("Settings file", str(SETTINGS_FILE))

    console.print(table)

    # Keys
    console.print()
    keys_table = Table(title="API Keys", box=box.ROUNDED, title_style="bold cyan")
    keys_table.add_column("Provider", style="bold")
    keys_table.add_column("Key", style="dim")
    keys_table.add_column("Source")

    for prov_name, meta in PROVIDER_META.items():
        env_key_name = meta.get("env_key")
        if not env_key_name:
            keys_table.add_row(meta["label"], "—", "[dim]Not required[/dim]")
            continue

        stored = settings.api_keys.get(prov_name)
        from_env = os.environ.get(env_key_name) if not stored else None

        if stored:
            keys_table.add_row(
                meta["label"],
                Settings.mask_key(stored),
                "[green]~/.codilay/settings.json[/green]",
            )
        elif from_env:
            keys_table.add_row(
                meta["label"],
                Settings.mask_key(from_env),
                f"[yellow]${env_key_name}[/yellow]",
            )
        else:
            keys_table.add_row(
                meta["label"],
                "—",
                "[red]Not configured[/red]",
            )

    console.print(keys_table)
    _pause()


# ── 7. Chat with codebase ────────────────────────────────────────────────────

def _menu_chat(settings: Settings) -> Optional[dict]:
    """Prompt for a codebase path and launch chat."""
    _clear()
    _header("Chat with Your Codebase")
    _back_hint()

    prov = settings.default_provider
    if not settings.has_provider_configured(prov):
        console.print(
            f"[red]⚠  No API key configured for {PROVIDER_META.get(prov, {}).get('label', prov)}.[/red]"
        )
        console.print("[dim]Go to [bold]Setup[/bold] or [bold]Manage API Keys[/bold] first.[/dim]\n")
        _pause()
        return None

    raw = Prompt.ask(
        "Path to documented codebase", default="."
    )
    if _is_back(raw):
        return None

    target = os.path.abspath(raw)
    if not os.path.isdir(target):
        console.print(f"[red]Not a directory: {target}[/red]")
        _pause()
        return None

    output_dir = os.path.join(target, "codilay")
    codebase_md = os.path.join(output_dir, "CODEBASE.md")
    if not os.path.exists(codebase_md):
        console.print(
            f"[red]No documentation found at {output_dir}[/red]\n"
            f"[dim]Run [bold]codilay {target}[/bold] first to generate docs.[/dim]"
        )
        _pause()
        return None

    return {"action": "chat", "target": target}


# ── 8. Help ───────────────────────────────────────────────────────────────────

def _menu_help():
    """Show usage help."""
    _clear()
    _header("Help")

    console.print(Panel(
        "[bold]CodiLay[/bold] is an AI agent that reads your codebase\n"
        "and generates comprehensive documentation.\n\n"
        "[bold cyan]Quick Start[/bold cyan]\n"
        "  1. Run [bold]codilay[/bold] → interactive menu\n"
        "  2. Go to [bold]Setup[/bold] to configure your API key\n"
        "  3. Go to [bold]Document a codebase[/bold] and point to your project\n\n"
        "[bold cyan]CLI Usage[/bold cyan]\n"
        "  [bold]codilay .[/bold]                        Document current directory\n"
        "  [bold]codilay /path/to/project[/bold]         Document a project\n"
        "  [bold]codilay . -p openai -m gpt-4o[/bold]    Override provider/model\n"
        "  [bold]codilay chat .[/bold]                   Chat with your codebase\n"
        "  [bold]codilay chat . --resume[/bold]          Resume last conversation\n"
        "  [bold]codilay chat . --list[/bold]            List past conversations\n"
        "  [bold]codilay setup[/bold]                    Run setup wizard\n"
        "  [bold]codilay config[/bold]                   View settings\n"
        "  [bold]codilay keys[/bold]                     Manage API keys\n"
        "  [bold]codilay status .[/bold]                 Show doc status\n"
        "  [bold]codilay diff .[/bold]                   Show changes since last run\n"
        "  [bold]codilay clean .[/bold]                  Remove generated files\n\n"
        "[bold cyan]Navigation[/bold cyan]\n"
        "  Enter [bold]0[/bold] or [bold]b[/bold] at any prompt to go back\n"
        "  Press [bold]Ctrl+C[/bold] to quit immediately\n\n"
        "[bold cyan]Settings[/bold cyan]\n"
        f"  Config file: [bold]{SETTINGS_FILE}[/bold]\n"
        "  API keys are stored persistently — no more exporting!\n\n"
        "[bold cyan]Links[/bold cyan]\n"
        "  GitHub:  https://github.com/HarmanPreet-Singh-XYT/codilay\n"
        "  Issues:  https://github.com/HarmanPreet-Singh-XYT/codilay/issues",
        border_style="cyan",
        title="[bold]CodiLay Help[/bold]",
        title_align="left",
    ))
    _pause()

