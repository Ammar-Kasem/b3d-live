# b3d-lsp

A live-reloading browser viewer for [build123d](https://github.com/gumyr/build123d) CAD scripts, driven by your editor on every keystroke.

Point your editor at `b3d-lsp` as a Python language server — the browser opens automatically and updates as you type.

---

## Quick start

```bash
uv sync
```

```bash
uv run b3d-lsp body.py cab.py
```

The browser opens at `http://localhost:1234`.

### Options

```
b3d-lsp [files ...] [--port PORT]
```

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `files`  | (none)  | Files to load on startup; more can be opened from the editor at any time |
| `--port` | `1234`  | Browser port |

---

## Editor setup

`b3d-lsp` is a standard LSP server that runs over stdio. Configure it as an
additional language server for Python files in your editor.

### Helix

```toml
# ~/.config/helix/languages.toml
[language-server.b3d-lsp]
command = "/path/to/.venv/bin/b3d-lsp"
args = ["body.py", "cab.py"]

[[language]]
name = "python"
language-servers = ["ruff", "b3d-lsp"]
```

Replace `/path/to/.venv` with the absolute path to the project's virtual environment.

### Neovim (nvim-lspconfig)

```lua
local lspconfig = require("lspconfig")
local configs   = require("lspconfig.configs")

configs.b3d_lsp = {
  default_config = {
    cmd     = { "/path/to/.venv/bin/b3d-lsp", "body.py", "cab.py" },
    filetypes = { "python" },
    root_dir  = lspconfig.util.root_pattern("pyproject.toml", ".git"),
  },
}
lspconfig.b3d_lsp.setup {}
```

---

## How it works

Most live viewers re-execute the entire script on every save. For complex
models this means waiting for all geometry to rebuild even when only one part
changed.

b3d-lsp uses **Tree-sitter incremental parsing** to sync the parse tree
directly with the VTK scene graph:

1. On each keystroke, the editor sends a `textDocument/didChange` notification.
   Tree-sitter re-parses only the bytes that changed.
2. Every top-level `with BuildPart() as x:`, `with BuildSketch() as x:`, and
   `with BuildLine() as x:` block is an independent unit mapped 1:1 to a VTK actor.
3. The raw byte edit region is compared against each block's byte span — only
   overlapping blocks are re-executed.
4. Pre-part variables defined above a block (e.g. `body_color = Color(0x4683CE)`)
   are tracked by name. When one changes, only blocks that actually reference it
   re-execute.
5. Top-level metadata assignments (`body.part.color = Color(...)`) update the
   cached actor property directly without re-executing the block.
6. Cross-file dependencies are resolved with **jedi** — when a shared module
   changes, only the watched files that import it reload.

```
Tree-sitter node (BuildPart body) ←── 1:1 ──► vtkActor
       byte range unchanged                    actor reused, nothing runs
       byte range changed                      re-execute block, replace actor
```

Rendering runs entirely client-side via **VTK.wasm** (through
[trame-vtklocal](https://github.com/Kitware/trame-vtklocal)). The Python server
tessellates geometry and pushes a VTK render window state to the browser — no
server-side OpenGL required.

---

## Resilience

The viewer stays usable while you are mid-edit:

- **Syntax errors** — Tree-sitter produces a partial tree even for broken
  syntax. Only the block containing the error loses its actor; all other blocks
  continue reloading normally.
- **Runtime errors** — if a single block fails to execute, its last good actor
  is preserved. Other blocks are unaffected.

---

## Toolbar

| Control          | Action                                     |
| ---------------- | ------------------------------------------ |
| Wireframe        | Toggle between solid and wireframe display |
| Show edges       | Toggle face-edge overlay                   |
| Light/dark       | Toggle background colour                   |
| Parallel         | Toggle parallel/perspective projection     |
| Reset camera     | Fit all shapes in view                     |
| X / Y / Z        | Snap camera to that axis                   |
| -X / -Y / -Z     | Snap camera to negative axis               |
| Isometric        | Snap camera to isometric view              |
| Shape counter    | Live count of shapes and cache hits        |

---

## Multiple files and shared constants

Any number of `.py` files can be passed on the command line or opened from the
editor. Their shapes are all rendered in the same scene:

```bash
b3d-lsp body.py cab.py wheels.py
```

Pre-part variables can be shared via normal imports:

```python
# common.py
body_color = Color(0x4683CE)

# body.py
from common import body_color

with BuildPart() as body:
    ...
    body.part.color = body_color
```

When `common.py` changes, b3d-lsp's dependency graph identifies which watched
files import it and reloads only those — and within each file, only the blocks
that reference the changed variable.

---

## How it compares

|                      | b3d-lsp                   | ocp-vscode      | cq-editor       | YACV                |
| -------------------- | ------------------------- | --------------- | --------------- | ------------------- |
| Editor coupling      | none — any editor         | VS Code only    | built-in editor | none                |
| Reload unit          | changed block only        | whole file      | whole file      | whole file          |
| Change detection     | Tree-sitter byte range    | file hash       | file hash       | file hash           |
| Rendering            | VTK.wasm in browser       | embedded window | PyQT window     | OCP.wasm in browser |
| Multi-file watch     | yes                       | no              | no              | no                  |
| Cross-file deps      | yes (jedi)                | no              | no              | no                  |
| Error resilience     | per-block, last good kept | blanks on error | blanks on error | blanks on error     |
| Keystroke reload     | yes                       | no              | no              | no                  |
| Static deployment    | no                        | no              | no              | yes                 |
| Face/edge inspection | no                        | yes             | yes             | yes                 |

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Editor (Helix, neovim, …)                          │
│  starts b3d-lsp as a language server subprocess     │
└──────────┬──────────────────────────────────────────┘
           │  stdio (LSP protocol)
┌──────────▼──────────────────────────────────────────┐
│  b3d-lsp process (dev.py)                           │
│                                                     │
│  pygls LSP server ──► didChange / didSave           │
│         │                    │                      │
│         │            Tree-sitter incremental parse  │
│         │                    │                      │
│         │            classify + exec changed blocks │
│         │                    │                      │
│  diagnostics ◄──── tessellate → vtkActor            │
│                               │                     │
│                    trame-vtklocal push               │
└───────────────────────────────┬─────────────────────┘
                                │  WebSocket
┌───────────────────────────────▼─────────────────────┐
│  Browser                                            │
│  VTK.wasm renders vtkRenderWindow mirror            │
└─────────────────────────────────────────────────────┘
```

---

## Roadmap

- **Simulation support** — accept VTK dataset files (`.vtu`, `.vtp`) alongside
  build123d scripts; render scalar/vector fields with colormaps; deformed shape
  overlay for FEM output.
- **Face/edge inspection** — click to select and inspect topology, matching
  ocp-vscode.

---

## Stack

| Package                                                     | Role                                    |
| ----------------------------------------------------------- | --------------------------------------- |
| [build123d](https://github.com/gumyr/build123d)             | CAD geometry kernel (OCC wrapper)       |
| [vtk](https://vtk.org)                                      | 3D rendering pipeline                   |
| [trame](https://kitware.github.io/trame/)                   | Web application server                  |
| [trame-vtklocal](https://github.com/Kitware/trame-vtklocal) | VTK.wasm bridge                         |
| [trame-vuetify](https://github.com/Kitware/trame-vuetify)   | Vuetify 3 UI components                 |
| [tree-sitter](https://tree-sitter.github.io/)               | Incremental parser for change detection |
| [jedi](https://jedi.readthedocs.io/)                        | Semantic dep graph (cross-file imports) |
| [pygls](https://github.com/openlawlibrary/pygls)            | LSP server framework                    |

---

## Contributing

### Project structure

```
dev.py          viewer — Tree-sitter parser, VTK pipeline, trame UI, LSP server
test_dev.py     unit tests for the parsing layer (no VTK or build123d required)
body.py         example: truck body
cab.py          example: truck cab
pyproject.toml
```

### Running tests

```bash
uv sync --extra dev
uv run pytest test_dev.py
```

### Key functions in dev.py

| Function                        | Purpose                                                                 |
| ------------------------------- | ----------------------------------------------------------------------- |
| `_compute_edit(old, new)`       | Find the changed byte region between two source versions                |
| `_find_build_blocks(tree, src)` | Return all top-level build context blocks from a Tree-sitter parse tree |
| `_block_changed(node, ranges)`  | Check if a block's byte range overlaps the changed region               |
| `_defined_names(node)`          | Names bound by a top-level statement; None for unknowable forms         |
| `_load_actors(filepath)`        | Core reload — parse, diff, exec changed blocks, tessellate              |
| `_shape_to_actor(shape)`        | Tessellate a build123d Shape into a vtkActor                            |
| `_build_lsp(filepaths, loop)`   | Build pygls LanguageServer with debounced reload handlers               |
| `_build_ui(server, filepaths)`  | Constructs the trame/Vuetify toolbar and VTK view                       |
| `main()`                        | Entry point — starts viewer + LSP stdio server                          |

### Adding a new toolbar button

All UI is in `_build_ui()`. Buttons use Vuetify 3 `VBtn` wired via `server.controller`:

```python
ctrl.my_action = lambda: do_something()
vuetify3.VBtn(icon="mdi-icon-name", click=ctrl.my_action, **_btn)
```

MDI icon names: https://pictogrammers.com/library/mdi/
