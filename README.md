# b3d-live

A live-reloading browser viewer for [build123d](https://github.com/gumyr/build123d) CAD scripts.
Save a `.py` file and only the blocks that actually changed rebuild — everything else stays cached.

Two launch modes:

| Mode | Command | Reload trigger |
| ---- | ------- | -------------- |
| **File watcher** | `b3d-live body.py` | file save |
| **Editor LSP** | `b3d-lsp body.py` | every keystroke |

---

## Quick start

```bash
uv sync
```

### File-watcher mode

```bash
# view a single file
uv run b3d-live body.py

# view multiple files side by side
uv run b3d-live body.py cab.py

# custom port
uv run b3d-live body.py --port 8080
```

### Editor LSP mode

`b3d-lsp` is a language server that starts the viewer and reloads on every keystroke — no
manual startup required.  Point your editor at it as a language server for Python files.

```bash
uv run b3d-lsp body.py cab.py
```

The browser opens automatically at `http://localhost:1234`.

#### Helix

```toml
# ~/.config/helix/languages.toml
[language-server.b3d-live]
command = "/path/to/.venv/bin/b3d-lsp"
args    = ["body.py", "cab.py"]

[[language]]
name = "python"
language-servers = ["ruff", "b3d-live"]
```

Replace `/path/to/.venv` with the absolute path to the project's virtual environment.

---

## How it works

Most live viewers re-execute the entire script on every save. For complex models this means
waiting for all geometry to rebuild even when only one part changed.

b3d-live uses **Tree-sitter incremental parsing** to sync the parse tree directly with the
VTK scene graph:

1. On each save (or keystroke in LSP mode), Tree-sitter re-parses only the bytes that changed.
2. Every top-level `with BuildPart() as x:`, `with BuildSketch() as x:`, and
   `with BuildLine() as x:` block is an independent unit mapped 1:1 to a VTK actor.
3. The raw byte edit region is compared against each block's byte span — only overlapping
   blocks are re-executed. No hashing, no full-file diffing.
4. Pre-part variables defined above a block (e.g. `body_color = Color(0x4683CE)`) are
   tracked by name. When one changes, only blocks that actually reference it re-execute.
5. Top-level metadata assignments (`body.part.color = Color(...)`) update the cached actor
   property directly without re-executing the block.

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

- **Syntax errors** — Tree-sitter produces a partial tree even for broken syntax. Only the
  block containing the error loses its actor; all other blocks continue reloading normally.
- **Runtime errors** — if a single block fails to execute, its last good actor is preserved.
  Other blocks are unaffected.

---

## Toolbar

| Control       | Action                                     |
| ------------- | ------------------------------------------ |
| Wireframe     | Toggle between solid and wireframe display |
| Light/dark    | Toggle background colour                   |
| Reset camera  | Fit all shapes in view                     |
| X / Y / Z     | Snap camera to that axis                   |
| Isometric     | Snap camera to isometric view              |
| Shape counter | Live count of shapes and cache hits        |
| Filename chip | Shows which files are being watched        |

---

## Multiple files

Any number of `.py` files can be passed on the command line. Each file is watched and
reloaded independently. Their shapes are all rendered in the same scene:

```bash
b3d-live body.py cab.py wheels.py
b3d-lsp  body.py cab.py wheels.py
```

Changes to one file do not invalidate the cache of the others.

### Shared constants across files

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

When `common.py` changes, b3d-live's dependency graph identifies which watched files
import it and reloads only those — and within each file, only the blocks that reference
the changed variable.

---

## How it compares

|                      | b3d-live / b3d-lsp        | ocp-vscode      | cq-editor       | YACV                |
| -------------------- | ------------------------- | --------------- | --------------- | ------------------- |
| Editor coupling      | none — any editor         | VS Code only    | built-in editor | none                |
| Reload unit          | changed block only        | whole file      | whole file      | whole file          |
| Change detection     | Tree-sitter byte range    | file hash       | file hash       | file hash           |
| Rendering            | VTK.wasm in browser       | embedded window | PyQT window     | OCP.wasm in browser |
| Multi-file watch     | yes                       | no              | no              | no                  |
| Cross-file deps      | yes                       | no              | no              | no                  |
| Error resilience     | per-block, last good kept | blanks on error | blanks on error | blanks on error     |
| Keystroke reload     | yes (LSP mode)            | no              | no              | no                  |
| Static deployment    | no                        | no              | no              | yes                 |
| Face/edge inspection | no                        | yes             | yes             | yes                 |

---

## Architecture

### File-watcher mode (`b3d-live`)

```
┌─────────────────────────────────────────────────────┐
│  Python process (dev.py)                            │
│                                                     │
│  watchfiles ──► Tree-sitter incremental parse       │
│                       │                             │
│               classify blocks                       │
│                       │                             │
│   ┌─────── byte range overlap? ───────┐             │
│   │ no                                │ yes         │
│ reuse actor                      exec block         │
│                                        │            │
│                                   tessellate        │
│                                        │            │
│                                   vtkActor          │
│                                        │            │
│                    trame-vtklocal push              │
└──────────────────────┬──────────────────────────────┘
                       │  WebSocket
┌──────────────────────▼──────────────────────────────┐
│  Browser                                            │
│  VTK.wasm renders vtkRenderWindow mirror            │
└─────────────────────────────────────────────────────┘
```

### LSP mode (`b3d-lsp`)

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

- **Simulation support** — accept VTK dataset files (`.vtu`, `.vtp`) alongside build123d
  scripts; render scalar/vector fields with colormaps; deformed shape overlay for FEM output.
- **Face/edge inspection** — click to select and inspect topology, matching ocp-vscode.

---

## Stack

| Package                                                     | Role                              |
| ----------------------------------------------------------- | --------------------------------- |
| [build123d](https://github.com/gumyr/build123d)             | CAD geometry kernel (OCC wrapper) |
| [vtk](https://vtk.org)                                      | 3D rendering pipeline             |
| [trame](https://kitware.github.io/trame/)                   | Web application server            |
| [trame-vtklocal](https://github.com/Kitware/trame-vtklocal) | VTK.wasm bridge                   |
| [trame-vuetify](https://github.com/Kitware/trame-vuetify)   | Vuetify 3 UI components           |
| [watchfiles](https://github.com/samuelcolvin/watchfiles)    | Cross-platform file watcher       |
| [tree-sitter](https://tree-sitter.github.io/)               | Incremental parser for change detection |
| [pygls](https://github.com/openlawlibrary/pygls)            | LSP server framework              |

---

## Contributing

### Project structure

```
dev.py          main viewer — watcher, Tree-sitter parser, VTK pipeline, trame UI, LSP server
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

| Function                       | Purpose                                                                 |
| ------------------------------ | ----------------------------------------------------------------------- |
| `_compute_edit(old, new)`      | Find the changed byte region between two source versions                |
| `_find_build_blocks(tree, src)`| Return all top-level build context blocks from a Tree-sitter parse tree |
| `_block_changed(node, ranges)` | Check if a block's byte range overlaps the changed region               |
| `_defined_names(node)`         | Names bound by a top-level statement; None for unknowable forms         |
| `_load_actors(filepath)`       | Core reload — parse, diff, exec changed blocks, tessellate              |
| `_shape_to_actor(shape)`       | Tessellate a build123d Shape into a vtkActor                            |
| `_watch_and_reload(filepaths)` | Async watcher loop — detects saves, calls `_load_actors`, updates scene |
| `_build_lsp(filepaths, loop)`  | Build pygls LanguageServer with debounced reload handlers               |
| `_build_ui(server, filepaths)` | Constructs the trame/Vuetify toolbar and VTK view                       |
| `main()`                       | Entry point for `b3d-live` — file-watcher mode                         |
| `lsp_main()`                   | Entry point for `b3d-lsp` — self-contained viewer+LSP stdio server     |

### Adding a new toolbar button

All UI is in `_build_ui()`. Buttons use Vuetify 3 `VBtn` wired via `server.controller`:

```python
ctrl.my_action = lambda: do_something()
vuetify3.VBtn(icon="mdi-icon-name", click=ctrl.my_action, **_btn)
```

MDI icon names: https://pictogrammers.com/library/mdi/
