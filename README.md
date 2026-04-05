# b3d-live

A live-reloading browser viewer for [build123d](https://github.com/gumyr/build123d) CAD scripts.
Edits to your `.py` files appear in the browser instantly — only the parts that actually changed
are re-built.

---

## Quick start

```bash
# install
uv sync

# view a single file
uv run b3d-live body.py

# view multiple files side by side
uv run b3d-live body.py cab.py

# custom port
uv run b3d-live body.py --port 8080
```

The browser opens automatically at `http://localhost:1234`.

---

## How it works

Most live viewers re-execute the entire script on every save. For complex models this means
waiting for all geometry to rebuild even when only one part changed.

b3d-live avoids this with **AST-based incremental reloading**:

1. On each file save, the script is parsed with Python's `ast` module.
2. Every top-level `with BuildPart() as x:`, `with BuildSketch() as x:`, and
   `with BuildLine() as x:` block is treated as an independent unit.
3. Each block's source is normalised (`ast.unparse`) and hashed (MD5).
4. Only blocks whose hash changed since the last reload are re-executed.
   All other blocks reuse their cached VTK actor — no geometry rebuild, no tessellation.
5. Top-level code outside build blocks is split into:
   - **pre-nodes** — imports and constants, run before any block executes
   - **post-nodes** — code that references block variables (joint connections,
     color assignments), run after all blocks with live objects in scope

Rendering is handled entirely client-side via **VTK.wasm** (through
[trame-vtklocal](https://github.com/Kitware/trame-vtklocal)). The Python server
tessellates geometry and pushes a VTK render window state to the browser; no
server-side OpenGL is required.

---

## Resilience

The viewer is designed to stay usable while you are mid-edit:

- **Syntax errors** — `ast.parse()` fails but the last good actors are kept on screen.
  The error is printed to the terminal.
- **Runtime errors** — if a single block fails to execute, its last good actor is
  preserved. Other blocks are unaffected and continue to reload normally.

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
```

Changes to one file do not invalidate the cache of the others.

---

## How it compares to other viewers

|                      | b3d-live                  | ocp-vscode      | cq-editor       | YACV                |
| -------------------- | ------------------------- | --------------- | --------------- | ------------------- |
| Editor coupling      | none — any editor         | VS Code only    | built-in editor | none                |
| Reload unit          | changed block only        | whole file      | whole file      | whole file          |
| Rendering            | VTK.wasm in browser       | embedded window | PyQT window     | OCP.wasm in browser |
| Multi-file watch     | yes                       | no              | no              | no                  |
| Error resilience     | last good state preserved | blanks on error | blanks on error | blanks on error     |
| Static deployment    | no                        | no              | no              | yes                 |
| Browser playground   | no                        | no              | no              | yes                 |
| Face/edge inspection | no                        | yes             | yes             | yes                 |

**The key differentiator** is block-level incremental reload. A truck model with a body,
cab, and four wheels means only the block you edited rebuilds — the other five stay cached.
For heavy geometry this is the difference between a 200 ms feedback loop and a 10 s wait.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Python process (dev.py)                        │
│                                                  │
│  watchfiles ──► AST parse                       │
│                    │                             │
│              classify blocks                     │
│                    │                             │
│         ┌──── hash match? ────┐                 │
│         │ yes                 │ no               │
│      reuse actor         exec block             │
│                               │                  │
│                          tessellate              │
│                               │                  │
│                         vtkActor                 │
│                               │                  │
│              trame-vtklocal push                 │
└──────────────────┬──────────────────────────────┘
                   │  WebSocket
┌──────────────────▼──────────────────────────────┐
│  Browser                                        │
│  VTK.wasm renders vtkRenderWindow mirror        │
└─────────────────────────────────────────────────┘
```

---

## Roadmap

### Tree-sitter integration

The current `ast`-based approach has three limitations that Tree-sitter addresses cleanly.

**The core idea: sync the Tree-sitter graph with the VTK scene graph**

Python's `ast` module has no incremental state — it re-parses the entire file from scratch
on every save, then the viewer re-hashes every block to find what changed. Tree-sitter's
incremental parser tracks exactly which nodes changed via a `has_changes` flag. This means
the MD5 hash cache can be replaced by a direct 1:1 mapping between Tree-sitter nodes and
VTK actors:

```
Tree-sitter node (BuildPart body) ←── 1:1 ──► vtkActor
       has_changes = True                      invalidate + re-execute
       has_changes = False                     untouched, no hash needed
```

Scene graph operations become direct consequences of parse tree changes:

| Tree-sitter event | VTK scene action |
|-------------------|-----------------|
| Node added | Create actor, add to scene |
| Node removed | Remove actor from scene |
| Node edited (`has_changes`) | Re-execute block, replace actor |
| Node unchanged | Actor stays, nothing runs |

**1. Syntax error granularity**
Currently when `ast.parse()` fails the entire file falls back to its last good state.
Tree-sitter can parse broken syntax and still produce a partial tree, so only the block
containing the error loses its actor — all other blocks continue reloading normally.

**2. Post-node classification**
Any top-level statement that references a block variable currently forces that block to
re-execute on every save, bypassing the cache:

```python
with BuildPart() as body:
    ...

body.part.color = Color(0x4683CE)  # currently forces body to re-execute every time
```

Tree-sitter queries can distinguish metadata-only statements (color, label) from
geometry-affecting ones (joint connections, transforms), so only the latter invalidate
the cache:

```scheme
; metadata-only: does not need live re-execution
(assignment
  left: (attribute
    object: (identifier) @var
    attribute: (identifier) @attr (#match? @attr "color|label|name")))
```

**3. Cross-file dependency tracking**
When any file in the project is saved, the current watcher invalidates all local modules
and re-runs every watched file. With Tree-sitter maintaining a project-wide parse state,
import statements are queried to build a reverse dependency graph:

```scheme
(import_from_statement module_name: (dotted_name) @module)
(import_statement name: (dotted_name) @module)
```

When `common.py` changes, Tree-sitter can identify exactly which blocks in dependent files
reference the changed definitions — not just which files need re-running. The VTK scene
gets surgical updates: only the actors whose source actually changed are replaced.
watchfiles keeps its one job (detecting the save event); Tree-sitter handles parse, diff,
dependency traversal, and block identification in a single incremental pass.

### Simulation support

The architecture is intentionally kept close to VTK, which is the standard toolkit for
scientific visualisation. Adding FEM simulation output (stress fields, displacement, flow)
would require:

- Accepting VTK dataset files (`.vtu`, `.vtp`) alongside build123d scripts
- Rendering scalar/vector fields with colormaps and scalar bars
- Deformed shape overlay

The geometry pipeline (build123d → BREP) is kept separate from the mesh pipeline
intentionally, so FEM meshers (Gmsh, Netgen) can consume the BREP directly without
re-tessellating at viewer resolution.

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

---

## Contributing

### Project structure

```
dev.py        main viewer — watcher, AST parser, VTK pipeline, trame UI
body.py       example: truck body
cab.py        example: truck cab
pyproject.toml
```

### Key functions in dev.py

| Function                       | Purpose                                                                     |
| ------------------------------ | --------------------------------------------------------------------------- |
| `_build_var(node)`             | Returns the `as` variable name if a node is a top-level build context block |
| `_load_actors(filepath)`       | Core reload function — parses, classifies, diffs, executes, tessellates     |
| `_shape_to_actor(shape)`       | Tessellates a build123d Shape into a vtkActor                               |
| `_watch_and_reload(filepaths)` | Async loop — watches files, calls `_load_actors`, pushes to browser         |
| `_build_ui(server, filepaths)` | Constructs the trame/Vuetify toolbar and VTK view                           |

### Running locally

```bash
git clone ...
cd build123d
uv sync
uv run b3d-live body.py
```

### Adding a new toolbar button

All UI is in `_build_ui()` in `dev.py`. Buttons use Vuetify 3 `VBtn` and controller
callbacks wired via `server.controller`:

```python
ctrl.my_action = lambda: do_something()
vuetify3.VBtn(icon="mdi-icon-name", click=ctrl.my_action, **_btn)
```

MDI icon names: https://pictogrammers.com/library/mdi/ — for example `mdi-file-cad`.
