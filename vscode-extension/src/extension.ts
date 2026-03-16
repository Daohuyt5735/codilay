/**
 * CodiLay VSCode Extension
 *
 * Surfaces codebase documentation inline alongside the file being edited.
 * Connects to a running CodiLay server (codilay serve .) to fetch
 * documentation sections, dependency graph data, and chat answers.
 */

import * as vscode from "vscode";

// ── Types ────────────────────────────────────────────────────────────────────

interface Section {
  id: string;
  title: string;
  file: string;
  tags: string[];
  content: string;
}

interface ChatResponse {
  answer: string;
  sources: string[];
  confidence: number;
  escalated: boolean;
  conversation_id: string;
  message_id: string;
}

interface SearchResult {
  conversation_id: string;
  conversation_title: string;
  message_id: string;
  role: string;
  snippet: string;
  score: number;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function getServerUrl(): string {
  const config = vscode.workspace.getConfiguration("codilay");
  return config.get<string>("serverUrl", "http://127.0.0.1:8484");
}

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${getServerUrl()}${path}`;
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`CodiLay API error (${resp.status}): ${text}`);
  }
  return resp.json() as Promise<T>;
}

// ── Section Tree Provider ────────────────────────────────────────────────────

class SectionTreeItem extends vscode.TreeItem {
  constructor(
    public readonly section: Section,
    public readonly collapsibleState: vscode.TreeItemCollapsibleState
  ) {
    super(section.title, collapsibleState);
    this.tooltip = `${section.title}\n${section.file || "No file"}`;
    this.description = section.file || "";
    this.contextValue = "section";
    this.command = {
      command: "codilay.openSection",
      title: "Open Section",
      arguments: [section],
    };
  }
}

class SectionTreeProvider
  implements vscode.TreeDataProvider<SectionTreeItem>
{
  private _onDidChangeTreeData = new vscode.EventEmitter<
    SectionTreeItem | undefined | null
  >();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private sections: Section[] = [];

  refresh(): void {
    this._onDidChangeTreeData.fire(undefined);
  }

  async load(): Promise<void> {
    try {
      const data = await apiFetch<{ sections: Section[] }>("/api/sections");
      this.sections = data.sections;
      this.refresh();
    } catch {
      this.sections = [];
      this.refresh();
    }
  }

  getTreeItem(element: SectionTreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(): SectionTreeItem[] {
    return this.sections.map(
      (s) =>
        new SectionTreeItem(s, vscode.TreeItemCollapsibleState.None)
    );
  }

  getSectionForFile(filePath: string): Section | undefined {
    const rel = vscode.workspace.asRelativePath(filePath);
    return this.sections.find((s) => s.file === rel);
  }
}

// ── Webview Panel for Documentation ──────────────────────────────────────────

let docPanel: vscode.WebviewPanel | undefined;

function showDocPanel(
  context: vscode.ExtensionContext,
  content: string,
  title: string
): void {
  if (docPanel) {
    docPanel.title = title;
    docPanel.webview.html = getDocHtml(content, title);
    docPanel.reveal(vscode.ViewColumn.Beside);
  } else {
    docPanel = vscode.window.createWebviewPanel(
      "codilayDoc",
      title,
      vscode.ViewColumn.Beside,
      { enableScripts: false, retainContextWhenHidden: true }
    );
    docPanel.webview.html = getDocHtml(content, title);
    docPanel.onDidDispose(() => {
      docPanel = undefined;
    });
  }
}

function getDocHtml(content: string, title: string): string {
  // Basic markdown-to-html: escape HTML, convert headers/bold/code
  const escaped = content
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  const html = escaped
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h1>$1</h1>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");

  return `<!DOCTYPE html>
<html>
<head>
  <style>
    body {
      font-family: var(--vscode-font-family);
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
      padding: 16px;
      line-height: 1.6;
    }
    h1, h2, h3 { color: var(--vscode-textLink-foreground); }
    code {
      background: var(--vscode-textCodeBlock-background);
      padding: 2px 4px;
      border-radius: 3px;
      font-family: var(--vscode-editor-font-family);
    }
    .meta { color: var(--vscode-descriptionForeground); font-size: 0.9em; }
  </style>
</head>
<body>
  <h1>${title}</h1>
  <div>${html}</div>
</body>
</html>`;
}

// ── Inline Documentation Decorations ─────────────────────────────────────────

const docDecorationType = vscode.window.createTextEditorDecorationType({
  after: {
    color: "#888888",
    fontStyle: "italic",
    margin: "0 0 0 1em",
  },
});

async function updateInlineHints(
  editor: vscode.TextEditor,
  sectionProvider: SectionTreeProvider
): Promise<void> {
  const config = vscode.workspace.getConfiguration("codilay");
  if (!config.get<boolean>("inlineHints", true)) {
    editor.setDecorations(docDecorationType, []);
    return;
  }

  const section = sectionProvider.getSectionForFile(editor.document.fileName);
  if (!section || !section.content) {
    editor.setDecorations(docDecorationType, []);
    return;
  }

  // Show a hint on line 0 with the section title
  const decoration: vscode.DecorationOptions = {
    range: new vscode.Range(0, 0, 0, 0),
    renderOptions: {
      after: {
        contentText: ` CodiLay: ${section.title}`,
      },
    },
  };

  editor.setDecorations(docDecorationType, [decoration]);
}

// ── Activation ───────────────────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext): void {
  const sectionProvider = new SectionTreeProvider();

  // Register tree view
  vscode.window.registerTreeDataProvider("codilay.sections", sectionProvider);

  // Initial load
  sectionProvider.load();

  // ── Commands ─────────────────────────────────────────────────

  context.subscriptions.push(
    vscode.commands.registerCommand("codilay.showDocPanel", async () => {
      try {
        const data = await apiFetch<{ markdown: string }>("/api/document");
        showDocPanel(context, data.markdown, "CodiLay Documentation");
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        vscode.window.showErrorMessage(`CodiLay: ${msg}`);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codilay.showFileDoc", async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showInformationMessage("No active file.");
        return;
      }
      const section = sectionProvider.getSectionForFile(
        editor.document.fileName
      );
      if (section) {
        showDocPanel(context, section.content, section.title);
      } else {
        vscode.window.showInformationMessage(
          "No documentation found for this file."
        );
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codilay.openSection", (section: Section) => {
      showDocPanel(context, section.content, section.title);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codilay.askQuestion", async () => {
      const question = await vscode.window.showInputBox({
        prompt: "Ask CodiLay about your codebase",
        placeHolder: "How does the authentication flow work?",
      });
      if (!question) {
        return;
      }

      await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "CodiLay is thinking...",
          cancellable: false,
        },
        async () => {
          try {
            const resp = await apiFetch<ChatResponse>("/api/chat", {
              method: "POST",
              body: JSON.stringify({ question }),
            });
            showDocPanel(
              context,
              resp.answer +
                (resp.escalated ? "\n\n---\n*Deep agent was used*" : "") +
                (resp.sources.length
                  ? `\n\n---\n*Sources: ${resp.sources.join(", ")}*`
                  : ""),
              "Chat Answer"
            );
          } catch (e: unknown) {
            const msg = e instanceof Error ? e.message : String(e);
            vscode.window.showErrorMessage(`CodiLay: ${msg}`);
          }
        }
      );
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codilay.searchConversations", async () => {
      const query = await vscode.window.showInputBox({
        prompt: "Search past conversations",
        placeHolder: "database migration",
      });
      if (!query) {
        return;
      }

      try {
        const data = await apiFetch<{
          results: SearchResult[];
        }>(`/api/search?q=${encodeURIComponent(query)}&top_k=10`);

        if (!data.results.length) {
          vscode.window.showInformationMessage(`No results for "${query}".`);
          return;
        }

        const items = data.results.map((r) => ({
          label: `${r.role === "user" ? "You" : "CodiLay"}: ${r.snippet.slice(0, 80)}`,
          description: r.conversation_title,
          detail: `Score: ${r.score.toFixed(2)}`,
        }));

        const selected = await vscode.window.showQuickPick(items, {
          placeHolder: `${data.results.length} results`,
        });

        if (selected) {
          const idx = items.indexOf(selected);
          const result = data.results[idx];
          showDocPanel(context, result.snippet, "Search Result");
        }
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        vscode.window.showErrorMessage(`CodiLay: ${msg}`);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codilay.showGraph", async () => {
      try {
        const data = await apiFetch<{ nodes: unknown[]; edges: unknown[] }>(
          "/api/graph/filter",
          {
            method: "POST",
            body: JSON.stringify({}),
          }
        );
        showDocPanel(
          context,
          `# Dependency Graph\n\n` +
            `**${(data.nodes || []).length} nodes, ${(data.edges || []).length} edges**\n\n` +
            `Use the web UI (\`codilay serve .\`) for an interactive graph view.`,
          "Dependency Graph"
        );
      } catch (e: unknown) {
        const msg = e instanceof Error ? e.message : String(e);
        vscode.window.showErrorMessage(`CodiLay: ${msg}`);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("codilay.refresh", () => {
      sectionProvider.load();
      vscode.window.showInformationMessage("CodiLay: Documentation refreshed.");
    })
  );

  // ── Inline hints on active editor change ─────────────────────

  if (vscode.window.activeTextEditor) {
    updateInlineHints(vscode.window.activeTextEditor, sectionProvider);
  }

  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      if (editor) {
        updateInlineHints(editor, sectionProvider);
      }
    })
  );
}

export function deactivate(): void {
  // Cleanup
}
