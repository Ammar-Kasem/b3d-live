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

The current AST approach has two known limitations:

**1. Syntax error granularity**
When `ast.parse()` fails, the entire file is treated as broken and all blocks fall back
to their last good state. With [Tree-sitter](https://tree-sitter.github.io/tree-sitter/),
which can parse broken syntax and still produce a partial tree, it would be possible to
identify exactly which block contains the error and keep all other blocks reloading normally.

**2. Post-node classification**
Any top-level statement that references a block variable is currently treated as
geometry-affecting and forces that block to re-execute on every save (bypassing the cache).
This means:

```python
with BuildPart() as body:
    ...

body.part.color = Color(0x4683CE)  # forces body to re-execute every time
```

Tree-sitter's query syntax makes it straightforward to distinguish metadata-only statements
(color, label) from geometry-affecting ones (joint connections, transforms), so only the
latter invalidate the cache:

```scheme
; metadata-only: does not need live re-execution
(assignment
  left: (attribute
    object: (identifier) @var
    attribute: (identifier) @attr (#match? @attr "color|label|name")))
```

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
