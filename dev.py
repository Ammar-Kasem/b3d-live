"""Live build123d viewer — VTK WASM via trame-vtklocal, no server-side GL.

Tree-sitter is used for all parsing, giving three advantages over Python's
built-in ast module:

  1. Block-level error isolation — if one build block has a syntax error,
     tree-sitter still finds and re-runs the other blocks normally.  With
     ast.parse() a single error aborts the whole file.

  2. Metadata-only post-block classification — assignments like
     `body.part.color = Color(...)` are detected as metadata-only and applied
     to the cached actor without forcing the block to re-execute.

  3. Cross-file dependency graph — import statements are queried to build a
     reverse dep graph.  When a helper module changes, only the watched files
     that actually import it are reloaded.

Change detection uses MD5 of the normalised block source (same as before),
since tree-sitter's has_changes flag requires tree.edit() calls that are not
yet plumbed in at this layer.
"""

import argparse
import ast
import asyncio
import hashlib
import os
import sys
import types

import vtk
from tree_sitter import Language, Parser, Query, QueryCursor
import tree_sitter_python
from trame.app import get_server
from trame.ui.vuetify3 import SinglePageLayout
from trame.widgets import vuetify3, vtklocal
from watchfiles import awatch, Change

# ── Tree-sitter setup ──────────────────────────────────────────────────────────
_PY_LANGUAGE = Language(tree_sitter_python.language())
_ts_parser   = Parser(_PY_LANGUAGE)

# In tree-sitter-python ≥ 0.23 the with_item uses as_pattern instead of
# separate value:/alias: fields.
_BUILD_BLOCK_QUERY = Query(_PY_LANGUAGE, """
(with_statement
  (with_clause
    (with_item
      (as_pattern
        (call (identifier) @ctx)
        (as_pattern_target (identifier) @var))))) @block
""")

_IMPORT_QUERY = Query(_PY_LANGUAGE, """
[
  (import_from_statement module_name: (dotted_name) @module)
  (import_statement name: (dotted_name) @module)
]
""")

_BUILD_CTXS = {"BuildPart", "BuildSketch", "BuildLine"}
_META_PROPS = {"part", "sketch"}
_META_ATTRS = {"color", "label", "name"}

# ── VTK / trame globals ────────────────────────────────────────────────────────
_renderer:      vtk.vtkRenderer | None = None
_render_window: vtk.vtkRenderWindow | None = None
_view   = None
_server = None

# ── Per-file state ─────────────────────────────────────────────────────────────
# filepath -> {var_name -> (source_hash, vtkActor, build123d_obj)}
_file_cache: dict[str, dict[str, tuple[str, vtk.vtkActor, object]]] = {}

# filepath -> Tree-sitter Tree
_file_trees: dict[str, object] = {}

# filepath -> set of local filepaths it imports
_dep_graph: dict[str, set[str]] = {}

_VIEWER_FILES = {"dev.py", "viewer.py"}
_SHOW_MODULES = {"viewer", "ocp_vscode", "ocp_viewer"}


# ── Tree-sitter helpers ────────────────────────────────────────────────────────

def _find_build_blocks(tree, source: bytes) -> list[tuple[str, object]]:
    """Return [(var_name, node)] for every top-level build context block."""
    result  = []
    cursor  = QueryCursor(_BUILD_BLOCK_QUERY)
    matches = cursor.matches(tree.root_node)
    for _, caps in matches:
        ctx_nodes   = caps.get("ctx",   [])
        var_nodes   = caps.get("var",   [])
        block_nodes = caps.get("block", [])
        if not (ctx_nodes and var_nodes and block_nodes):
            continue
        ctx_node   = ctx_nodes[0]
        var_node   = var_nodes[0]
        block_node = block_nodes[0]
        if ctx_node.text.decode() not in _BUILD_CTXS:
            continue
        if block_node.parent is None or block_node.parent.type != "module":
            continue
        result.append((var_node.text.decode(), block_node))
    return result


def _referenced_names(node) -> set[str]:
    """Recursively collect all identifier text values in a node's subtree."""
    names: set[str] = set()

    def _walk(n):
        if n.type == "identifier":
            names.add(n.text.decode())
        for child in n.children:
            _walk(child)

    _walk(node)
    return names


def _is_show_call_node(node) -> bool:
    if node.type != "expression_statement":
        return False
    call = node.children[0] if node.children else None
    if not call or call.type != "call":
        return False
    func = call.child_by_field_name("function")
    return func is not None and func.text.decode() == "show"


def _parse_metadata_stmt(node, source: bytes) -> tuple[str, str, str] | None:
    """If node is `var.(part|sketch).(color|label|name) = expr`,
    return (var_name, attr_name, value_src).  Otherwise None."""
    # Top-level assignments are wrapped in expression_statement in the module
    if node.type == "expression_statement" and node.children:
        node = node.children[0]
    if node.type != "assignment":
        return None
    left  = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if not left or not right or left.type != "attribute":
        return None
    attr_node = left.child_by_field_name("attribute")
    obj_node  = left.child_by_field_name("object")
    if not attr_node or not obj_node:
        return None
    if attr_node.text.decode() not in _META_ATTRS:
        return None
    if obj_node.type != "attribute":
        return None
    prop_node = obj_node.child_by_field_name("attribute")
    var_node  = obj_node.child_by_field_name("object")
    if not prop_node or not var_node:
        return None
    if prop_node.text.decode() not in _META_PROPS:
        return None
    return (
        var_node.text.decode(),
        attr_node.text.decode(),
        source[right.start_byte:right.end_byte].decode(),
    )


def _update_dep_graph(filepath: str, tree, source: bytes) -> None:
    """Rebuild the dependency entry for filepath from its import statements."""
    local_dir = os.path.dirname(filepath)
    cursor    = QueryCursor(_IMPORT_QUERY)
    caps      = cursor.captures(tree.root_node)
    deps: set[str] = set()
    for node in caps.get("module", []):
        mod_name  = node.text.decode().split(".")[0]
        candidate = os.path.abspath(os.path.join(local_dir, mod_name + ".py"))
        if os.path.exists(candidate) and candidate != filepath:
            deps.add(candidate)
    _dep_graph[filepath] = deps


def _block_hash(block_src: str) -> str:
    """MD5 of normalised block source (whitespace/comment agnostic)."""
    try:
        return hashlib.md5(ast.unparse(ast.parse(block_src)).encode()).hexdigest()
    except SyntaxError:
        return hashlib.md5(block_src.encode()).hexdigest()


def _compile_block(block_src: str, filepath: str, start_row: int):
    """Compile block source with correct file line numbers for error messages."""
    tree = ast.parse(block_src)
    ast.increment_lineno(tree, start_row)
    return compile(tree, filepath, "exec")


# ── VTK helpers ───────────────────────────────────────────────────────────────

def _invalidate_local_modules(dirpath: str) -> None:
    dirpath = os.path.abspath(dirpath)
    for name in list(sys.modules):
        f = getattr(sys.modules[name], "__file__", None)
        if f and os.path.abspath(os.path.dirname(f)) == dirpath:
            del sys.modules[name]


def _setup_vtk():
    global _renderer, _render_window
    _renderer = vtk.vtkRenderer()
    _renderer.SetBackground(0.12, 0.12, 0.12)
    _render_window = vtk.vtkRenderWindow()
    _render_window.AddRenderer(_renderer)
    _render_window.SetOffScreenRendering(1)
    _render_window.SetSize(1280, 720)
    interactor = vtk.vtkRenderWindowInteractor()
    interactor.SetRenderWindow(_render_window)
    interactor.GetInteractorStyle().SetCurrentStyleToTrackballCamera()


def _shape_to_actor(shape) -> vtk.vtkActor:
    vertices, triangles = shape.tessellate(0.5)

    points = vtk.vtkPoints()
    points.SetNumberOfPoints(len(vertices))
    for i, v in enumerate(vertices):
        points.SetPoint(i, v.X, v.Y, v.Z)

    cells = vtk.vtkCellArray()
    for tri in triangles:
        cells.InsertNextCell(3, tri)

    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetPolys(cells)

    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(poly)
    normals.ComputePointNormalsOn()
    normals.SplittingOff()
    normals.Update()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputData(normals.GetOutput())

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    prop = actor.GetProperty()
    if shape.color is not None:
        r, g, b, a = shape.color.to_tuple()
        prop.SetColor(r, g, b)
        prop.SetOpacity(a)
    else:
        prop.SetColor(0.68, 0.78, 0.91)
    prop.SetAmbient(0.2)
    prop.SetDiffuse(0.8)
    prop.SetSpecular(0.3)
    prop.SetSpecularPower(40)
    return actor


def _stub_show_modules():
    for name in _SHOW_MODULES:
        fake      = types.ModuleType(name)
        fake.show = lambda *a, **k: None
        sys.modules[name] = fake


def _unstub_show_modules():
    for name in _SHOW_MODULES:
        sys.modules.pop(name, None)


def _extract_shape(obj):
    if obj is None:
        return None
    if hasattr(obj, "wrapped"):
        return obj
    if hasattr(obj, "part") and obj.part:
        return obj.part
    if hasattr(obj, "sketch") and obj.sketch:
        return obj.sketch
    return None


# ── Core reload ───────────────────────────────────────────────────────────────

def _load_actors(filepath: str) -> list[vtk.vtkActor]:
    """Incremental reload driven by Tree-sitter.

    Tree-sitter parses the file (tolerating syntax errors), finds build blocks,
    and classifies non-block top-level code.  Only blocks whose source hash
    changed are re-executed.  Blocks with a syntax error (block.has_error) keep
    their last good actor.  Metadata-only post-block assignments are applied to
    cached actors without re-executing the block.
    """
    try:
        source = open(filepath, "rb").read()
    except OSError as exc:
        print(f"[b3d] Cannot read {filepath}: {exc}")
        cache = _file_cache.get(filepath, {})
        return [a for _, a, _ in cache.values()]

    old_tree = _file_trees.get(filepath)
    new_tree = _ts_parser.parse(source, old_tree)
    _file_trees[filepath] = new_tree
    _update_dep_graph(filepath, new_tree, source)

    build_blocks = _find_build_blocks(new_tree, source)
    if not build_blocks:
        return []

    block_var_names = {var for var, _ in build_blocks}
    block_ranges    = {(n.start_byte, n.end_byte) for _, n in build_blocks}

    # ── Classify top-level non-block statements ──────────────────────────────
    pre_parts: list[str]                  = []
    post_geom: list[object]               = []
    post_meta: list[tuple[str, str, str]] = []

    for child in new_tree.root_node.children:
        if child.type in ("comment", "newline"):
            continue
        if (child.start_byte, child.end_byte) in block_ranges:
            continue
        if _is_show_call_node(child):
            continue
        refs = _referenced_names(child) & block_var_names
        if not refs:
            pre_parts.append(source[child.start_byte:child.end_byte].decode())
            continue
        meta = _parse_metadata_stmt(child, source)
        if meta and meta[0] in block_var_names:
            post_meta.append(meta)
        else:
            post_geom.append(child)

    post_refs: set[str] = set()
    for node in post_geom:
        post_refs |= _referenced_names(node) & block_var_names

    # ── Execute pre-nodes (imports, constants, helpers) ──────────────────────
    base_ns: dict = {}
    _stub_show_modules()
    pre_src = "\n".join(pre_parts)
    try:
        exec(compile(pre_src, filepath, "exec"), base_ns)  # noqa: S102
    except Exception as exc:
        print(f"[b3d] Setup error: {exc}")
        _unstub_show_modules()
        cache = _file_cache.get(filepath, {})
        return [a for _, a, _ in cache.values()]
    finally:
        _unstub_show_modules()

    cache      = _file_cache.setdefault(filepath, {})
    actors:    list[vtk.vtkActor]                         = []
    new_cache: dict[str, tuple[str, vtk.vtkActor, object]] = {}
    live_objs: dict[str, object]                          = {}

    # ── Process build blocks ─────────────────────────────────────────────────
    for var_name, node in build_blocks:
        # Keep last good actor for blocks with syntax errors
        if node.has_error:
            if var_name in cache:
                h, actor, obj = cache[var_name]
                actors.append(actor)
                new_cache[var_name] = (h, actor, obj)
            continue

        block_src  = source[node.start_byte:node.end_byte].decode()
        h          = _block_hash(block_src)
        needs_live = var_name in post_refs

        if not needs_live and var_name in cache and cache[var_name][0] == h:
            _, actor, obj = cache[var_name]
            actors.append(actor)
            new_cache[var_name] = (h, actor, obj)
            continue

        ns = dict(base_ns)
        try:
            code = _compile_block(block_src, filepath, node.start_point[0])
            exec(code, ns)  # noqa: S102
        except Exception as exc:
            print(f"[b3d] Block '{var_name}' error: {exc}")
            if var_name in cache:
                _, actor, obj = cache[var_name]
                actors.append(actor)
                new_cache[var_name] = cache[var_name]
            continue

        obj = ns.get(var_name)
        if needs_live:
            live_objs[var_name] = obj
            continue

        shape = _extract_shape(obj)
        if shape is None:
            print(f"[b3d] Block '{var_name}': no shape captured")
            continue
        try:
            actor = _shape_to_actor(shape)
            actors.append(actor)
            new_cache[var_name] = (h, actor, obj)
        except Exception as exc:
            print(f"[b3d] Tessellation error in '{var_name}': {exc}")

    # ── Run geometry-affecting post-nodes with live objects ──────────────────
    if post_geom and live_objs:
        post_ns  = {**base_ns, **live_objs}
        post_src = "\n".join(
            source[n.start_byte:n.end_byte].decode() for n in post_geom
        )
        _stub_show_modules()
        try:
            exec(compile(post_src, filepath, "exec"), post_ns)  # noqa: S102
        except Exception as exc:
            if not isinstance(exc, NameError):
                print(f"[b3d] Post-build error: {exc}")
        finally:
            _unstub_show_modules()

        for var_name, node in build_blocks:
            if var_name not in live_objs:
                continue
            shape = _extract_shape(post_ns.get(var_name))
            if shape is None:
                continue
            try:
                block_src = source[node.start_byte:node.end_byte].decode()
                h         = _block_hash(block_src)
                actor     = _shape_to_actor(shape)
                actors.append(actor)
                new_cache[var_name] = (h, actor, post_ns[var_name])
            except Exception as exc:
                print(f"[b3d] Tessellation error in '{var_name}': {exc}")

    # ── Apply metadata-only updates to cached actors ─────────────────────────
    for var_name, attr_name, value_src in post_meta:
        entry = new_cache.get(var_name)
        if entry is None:
            continue
        _, actor, _ = entry
        try:
            value = eval(value_src, dict(base_ns))  # noqa: S307
            if attr_name == "color" and value is not None:
                r, g, b, a = value.to_tuple()
                actor.GetProperty().SetColor(r, g, b)
                actor.GetProperty().SetOpacity(a)
        except Exception as exc:
            print(f"[b3d] Metadata '{var_name}.{attr_name}': {exc}")

    cache.clear()
    cache.update(new_cache)
    return actors


# ── File watcher ──────────────────────────────────────────────────────────────

async def _watch_and_reload(filepaths: list[str]):
    loop = asyncio.get_running_loop()
    dirs = list({os.path.dirname(fp) or "." for fp in filepaths})

    try:
        async for changes in awatch(*dirs):
            changed_abs = {
                os.path.abspath(p)
                for c, p in changes
                if c in (Change.modified, Change.added)
                and p.endswith(".py")
                and os.path.basename(p) not in _VIEWER_FILES
            }
            if not changed_abs:
                continue

            # Determine which watched files need reloading
            to_reload: set[str] = set()
            for fp in filepaths:
                fp_abs = os.path.abspath(fp)
                if fp_abs in changed_abs:
                    to_reload.add(fp)
                elif changed_abs & _dep_graph.get(fp_abs, set()):
                    # A dependency changed — drop the cached tree so the next
                    # parse has no prior state and all blocks re-hash correctly.
                    _file_trees.pop(fp_abs, None)
                    to_reload.add(fp)

            if not to_reload:
                continue

            for p in changed_abs:
                _invalidate_local_modules(os.path.dirname(p))

            all_actors: list[vtk.vtkActor] = []
            for fp in filepaths:
                if fp in to_reload:
                    actors = await loop.run_in_executor(None, _load_actors, fp)
                else:
                    cache  = _file_cache.get(fp, {})
                    actors = [a for _, a, _ in cache.values()]
                all_actors.extend(actors)

            if not all_actors:
                continue

            _renderer.RemoveAllViewProps()
            for actor in all_actors:
                _renderer.AddActor(actor)
            _renderer.ResetCamera()
            _render_window.Render()
            if _view is not None:
                _view.update()

            cached = sum(len(c) for c in _file_cache.values())
            print(f"[b3d] {len(all_actors)} shape(s), {cached} from cache")
            if _server is not None:
                _server.state.shape_count = len(all_actors)
                _server.state.cache_count = cached
                _server.state.dirty("shape_count", "cache_count")
    except asyncio.CancelledError:
        pass


# ── Axis snap ─────────────────────────────────────────────────────────────────

def _set_axis_view(direction, up):
    bounds = _renderer.ComputeVisiblePropBounds()
    if bounds[0] > bounds[1]:
        return
    cx   = (bounds[0] + bounds[1]) / 2
    cy   = (bounds[2] + bounds[3]) / 2
    cz   = (bounds[4] + bounds[5]) / 2
    dist = max(bounds[1]-bounds[0], bounds[3]-bounds[2], bounds[5]-bounds[4]) * 2.5
    camera = _renderer.GetActiveCamera()
    camera.SetFocalPoint(cx, cy, cz)
    camera.SetPosition(
        cx + direction[0]*dist,
        cy + direction[1]*dist,
        cz + direction[2]*dist,
    )
    camera.SetViewUp(*up)
    _renderer.ResetCamera()
    _render_window.Render()
    if _view is not None:
        _view.update(push_camera=True)


# ── UI ────────────────────────────────────────────────────────────────────────

def _build_ui(server, filepaths, initial_count=0):
    global _view
    state, ctrl = server.state, server.controller

    state.shape_count = initial_count
    state.cache_count = 0
    state.wireframe   = False
    state.dark_bg     = True
    state.filenames   = "  |  ".join(os.path.basename(fp) for fp in filepaths)

    def reset_camera():
        if _view is not None:
            _view.reset_camera()

    def toggle_wireframe():
        state.wireframe = not state.wireframe
        col = _renderer.GetActors()
        col.InitTraversal()
        actor = col.GetNextActor()
        while actor:
            if state.wireframe:
                actor.GetProperty().SetRepresentationToWireframe()
            else:
                actor.GetProperty().SetRepresentationToSurface()
            actor = col.GetNextActor()
        _render_window.Render()
        if _view is not None:
            _view.update()

    def toggle_background():
        state.dark_bg = not state.dark_bg
        if state.dark_bg:
            _renderer.SetBackground(0.12, 0.12, 0.12)
        else:
            _renderer.SetBackground(0.95, 0.95, 0.95)
        _render_window.Render()
        if _view is not None:
            _view.update()

    ctrl.reset_camera      = reset_camera
    ctrl.toggle_wireframe  = toggle_wireframe
    ctrl.toggle_background = toggle_background
    ctrl.view_x   = lambda: _set_axis_view(( 1,  0,  0), (0, 0, 1))
    ctrl.view_y   = lambda: _set_axis_view(( 0, -1,  0), (0, 0, 1))
    ctrl.view_z   = lambda: _set_axis_view(( 0,  0,  1), (0, 1, 0))
    ctrl.view_iso = lambda: _set_axis_view(( 1, -1,  1), (0, 0, 1))

    _btn = dict(variant="text", density="compact", size="small")

    with SinglePageLayout(server) as layout:
        layout.title.set_text("build123d")

        with layout.toolbar as tb:
            tb.density = "compact"

            vuetify3.VSpacer()
            vuetify3.VBtn(
                icon="mdi-vector-square", title="Wireframe",
                click=ctrl.toggle_wireframe, **_btn,
            )
            vuetify3.VBtn(
                icon="mdi-theme-light-dark", title="Toggle background",
                click=ctrl.toggle_background, **_btn,
            )
            vuetify3.VBtn(
                icon="mdi-fit-to-screen", title="Reset camera",
                click=ctrl.reset_camera, **_btn,
            )
            vuetify3.VDivider(vertical=True, classes="mx-2")
            vuetify3.VBtn("X", title="View along X", click=ctrl.view_x, **_btn)
            vuetify3.VBtn("Y", title="View along Y", click=ctrl.view_y, **_btn)
            vuetify3.VBtn("Z", title="View along Z", click=ctrl.view_z, **_btn)
            vuetify3.VBtn(
                icon="mdi-axis-arrow", title="Isometric",
                click=ctrl.view_iso, **_btn,
            )
            vuetify3.VSpacer()

            vuetify3.VChip(
                "{{ shape_count }} shapes · {{ cache_count }} cached",
                size="x-small", color="primary", variant="tonal", classes="mr-2",
            )
            vuetify3.VChip(
                "{{ filenames }}",
                size="x-small", variant="outlined", classes="mr-1",
            )

        with layout.content:
            with vuetify3.VContainer(fluid=True, classes="pa-0 fill-height"):
                _view = vtklocal.LocalView(
                    _render_window,
                    style="width:100%; height:100%;",
                )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    global _server
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", default=["main.py"])
    parser.add_argument("--port", type=int, default=1234)
    args = parser.parse_args()

    filepaths = [os.path.abspath(f) for f in args.files]
    _server   = get_server(client_type="vue3")

    _setup_vtk()

    all_actors: list[vtk.vtkActor] = []
    for filepath in filepaths:
        all_actors.extend(_load_actors(filepath))
    for actor in all_actors:
        _renderer.AddActor(actor)
    if all_actors:
        _renderer.ResetCamera()
        _render_window.Render()
        print(f"[b3d] {len(all_actors)} shape(s) loaded from {len(filepaths)} file(s)")

    _build_ui(_server, filepaths, initial_count=len(all_actors))

    @_server.controller.on_server_ready.add
    def _open_browser(**_):
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")

    print(f"[b3d] Watching  : {', '.join(args.files)}")
    print(f"[b3d] Browser   : http://localhost:{args.port}")

    async def _run():
        asyncio.create_task(_watch_and_reload(filepaths))
        await _server.start(exec_mode="task", open_browser=False, port=args.port)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
