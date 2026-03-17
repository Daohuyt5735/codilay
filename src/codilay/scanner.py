"""File scanner — walks the codebase, respects .gitignore and config ignores."""

import hashlib
import os
import subprocess
from typing import Dict, List, Optional

import pathspec


class Scanner:
    """Scans a codebase directory, respecting ignore rules."""

    TEXT_EXTENSIONS = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".mjs",
        ".cjs",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cxx",
        ".cs",
        ".fs",
        ".vb",
        ".swift",
        ".m",
        ".mm",
        ".lua",
        ".r",
        ".R",
        ".jl",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".bat",
        ".cmd",
        ".sql",
        ".graphql",
        ".gql",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".xml",
        ".xsl",
        ".xslt",
        ".svg",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".env",
        ".env.example",
        ".env.local",
        ".md",
        ".markdown",
        ".rst",
        ".txt",
        ".adoc",
        ".dockerfile",
        ".containerfile",
        ".tf",
        ".hcl",
        ".proto",
        ".thrift",
        ".avsc",
        ".vue",
        ".svelte",
        ".astro",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".hs",
        ".lhs",
        ".elm",
        ".purs",
        ".clj",
        ".cljs",
        ".cljc",
        ".edn",
        ".ml",
        ".mli",
        ".re",
        ".rei",
        ".dart",
        ".nim",
        ".zig",
        ".v",
        ".rake",
        ".gemspec",
        ".gradle",
        ".sbt",
        ".cmake",
        ".make",
        ".mk",
    }

    TEXT_FILENAMES = {
        "Makefile",
        "Dockerfile",
        "Containerfile",
        "Vagrantfile",
        "Rakefile",
        "Gemfile",
        "Procfile",
        "Brewfile",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".eslintrc",
        ".prettierrc",
        ".babelrc",
        ".dockerignore",
        ".npmignore",
        ".slugignore",
        "LICENSE",
        "LICENCE",
        "README",
        "CHANGELOG",
        "CONTRIBUTING",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "package.json",
        "tsconfig.json",
        "webpack.config.js",
        "docker-compose.yml",
        "docker-compose.yaml",
    }

    def __init__(self, target_path: str, config, output_dir: Optional[str] = None):
        self.target_path = os.path.abspath(target_path)
        self.config = config
        self.output_dir = os.path.abspath(output_dir) if output_dir else None
        self._build_ignore_spec()

    def _build_ignore_spec(self):
        patterns = [
            "/.git/",
            "/.git/**",
            "/node_modules/",
            "/node_modules/**",
            "/__pycache__/",
            "/__pycache__/**",
            "/.codilay_state.json",
            "/.venv/",
            "/.venv/**",
            "/venv/",
            "/venv/**",
            "/.env/",
            "/.env/**",
            "/env/",
            "/env/**",
            "/build/",
            "/build/**",
            "/dist/",
            "/dist/**",
            "/.dart_tool/",
            "/.dart_tool/**",
            "/.gradle/",
            "/.gradle/**",
            "/Pods/",
            "/Pods/**",
            "/.idea/",
            "/.idea/**",
            "/.vscode/",
            "/.vscode/**",
            "/.vs/",
            "/.vs/**",
            "/target/",
            "/target/**",
            "/out/",
            "/out/**",
            "/.cache/",
            "/.cache/**",
            "/.next/",
            "/.next/**",
            "/coverage/",
            "/coverage/**",
        ]

        gitignore_path = os.path.join(self.target_path, ".gitignore")
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)

        patterns.extend(self.config.ignore_patterns)
        patterns.extend(self.config.skip_generated)

        if self.output_dir:
            try:
                rel_output = os.path.relpath(self.output_dir, self.target_path)
                if not rel_output.startswith("..") and rel_output != ".":
                    rel_output = rel_output.replace(os.sep, "/")
                    # Prefix with / to make it root-relative
                    patterns.append(f"/{rel_output}/")
                    patterns.append(f"/{rel_output}/**")
            except ValueError:
                pass

        self.ignore_spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def _is_ignored(self, rel_path: str) -> bool:
        return self.ignore_spec.match_file(rel_path)

    def _is_text_file(self, filepath: str) -> bool:
        basename = os.path.basename(filepath)
        _, ext = os.path.splitext(basename)

        if basename in self.TEXT_FILENAMES:
            return True
        if ext.lower() in self.TEXT_EXTENSIONS:
            return True

        try:
            with open(filepath, "rb") as f:
                chunk = f.read(1024)
                if b"\x00" in chunk:
                    return False
                return True
        except (IOError, OSError):
            return False

    def get_all_files(self) -> List[str]:
        if hasattr(self, "_cached_files"):
            return self._cached_files

        files = []

        # ── Strategy 1: Git-aware discovery (fastest, most accurate) ───────────
        try:
            # git ls-files --cached --others --exclude-standard handles:
            # - Tracked files
            # - Untracked NOT-ignored files
            # - Nested .gitignore files correctly
            result = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "."],
                cwd=self.target_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                git_lines = result.stdout.splitlines()
                for rel_path in git_lines:
                    rel_path = rel_path.replace(os.sep, "/")
                    if self._is_ignored(rel_path):
                        continue

                    full_path = os.path.join(self.target_path, rel_path)
                    if not os.path.isfile(full_path):
                        continue

                    try:
                        size = os.path.getsize(full_path)
                        if 0 < size <= self.config.max_file_size:
                            if self._is_text_file(full_path):
                                files.append(rel_path)
                    except OSError:
                        pass

                self._cached_files = sorted(files)
                return self._cached_files
        except (FileNotFoundError, subprocess.SubprocessError, subprocess.TimeoutExpired):
            pass

        # ── Strategy 2: Manual walk fallback ─────────────────────────────────────
        for root, dirs, filenames in os.walk(self.target_path):
            rel_root = os.path.relpath(root, self.target_path)
            if rel_root == ".":
                rel_root = ""

            dirs[:] = [
                d for d in dirs if not self._is_ignored(os.path.join(rel_root, d) + "/" if rel_root else d + "/")
            ]

            for fname in sorted(filenames):
                rel_path = os.path.join(rel_root, fname) if rel_root else fname
                rel_path = rel_path.replace(os.sep, "/")

                if self._is_ignored(rel_path):
                    continue

                full_path = os.path.join(root, fname)

                try:
                    size = os.path.getsize(full_path)
                    if 0 < size <= self.config.max_file_size:
                        if self._is_text_file(full_path):
                            files.append(rel_path)
                except OSError:
                    continue

        self._cached_files = sorted(files)
        return self._cached_files

    def get_file_tree(self) -> str:
        """
        Builds a visual file tree.
        Always uses the internal _build_tree logic to ensure parity
        with identical ignore rules as get_all_files.
        """
        return self._build_tree()

    def _build_tree(self) -> str:
        lines = [os.path.basename(self.target_path) + "/"]
        files = self.get_all_files()

        tree: dict = {}
        for f in files:
            parts = f.split("/")
            current = tree
            for part in parts[:-1]:
                if part not in current:
                    current[part] = {}
                current = current[part]
            current[parts[-1]] = None

        self._render_tree(tree, lines, prefix="")
        return "\n".join(lines)

    def _render_tree(self, tree: dict, lines: list, prefix: str):
        entries = sorted(tree.keys(), key=lambda x: (tree[x] is not None, x))
        for i, name in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "`-- " if is_last else "|-- "
            suffix = "/" if tree[name] is not None else ""
            lines.append(f"{prefix}{connector}{name}{suffix}")
            if tree[name] is not None:
                extension = "    " if is_last else "|   "
                self._render_tree(tree[name], lines, prefix + extension)

    def preload_md_files(self) -> Dict[str, str]:
        md_files = {}
        for rel_path in self.get_all_files():
            if rel_path.lower().endswith((".md", ".markdown", ".rst")):
                full_path = os.path.join(self.target_path, rel_path)
                content = self.read_file(full_path)
                if content:
                    md_files[rel_path] = content[:5000]
        return md_files

    def read_file(self, filepath: str) -> Optional[str]:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "\x00" in content[:1024]:
                return None
            return content
        except (IOError, OSError, UnicodeDecodeError):
            return None

    def get_changed_files(self, previously_processed: List[str]) -> List[str]:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=self.target_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                git_changed = set(result.stdout.strip().split("\n")) if result.stdout.strip() else set()
                result2 = subprocess.run(
                    ["git", "ls-files", "--others", "--exclude-standard"],
                    cwd=self.target_path,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result2.returncode == 0 and result2.stdout.strip():
                    git_changed.update(result2.stdout.strip().split("\n"))

                all_files = set(self.get_all_files())
                return [f for f in all_files if f in git_changed or f not in previously_processed]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        all_files = self.get_all_files()
        processed_set = set(previously_processed)
        return [f for f in all_files if f not in processed_set]

    def get_file_hash(self, filepath: str) -> Optional[str]:
        """Get a hash of a file's contents for change detection fallback."""
        try:
            with open(filepath, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except (IOError, OSError):
            return None
