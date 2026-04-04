"""Live build123d viewer — VTK WASM via trame-vtklocal, no server-side GL."""

import argparse
import ast
import asyncio
import hashlib
import os
import re
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

# cell index -> (source_hash, vtkActor)
_cell_cache: dict[int, tuple[str, vtk.vtkActor]] = {}

# .py files belonging to the viewer itself — never trigger a CAD reload
_VIEWER_FILES = {"dev.py", "viewer.py"}


def _cell_hash(src: str) -> str:
    """Hash a cell's semantic content, ignoring comments and whitespace."""
    try:
        normalized = ast.unparse(ast.parse(src))
    except SyntaxError:
        normalized = src
    return hashlib.md5(normalized.encode()).hexdigest()


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


def _load_actors(filepath: str) -> list[vtk.vtkActor]:
    """Execute the file cell by cell, reusing cached actors for unchanged cells.

    The file is split on # %% markers. The first cell (imports) always runs to
    build a base namespace. Each subsequent cell executes independently in a
    copy of that namespace; if its source hash matches the cache the cached
    actor is reused and OCC + tessellation are skipped entirely.
    """
    with open(filepath) as f:
        src = f.read()

    cells = [c.strip() for c in re.split(r"^# %%[^\n]*$", src, flags=re.MULTILINE) if c.strip()]
    if not cells:
        return []

    # First cell: imports — always execute, builds the base namespace.
    base_ns: dict = {}
    fake = types.ModuleType("viewer")
    fake.show = lambda *a, **k: None
    sys.modules["viewer"] = fake
    try:
        exec(compile(cells[0], filepath, "exec"), base_ns)  # noqa: S102
    except Exception as exc:
        print(f"[b3d] Error in setup cell: {exc}")
        return []
    finally:
        sys.modules.pop("viewer", None)

    actors: list[vtk.vtkActor] = []
    new_cache: dict[int, tuple[str, vtk.vtkActor]] = {}

    for i, cell in enumerate(cells[1:], 1):
        h = _cell_hash(cell)

        if i in _cell_cache and _cell_cache[i][0] == h:
            actors.append(_cell_cache[i][1])
            new_cache[i] = _cell_cache[i]
            continue

        # Changed cell — execute in its own copy of the import namespace.
        ns = dict(base_ns)
        captured: list = []

        def _show(*args, **_):
            for a in args:
                if hasattr(a, "wrapped"):
                    captured.append(a)
                elif hasattr(a, "part") and hasattr(a.part, "wrapped"):
                    captured.append(a.part)
                elif hasattr(a, "sketch") and hasattr(a.sketch, "wrapped"):
                    captured.append(a.sketch)

        ns["show"] = _show

        try:
            exec(compile(cell, filepath, "exec"), ns)  # noqa: S102
        except NameError:
            pass  # expected when a cell references shapes from other cells (e.g. show())
        except Exception as exc:
            print(f"[b3d] Cell {i} error: {exc}")

        if not captured:
            from build123d import Shape, BuildPart, BuildSketch
            for k, v in ns.items():
                if k in base_ns:
                    continue
                if isinstance(v, Shape):
                    captured.append(v)
                elif isinstance(v, BuildPart) and v.part:
                    captured.append(v.part)
                elif isinstance(v, BuildSketch) and v.sketch:
                    captured.append(v.sketch)

        for shape in captured:
            try:
                actor = _shape_to_actor(shape)
                actors.append(actor)
                new_cache[i] = (h, actor)
                break
            except Exception as exc:
                print(f"[b3d] Tessellation error in cell {i}: {exc}")

    _cell_cache.clear()
    _cell_cache.update(new_cache)
    return actors


async def _watch_and_reload(filepath: str):
    loop    = asyncio.get_running_loop()
    dirpath = os.path.dirname(filepath) or "."

    try:
        async for changes in awatch(dirpath):
            changed = {
                os.path.basename(p)
                for c, p in changes
                if c in (Change.modified, Change.added) and p.endswith(".py")
            }
            if not (changed - _VIEWER_FILES):
                continue

            _invalidate_local_modules(dirpath)

            actors = await loop.run_in_executor(None, _load_actors, filepath)
            if not actors:
                continue

            _renderer.RemoveAllViewProps()
            for actor in actors:
                _renderer.AddActor(actor)
            _renderer.ResetCamera()
            _render_window.Render()
            if _view is not None:
                _view.update()
            cached = sum(1 for i in _cell_cache if _cell_cache[i][0] != "")
            print(f"[b3d] {len(actors)} shape(s), {cached} from cache")
            if _server is not None:
                _server.state.shape_count = len(actors)
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


def _build_ui(server, filepath, initial_count=0):
    global _view
    state, ctrl = server.state, server.controller

    state.shape_count = initial_count
    state.cache_count = 0
    state.wireframe = False
    state.dark_bg = True
    state.filename = os.path.basename(filepath)

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
                "{{ filename }}",
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
    parser.add_argument("file", nargs="?", default="main.py")
    parser.add_argument("--port", type=int, default=1234)
    args = parser.parse_args()

    filepath = os.path.abspath(args.file)
    _server = get_server(client_type="vue3")

    _setup_vtk()

    actors = _load_actors(filepath)
    if actors:
        for actor in actors:
            _renderer.AddActor(actor)
        _renderer.ResetCamera()
        _render_window.Render()
        print(f"[b3d] {len(actors)} shape(s) loaded")

    _build_ui(_server, filepath, initial_count=len(actors))

    @_server.controller.on_server_ready.add
    def _open_browser(**_):
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")

    print(f"[b3d] Watching  : {args.file}")
    print(f"[b3d] Browser   : http://localhost:{args.port}")

    async def _run():
        asyncio.create_task(_watch_and_reload(filepath))
        await _server.start(exec_mode="task", open_browser=False, port=args.port)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
