"""Live build123d viewer — VTK WASM via trame-vtklocal, no server-side GL."""

import argparse
import ast
import asyncio
import hashlib
import os
import sys
import types

import vtk
from trame.app import get_server
from trame.ui.vuetify3 import SinglePageLayout
from trame.widgets import vuetify3, vtklocal
from watchfiles import awatch, Change

_renderer: vtk.vtkRenderer | None = None
_render_window: vtk.vtkRenderWindow | None = None
_view = None
_server = None

# filepath -> {block_index -> (source_hash, vtkActor)}
_file_cache: dict[str, dict[int, tuple[str, vtk.vtkActor]]] = {}

# .py files belonging to the viewer itself — never trigger a CAD reload
_VIEWER_FILES = {"dev.py", "viewer.py"}


_BUILD_CTXS = {"BuildPart", "BuildSketch", "BuildLine"}


def _build_var(node: ast.stmt) -> str | None:
    """Return the 'as' variable name if node is a top-level with BuildPart/Sketch/Line,
    otherwise None."""
    if not isinstance(node, ast.With):
        return None
    for item in node.items:
        call = item.context_expr
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in _BUILD_CTXS
            and isinstance(item.optional_vars, ast.Name)
        ):
            return item.optional_vars.id
    return None


def _invalidate_local_modules(dirpath: str) -> None:
    """Remove modules loaded from dirpath from sys.modules so they are
    re-imported fresh on the next exec."""
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
    prop.SetColor(0.68, 0.78, 0.91)
    prop.SetAmbient(0.2)
    prop.SetDiffuse(0.8)
    prop.SetSpecular(0.3)
    prop.SetSpecularPower(40)
    return actor


_SHOW_MODULES = {"viewer", "ocp_vscode", "ocp_viewer"}


def _stub_show_modules():
    for name in _SHOW_MODULES:
        fake = types.ModuleType(name)
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


def _load_actors(filepath: str) -> list[vtk.vtkActor]:
    """Parse the file with ast, find every top-level with BuildPart/Sketch/Line block,
    and re-execute only those whose source hash changed.

    Top-level code that is not a build block (imports, joint connections, show calls)
    is split into:
      - import_nodes  : run first to populate base_ns
      - post_nodes    : run after all build blocks with all block vars in scope

    Build blocks referenced in post_nodes (e.g. for joint connections) are always
    re-executed so live objects are available; their actors are re-tessellated after
    post_nodes run to pick up any location changes.
    """
    with open(filepath) as f:
        src = f.read()

    try:
        tree = ast.parse(src, filename=filepath)
    except SyntaxError as exc:
        print(f"[b3d] Syntax error: {exc}")
        return []

    # Classify top-level nodes
    build_blocks:  list[tuple[str, ast.With]] = []
    other_nodes:   list[ast.stmt] = []

    for node in tree.body:
        var = _build_var(node)
        if var:
            build_blocks.append((var, node))
        else:
            other_nodes.append(node)

    if not build_blocks:
        return []

    # Split other_nodes into pre (runs before blocks) and post (runs after).
    # A node goes to post only if it references a build-block variable AND is
    # not a bare show() call (which is stubbed and has no side effects we care
    # about — keeping it in pre avoids forcing those blocks out of cache).
    block_var_names = {var for var, _ in build_blocks}

    def _is_show_call(node: ast.stmt) -> bool:
        return (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "show"
        )

    pre_nodes:  list[ast.stmt] = []
    post_nodes: list[ast.stmt] = []
    for node in other_nodes:
        if _is_show_call(node):
            continue  # skip entirely — args reference block vars and would cause NameError
        refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        if refs & block_var_names:
            post_nodes.append(node)
        else:
            pre_nodes.append(node)

    # post_refs: block vars referenced in post_nodes — must always run live
    post_refs: set[str] = {
        n.id
        for pnode in post_nodes
        for n in ast.walk(pnode)
        if isinstance(n, ast.Name)
    } & block_var_names

    # Execute pre_nodes → base_ns (imports, constants like truck_color, helpers)
    base_ns: dict = {}
    _stub_show_modules()
    pre_mod = ast.fix_missing_locations(ast.Module(body=pre_nodes, type_ignores=[]))
    try:
        exec(compile(pre_mod, filepath, "exec"), base_ns)  # noqa: S102
    except Exception as exc:
        print(f"[b3d] Setup error: {exc}")
        _unstub_show_modules()
        return []
    finally:
        _unstub_show_modules()

    cache     = _file_cache.setdefault(filepath, {})
    actors:    list[vtk.vtkActor] = []
    new_cache: dict[int, tuple[str, vtk.vtkActor]] = {}
    live_objs: dict[str, object] = {}  # var_name -> live BuildPart (for post_nodes)

    for i, (var_name, node) in enumerate(build_blocks):
        h   = hashlib.md5(ast.unparse(node).encode()).hexdigest()
        mod = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))

        needs_live = var_name in post_refs

        if not needs_live and i in cache and cache[i][0] == h:
            actors.append(cache[i][1])
            new_cache[i] = cache[i]
            continue

        # Execute the block
        ns = dict(base_ns)
        try:
            exec(compile(mod, filepath, "exec"), ns)  # noqa: S102
        except Exception as exc:
            print(f"[b3d] Block '{var_name}' error: {exc}")
            continue

        obj = ns.get(var_name)
        if needs_live:
            live_objs[var_name] = obj
            continue  # tessellate after post_nodes

        shape = _extract_shape(obj)
        if shape is None:
            print(f"[b3d] Block '{var_name}': no shape captured")
            continue
        try:
            actor = _shape_to_actor(shape)
            actors.append(actor)
            new_cache[i] = (h, actor)
        except Exception as exc:
            print(f"[b3d] Tessellation error in '{var_name}': {exc}")

    # Run post-build code (joint connections, etc.) with all live objects in scope
    if post_nodes and live_objs:
        post_ns = {**base_ns, **live_objs}
        post_mod = ast.fix_missing_locations(ast.Module(body=post_nodes, type_ignores=[]))
        _stub_show_modules()
        try:
            exec(compile(post_mod, filepath, "exec"), post_ns)  # noqa: S102
        except Exception as exc:
            if not isinstance(exc, NameError):
                print(f"[b3d] Post-build error: {exc}")
        finally:
            _unstub_show_modules()

        # Re-tessellate post_refs blocks — joint connections may have moved them
        for i, (var_name, node) in enumerate(build_blocks):
            if var_name not in live_objs:
                continue
            h     = hashlib.md5(ast.unparse(node).encode()).hexdigest()
            shape = _extract_shape(post_ns.get(var_name))
            if shape is None:
                continue
            try:
                actor = _shape_to_actor(shape)
                actors.append(actor)
                new_cache[i] = (h, actor)
            except Exception as exc:
                print(f"[b3d] Tessellation error in '{var_name}': {exc}")

    cache.clear()
    cache.update(new_cache)
    return actors


async def _watch_and_reload(filepaths: list[str]):
    loop = asyncio.get_running_loop()
    dirs = list({os.path.dirname(fp) or "." for fp in filepaths})

    try:
        async for changes in awatch(*dirs):
            changed = {
                os.path.basename(p)
                for c, p in changes
                if c in (Change.modified, Change.added) and p.endswith(".py")
            }
            if not (changed - _VIEWER_FILES):
                continue

            for d in dirs:
                _invalidate_local_modules(d)

            all_actors: list[vtk.vtkActor] = []
            for filepath in filepaths:
                all_actors.extend(
                    await loop.run_in_executor(None, _load_actors, filepath)
                )
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


def _set_axis_view(direction, up):
    bounds = _renderer.ComputeVisiblePropBounds()
    if bounds[0] > bounds[1]:
        return  # no visible props
    cx = (bounds[0] + bounds[1]) / 2
    cy = (bounds[2] + bounds[3]) / 2
    cz = (bounds[4] + bounds[5]) / 2
    dist = max(bounds[1]-bounds[0], bounds[3]-bounds[2], bounds[5]-bounds[4]) * 2.5
    camera = _renderer.GetActiveCamera()
    camera.SetFocalPoint(cx, cy, cz)
    camera.SetPosition(cx + direction[0]*dist, cy + direction[1]*dist, cz + direction[2]*dist)
    camera.SetViewUp(*up)
    _renderer.ResetCamera()
    _render_window.Render()
    if _view is not None:
        _view.update(push_camera=True)


def _build_ui(server, filepaths, initial_count=0):
    global _view
    state, ctrl = server.state, server.controller

    state.shape_count = initial_count
    state.cache_count = 0
    state.wireframe = False
    state.dark_bg = True
    state.filenames = "  |  ".join(os.path.basename(fp) for fp in filepaths)

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

            # ── centre: all action buttons ──────────────────────────────
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

            # ── right: live counters + filename ─────────────────────────
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


def main():
    global _server
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", default=["main.py"])
    parser.add_argument("--port", type=int, default=1234)
    args = parser.parse_args()

    filepaths = [os.path.abspath(f) for f in args.files]
    _server = get_server(client_type="vue3")

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
