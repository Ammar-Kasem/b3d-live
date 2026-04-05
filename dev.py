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

Change detection uses tree.edit() + old_tree.changed_ranges(new_tree) so only
blocks whose byte range overlaps the changed region are re-executed.  No
hashing required.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import os
import pathlib
import sys
import traceback
import threading
import types
import urllib.parse

# Heavy deps (vtk, trame, tree-sitter, watchfiles) are imported inside main()
# so that `b3d-lsp` starts instantly without loading the viewer stack.

# ── Tree-sitter globals (set by main()) ───────────────────────────────────────
_PY_LANGUAGE       = None
_ts_parser         = None
_BUILD_BLOCK_QUERY = None
_IMPORT_QUERY      = None

_BUILD_CTXS = {"BuildPart", "BuildSketch", "BuildLine"}
_META_PROPS = {"part", "sketch"}
_META_ATTRS = {"color", "label", "name"}

# ── VTK / trame globals ────────────────────────────────────────────────────────
_renderer      = None
_render_window = None
_view          = None
_server        = None

# ── Per-file state ─────────────────────────────────────────────────────────────
# filepath -> {var_name -> (vtkActor, build123d_obj)}
_file_cache:   dict = {}
_file_trees:   dict[str, object] = {}   # filepath -> Tree-sitter Tree
_file_sources: dict[str, bytes]  = {}   # filepath -> last parsed source bytes
_dep_graph:    dict[str, set[str]] = {} # filepath -> set of local deps

_VIEWER_FILES = {"dev.py", "viewer.py"}
_SHOW_MODULES = {"viewer", "ocp_vscode", "ocp_viewer"}

# LSP state
_file_diagnostics:   dict[str, list] = {}       # filepath -> lsprotocol Diagnostic list
_lsp_debounce_tasks: dict[str, asyncio.Task] = {}


# ── Tree-sitter helpers ────────────────────────────────────────────────────────

def _compute_edit(old: bytes, new: bytes) -> tuple[int, int, int]:
    """Return (start_byte, old_end_byte, new_end_byte) of the changed region."""
    start = 0
    while start < len(old) and start < len(new) and old[start] == new[start]:
        start += 1
    old_end, new_end = len(old), len(new)
    while old_end > start and new_end > start and old[old_end-1] == new[new_end-1]:
        old_end -= 1
        new_end -= 1
    return start, old_end, new_end


def _byte_to_point(src: bytes, offset: int) -> tuple[int, int]:
    """Convert a byte offset to a (row, col) tree-sitter point."""
    prefix  = src[:offset]
    row     = prefix.count(b"\n")
    last_nl = prefix.rfind(b"\n")
    return (row, offset - (last_nl + 1))


class _ByteRange:
    """Minimal range object used for raw-byte change detection."""
    __slots__ = ("start_byte", "end_byte")
    def __init__(self, start_byte: int, end_byte: int):
        self.start_byte = start_byte
        self.end_byte   = end_byte


def _defined_names(node) -> set[str] | None:
    """Return names defined by a top-level statement, or None if unknowable.

    None means the statement could bind an unpredictable set of names (e.g.
    star imports), so callers must treat every name as potentially changed.

    Handles:
      x = expr                    → {"x"}
      x += expr                   → {"x"}
      from mod import a, b as c   → {"a", "c"}
      from mod import *           → None
      import foo, bar as b        → {"foo", "b"}
      def f(): / class C:         → {"f"} / {"C"}
      anything else               → None  (safe fallback)
    """
    t = node.type
    if t == "expression_statement" and node.children:
        return _defined_names(node.children[0])

    if t == "assignment":
        left = node.child_by_field_name("left")
        if left and left.type == "identifier":
            return {left.text.decode()}
        return None  # tuple-unpack or attribute — don't guess

    if t == "augmented_assignment":
        left = node.child_by_field_name("left")
        if left and left.type == "identifier":
            return {left.text.decode()}
        return None

    if t == "import_from_statement":
        for child in node.children:
            if child.type == "wildcard_import":
                return None
        module_id = (node.child_by_field_name("module_name") or object()).id
        result: set[str] = set()
        for child in node.children:
            if child.type == "aliased_import":
                alias = child.child_by_field_name("alias")
                name  = child.child_by_field_name("name")
                target = alias or name
                if target:
                    result.add(target.text.decode().split(".")[0])
            elif child.type == "dotted_name" and child.id != module_id:
                result.add(child.text.decode().split(".")[0])
        return result or None

    if t == "import_statement":
        result = set()
        for child in node.children:
            if child.type == "aliased_import":
                alias = child.child_by_field_name("alias")
                name  = child.child_by_field_name("name")
                target = alias or name
                if target:
                    result.add(target.text.decode().split(".")[0])
            elif child.type == "dotted_name":
                result.add(child.text.decode().split(".")[0])
        return result or None

    if t == "function_definition":
        n = node.child_by_field_name("name")
        return {n.text.decode()} if n else None

    if t == "class_definition":
        n = node.child_by_field_name("name")
        return {n.text.decode()} if n else None

    return None  # unknown — safe fallback


def _block_changed(node, changed_ranges) -> bool:
    """True if the block's byte range overlaps any changed range."""
    for r in changed_ranges:
        if r.start_byte < node.end_byte and r.end_byte > node.start_byte:
            return True
    return False


def _find_build_blocks(tree, source: bytes) -> list[tuple[str, object]]:
    """Return [(var_name, node)] for every top-level build context block."""
    result  = []
    matches = QueryCursor(_BUILD_BLOCK_QUERY).matches(tree.root_node)
    for _, caps in matches:
        ctx_node   = caps.get("ctx",   [])[0]
        var_node   = caps.get("var",   [])[0]
        block_node = caps.get("block", [])[0]
        if ctx_node.text.decode() not in _BUILD_CTXS:
            continue
        if block_node.parent is None or block_node.parent.type != "module":
            continue
        result.append((var_node.text.decode(), block_node))
    return result


def _referenced_names(node) -> set[str]:
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
    local_dir = os.path.dirname(filepath)
    caps      = QueryCursor(_IMPORT_QUERY).captures(tree.root_node)
    deps: set[str] = set()
    for node in caps.get("module", []):
        mod_name  = node.text.decode().split(".")[0]
        candidate = os.path.abspath(os.path.join(local_dir, mod_name + ".py"))
        if os.path.exists(candidate) and candidate != filepath:
            deps.add(candidate)
    _dep_graph[filepath] = deps


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

def _load_actors(filepath: str,
                 source: bytes | None = None) -> list:
    """Incremental reload driven by Tree-sitter.

    source — pre-loaded bytes (e.g. from LSP didChange).  When None the file
    is read from disk (watchfiles path).

    Uses tree.edit() + raw byte range to identify exactly which blocks changed.
    Collects LSP Diagnostic objects into _file_diagnostics[filepath].
    """
    diags: list = []
    _file_diagnostics[filepath] = diags

    if source is None:
        try:
            source = open(filepath, "rb").read()
        except OSError as exc:
            print(f"[b3d] Cannot read {filepath}: {exc}")
            cache = _file_cache.get(filepath, {})
            return [a for a, _ in cache.values()]

    old_tree   = _file_trees.get(filepath)
    old_source = _file_sources.get(filepath, b"")

    # Annotate the old tree with the edit so tree-sitter can reuse unchanged
    # subtrees during incremental parsing.
    changed_ranges = []
    if old_tree and old_source:
        s, oe, ne = _compute_edit(old_source, source)
        old_tree.edit(
            start_byte    = s,  old_end_byte    = oe,  new_end_byte    = ne,
            start_point   = _byte_to_point(old_source, s),
            old_end_point = _byte_to_point(old_source, oe),
            new_end_point = _byte_to_point(source,     ne),
        )
        # Use the raw byte edit region for block invalidation.
        # tree.changed_ranges() only detects structural AST changes and misses
        # value-only edits (e.g. changing a hex literal) where the node types
        # and positions are identical.  The raw (s, ne) range is always correct.
        if s != oe or s != ne:
            changed_ranges = [_ByteRange(s, ne)]

    new_tree = _ts_parser.parse(source, old_tree) if old_tree else _ts_parser.parse(source)

    _file_trees[filepath]   = new_tree
    _file_sources[filepath] = source
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
    post_meta_byte_ranges: list[tuple[int, int]] = []

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
            post_meta_byte_ranges.append((child.start_byte, child.end_byte))
        else:
            post_geom.append(child)

    post_refs: set[str] = set()
    for node in post_geom:
        post_refs |= _referenced_names(node) & block_var_names

    # Determine which pre-part names changed (if any).
    # None  → unknowable (star import, complex pattern) → re-exec all blocks.
    # set() → no pre-part change outside blocks.
    # {"x"} → only blocks referencing "x" need re-execution.
    changed_pre_part_vars: set[str] | None = set()

    if changed_ranges:
        for r in changed_ranges:
            in_block = any(
                r.start_byte < bn.end_byte and r.end_byte > bn.start_byte
                for _, bn in build_blocks
            )
            if not in_block:
                in_meta = any(
                    r.start_byte < me and r.end_byte > ms
                    for ms, me in post_meta_byte_ranges
                )
                if not in_meta:
                    # Find the top-level node that owns this change
                    for child in new_tree.root_node.children:
                        if (r.start_byte < child.end_byte
                                and r.end_byte > child.start_byte):
                            names = _defined_names(child)
                            if names is None:
                                changed_pre_part_vars = None  # fallback
                            elif isinstance(changed_pre_part_vars, set):
                                changed_pre_part_vars.update(names)
                            break
            if changed_pre_part_vars is None:
                break

    # ── Execute pre-nodes ────────────────────────────────────────────────────
    base_ns: dict = {}
    _stub_show_modules()
    try:
        exec(compile("\n".join(pre_parts), filepath, "exec"), base_ns)  # noqa: S102
    except Exception as exc:
        print(f"[b3d] Setup error: {exc}")
        _unstub_show_modules()
        cache = _file_cache.get(filepath, {})
        return [a for a, _ in cache.values()]
    finally:
        _unstub_show_modules()

    cache      = _file_cache.setdefault(filepath, {})
    actors:    list                = []
    new_cache: dict[str, tuple] = {}
    live_objs: dict[str, object]                 = {}

    # ── Process build blocks ─────────────────────────────────────────────────
    for var_name, node in build_blocks:
        # Syntax error in this block — keep last good actor
        if node.has_error:
            diags.append(_lsp_diag(
                node.start_point[0], node.end_point[0],
                f"Syntax error in block '{var_name}'",
            ))
            if var_name in cache:
                actors.append(cache[var_name][0])
                new_cache[var_name] = cache[var_name]
            continue

        needs_live = var_name in post_refs
        if changed_pre_part_vars is None:
            pre_stale = True
        else:
            pre_stale = bool(_referenced_names(node) & changed_pre_part_vars)
        changed = (not old_tree) or pre_stale or _block_changed(node, changed_ranges)

        if not needs_live and not changed and var_name in cache:
            actor, obj = cache[var_name]
            actors.append(actor)
            new_cache[var_name] = (actor, obj)
            continue

        block_src = source[node.start_byte:node.end_byte].decode()
        ns        = dict(base_ns)
        try:
            code = _compile_block(block_src, filepath, node.start_point[0])
            exec(code, ns)  # noqa: S102
        except Exception as exc:
            print(f"[b3d] Block '{var_name}' error: {exc}")
            line = _exc_line(exc, filepath)
            diags.append(_lsp_diag(line, line, f"Block '{var_name}': {exc}"))
            if var_name in cache:
                actors.append(cache[var_name][0])
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
            new_cache[var_name] = (actor, obj)
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
                actor = _shape_to_actor(shape)
                actors.append(actor)
                new_cache[var_name] = (actor, post_ns[var_name])
            except Exception as exc:
                print(f"[b3d] Tessellation error in '{var_name}': {exc}")

    # ── Apply metadata-only updates to cached actors ─────────────────────────
    for var_name, attr_name, value_src in post_meta:
        entry = new_cache.get(var_name)
        if entry is None:
            continue
        actor, _ = entry
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


# ── LSP helpers ───────────────────────────────────────────────────────────────

def _exc_line(exc: Exception, filepath: str) -> int:
    """0-indexed line of the innermost frame that belongs to filepath."""
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        if frame.filename == filepath:
            return max(0, frame.lineno - 1)
    return 0


def _lsp_diag(start_line: int, end_line: int, msg: str):
    from lsprotocol.types import Diagnostic, DiagnosticSeverity, Range, Position
    return Diagnostic(
        range=Range(
            start=Position(line=start_line, character=0),
            end=Position(line=end_line,   character=10_000),
        ),
        message=msg,
        severity=DiagnosticSeverity.Error,
        source="b3d-live",
    )


def _uri_to_abspath(uri: str) -> str:
    return os.path.abspath(urllib.parse.unquote(uri.removeprefix("file://")))


def _push_scene(all_actors: list, n_files: int) -> None:
    """Replace all VTK actors and push to the browser."""
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


def _build_lsp(filepaths: list[str]):
    """Create the pygls LanguageServer that drives live reload from editor events."""
    from pygls.lsp.server import LanguageServer
    from lsprotocol.types import (
        TEXT_DOCUMENT_DID_OPEN, TEXT_DOCUMENT_DID_CHANGE, TEXT_DOCUMENT_DID_SAVE,
        DidOpenTextDocumentParams, DidChangeTextDocumentParams,
        DidSaveTextDocumentParams,
    )

    watched = {os.path.abspath(fp) for fp in filepaths}
    _DEBOUNCE = 0.3   # seconds

    b3d = LanguageServer("b3d-live", "v0.2")

    async def _debounced_reload(ls, fp: str, source: bytes) -> None:
        await asyncio.sleep(_DEBOUNCE)
        loop = asyncio.get_running_loop()
        actors = await loop.run_in_executor(None, _load_actors, fp, source)
        all_actors = []
        for p in filepaths:
            p_abs = os.path.abspath(p)
            if p_abs == fp:
                all_actors.extend(actors)
            else:
                all_actors.extend(a for a, _ in _file_cache.get(p_abs, {}).values())
        _push_scene(all_actors, len(filepaths))
        uri = pathlib.Path(fp).as_uri()
        ls.publish_diagnostics(uri, _file_diagnostics.get(fp, []))

    def _trigger(ls, fp: str, source: bytes) -> None:
        t = _lsp_debounce_tasks.get(fp)
        if t:
            t.cancel()
        _lsp_debounce_tasks[fp] = asyncio.ensure_future(
            _debounced_reload(ls, fp, source)
        )

    @b3d.feature(TEXT_DOCUMENT_DID_OPEN)
    def did_open(ls, params: DidOpenTextDocumentParams) -> None:
        fp = _uri_to_abspath(params.text_document.uri)
        if fp in watched:
            _trigger(ls, fp, params.text_document.text.encode())

    @b3d.feature(TEXT_DOCUMENT_DID_CHANGE)
    def did_change(ls, params: DidChangeTextDocumentParams) -> None:
        fp = _uri_to_abspath(params.text_document.uri)
        if fp in watched:
            source = params.content_changes[-1].text.encode()
            _trigger(ls, fp, source)

    @b3d.feature(TEXT_DOCUMENT_DID_SAVE)
    def did_save(ls, params: DidSaveTextDocumentParams) -> None:
        fp = _uri_to_abspath(params.text_document.uri)
        if fp in watched:
            try:
                source = open(fp, "rb").read()
            except OSError:
                return
            _trigger(ls, fp, source)

    return b3d


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

            to_reload: set[str] = set()
            for fp in filepaths:
                fp_abs = os.path.abspath(fp)
                if fp_abs in changed_abs:
                    to_reload.add(fp)
                elif changed_abs & _dep_graph.get(fp_abs, set()):
                    # Drop tree so next parse has no prior state
                    _file_trees.pop(fp_abs, None)
                    _file_sources.pop(fp_abs, None)
                    to_reload.add(fp)

            if not to_reload:
                continue

            for p in changed_abs:
                _invalidate_local_modules(os.path.dirname(p))

            all_actors: list = []
            for fp in filepaths:
                if fp in to_reload:
                    actors = await loop.run_in_executor(None, _load_actors, fp)
                else:
                    cache  = _file_cache.get(fp, {})
                    actors = [a for a, _ in cache.values()]
                all_actors.extend(actors)

            if not all_actors:
                continue

            _push_scene(all_actors, len(filepaths))
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
    global vtk, awatch, Change, get_server, SinglePageLayout, vuetify3, vtklocal
    global Language, Parser, Query, QueryCursor
    global _PY_LANGUAGE, _ts_parser, _BUILD_BLOCK_QUERY, _IMPORT_QUERY

    import vtk
    from tree_sitter import Language, Parser, Query, QueryCursor
    import tree_sitter_python
    from trame.app import get_server
    from trame.ui.vuetify3 import SinglePageLayout
    from trame.widgets import vuetify3, vtklocal
    from watchfiles import awatch, Change

    _PY_LANGUAGE = Language(tree_sitter_python.language())
    _ts_parser   = Parser(_PY_LANGUAGE)
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

    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", default=["main.py"])
    parser.add_argument("--port",     type=int, default=1234)
    parser.add_argument("--lsp-port", type=int, default=None,
                        help="Start LSP server on this TCP port (e.g. 2087). "
                             "Enables live reload from editor on every keystroke.")
    args = parser.parse_args()

    filepaths = [os.path.abspath(f) for f in args.files]
    _server   = get_server(client_type="vue3")

    _setup_vtk()

    all_actors: list = []
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
        if args.lsp_port:
            lsp = _build_lsp(filepaths)
            asyncio.create_task(lsp.start_tcp("127.0.0.1", args.lsp_port))
            print(f"[b3d] LSP       : 127.0.0.1:{args.lsp_port}")
        await _server.start(exec_mode="task", open_browser=False, port=args.port)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


# ── stdio relay (for editors that start LSP servers as subprocesses) ──────────

def lsp_relay():
    """Relay stdin/stdout to a running b3d-live TCP LSP server.

    Helix, neovim, and other editors that start language servers as subprocesses
    communicate over stdio.  Run this as the editor's language server command and
    point it at the TCP port opened by `b3d-live --lsp-port PORT`.

    Usage:
        b3d-lsp [--port PORT]   (default: 2087)
    """
    import socket
    import threading

    parser = argparse.ArgumentParser(description="Relay stdio ↔ b3d-live LSP server")
    parser.add_argument("--port", type=int, default=2087)
    args = parser.parse_args()

    try:
        sock = socket.create_connection(("127.0.0.1", args.port), timeout=5)
    except (ConnectionRefusedError, TimeoutError):
        sys.stderr.write(
            f"[b3d-lsp] Cannot connect to 127.0.0.1:{args.port}\n"
            f"[b3d-lsp] Start b3d-live first:  b3d-live body.py --lsp-port {args.port}\n"
        )
        sys.exit(1)

    def _stdin_to_sock() -> None:
        try:
            while chunk := sys.stdin.buffer.read(4096):
                sock.sendall(chunk)
        except Exception:
            pass
        finally:
            sock.shutdown(socket.SHUT_WR)

    threading.Thread(target=_stdin_to_sock, daemon=True).start()

    try:
        while chunk := sock.recv(4096):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    except Exception:
        pass


if __name__ == "__main__":
    main()
