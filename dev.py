"""Live build123d viewer — VTK WASM via trame-vtklocal, no server-side GL."""

import argparse
import asyncio
import ctypes
import hashlib
import os
import re
import struct
import sys
import types

import vtk
from trame.app import get_server
from trame.ui.html import DivLayout
from trame.widgets import vtklocal

_renderer: vtk.vtkRenderer | None = None
_render_window: vtk.vtkRenderWindow | None = None
_view = None

# cell index -> (source_hash, vtkActor)
_cell_cache: dict[int, tuple[str, vtk.vtkActor]] = {}

_libc = ctypes.CDLL(None)
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO    = 0x00000080


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
        h = hashlib.md5(cell.encode()).hexdigest()

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
    loop  = asyncio.get_running_loop()
    fname = os.path.basename(filepath).encode()

    ifd = _libc.inotify_init()
    _libc.inotify_add_watch(
        ifd,
        (os.path.dirname(filepath) or ".").encode(),
        _IN_CLOSE_WRITE | _IN_MOVED_TO,
    )

    gate = asyncio.Event()

    def _on_readable():
        raw = os.read(ifd, 4096)
        off = 0
        while off < len(raw):
            _wd, mask, _cookie, nlen = struct.unpack_from("iIII", raw, off)
            off += 16
            name = raw[off : off + nlen].rstrip(b"\x00")
            off += nlen
            if name == fname and mask & (_IN_CLOSE_WRITE | _IN_MOVED_TO):
                gate.set()

    loop.add_reader(ifd, _on_readable)
    try:
        while True:
            await gate.wait()
            gate.clear()

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
            print(f"[b3d] {len(actors)} shape(s) — {cached} from cache")
    finally:
        loop.remove_reader(ifd)
        os.close(ifd)


def _build_ui(server):
    global _view
    with DivLayout(server) as layout:
        layout.root.style = "width:100vw; height:100vh; margin:0; padding:0;"
        _view = vtklocal.LocalView(_render_window, style="width:100%; height:100%;")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", nargs="?", default="main.py")
    parser.add_argument("--port", type=int, default=1234)
    args = parser.parse_args()

    filepath = os.path.abspath(args.file)
    server = get_server(client_type="vue3")

    _setup_vtk()

    actors = _load_actors(filepath)
    if actors:
        for actor in actors:
            _renderer.AddActor(actor)
        _renderer.ResetCamera()
        _render_window.Render()
        print(f"[b3d] {len(actors)} shape(s) loaded")

    _build_ui(server)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.call_soon(loop.create_task, _watch_and_reload(filepath))

    print(f"[b3d] Watching  : {args.file}")
    print(f"[b3d] Browser   : http://localhost:{args.port}")
    server.start(open_browser=True, port=args.port)


if __name__ == "__main__":
    main()
