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
_file_diagnostics: dict[str, list] = {}   # filepath -> lsprotocol Diagnostic list
_lsp_gen:          dict[str, int]  = {}   # filepath -> debounce generation counter

# Per-block status for the UI panel
# filepath -> {var_name -> "cached" | "rebuilt" | "error"}
_block_status: dict[str, dict[str, str]] = {}


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
                 source: bytes | None = None) -> tuple[list, int]:
    """Incremental reload driven by Tree-sitter.

    source — pre-loaded bytes (e.g. from LSP didChange).  When None the file
    is read from disk (watchfiles path).

    Uses tree.edit() + raw byte range to identify exactly which blocks changed.
    Collects LSP Diagnostic objects into _file_diagnostics[filepath].
    """
    diags: list = []
    _file_diagnostics[filepath] = diags
    hits = 0

    if source is None:
        try:
            source = open(filepath, "rb").read()
        except OSError as exc:
            print(f"[b3d] Cannot read {filepath}: {exc}")
            cache = _file_cache.get(filepath, {})
            return [a for a, _ in cache.values()], 0

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
        return [], 0

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
        return [a for a, _ in cache.values()], 0
    finally:
        _unstub_show_modules()

    cache           = _file_cache.setdefault(filepath, {})
    actors:    list                = []
    new_cache: dict[str, tuple] = {}
    live_objs: dict[str, object]                 = {}
    block_statuses: dict[str, str]               = {}
    # Accumulated block objects so later blocks can reference earlier ones
    # (e.g. cab2 referencing cab inside its own body)
    prior_objs: dict[str, object]                = {}

    # ── Process build blocks ─────────────────────────────────────────────────
    for var_name, node in build_blocks:
        # Syntax error in this block — keep last good actor
        if node.has_error:
            diags.append(_lsp_diag(
                node.start_point[0], node.end_point[0],
                f"Syntax error in block '{var_name}'",
            ))
            if var_name in cache:
                actor, obj = cache[var_name]
                actors.append(actor)
                new_cache[var_name] = cache[var_name]
                prior_objs[var_name] = obj
            block_statuses[var_name] = "error"
            continue

        needs_live = var_name in post_refs
        if changed_pre_part_vars is None:
            pre_stale = True
        else:
            pre_stale = bool(_referenced_names(node) & changed_pre_part_vars)
        # Also stale if any block it references was rebuilt/errored this pass
        _cached_so_far = {n for n, s in block_statuses.items() if s == "cached"}
        _other_deps = (_referenced_names(node) & block_var_names) - {var_name}
        dep_stale = bool(_other_deps - _cached_so_far)
        changed = (not old_tree) or pre_stale or dep_stale or _block_changed(node, changed_ranges)

        if not needs_live and not changed and var_name in cache:
            actor, obj = cache[var_name]
            actors.append(actor)
            new_cache[var_name] = (actor, obj)
            prior_objs[var_name] = obj
            block_statuses[var_name] = "cached"
            hits += 1
            continue

        block_src = source[node.start_byte:node.end_byte].decode()
        ns        = {**base_ns, **prior_objs}
        try:
            code = _compile_block(block_src, filepath, node.start_point[0])
            exec(code, ns)  # noqa: S102
        except Exception as exc:
            print(f"[b3d] Block '{var_name}' error: {exc}")
            line = _exc_line(exc, filepath)
            diags.append(_lsp_diag(line, line, f"Block '{var_name}': {exc}"))
            if var_name in cache:
                actor, obj = cache[var_name]
                actors.append(actor)
                new_cache[var_name] = cache[var_name]
                prior_objs[var_name] = obj
            block_statuses[var_name] = "error"
            continue

        obj = ns.get(var_name)
        prior_objs[var_name] = obj
        if needs_live:
            live_objs[var_name] = obj
            continue

        shape = _extract_shape(obj)
        if shape is None:
            print(f"[b3d] Block '{var_name}': no shape captured")
            block_statuses[var_name] = "error"
            continue
        try:
            actor = _shape_to_actor(shape)
            actors.append(actor)
            new_cache[var_name] = (actor, obj)
            block_statuses[var_name] = "rebuilt"
        except Exception as exc:
            print(f"[b3d] Tessellation error in '{var_name}': {exc}")
            block_statuses[var_name] = "error"

    # ── Run geometry-affecting post-nodes with live objects ──────────────────
    if post_geom and live_objs:
        post_ns  = {**base_ns, **prior_objs, **live_objs}
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
                block_statuses[var_name] = "error"
                continue
            try:
                actor = _shape_to_actor(shape)
                actors.append(actor)
                new_cache[var_name] = (actor, post_ns[var_name])
                block_statuses[var_name] = "rebuilt"
            except Exception as exc:
                print(f"[b3d] Tessellation error in '{var_name}': {exc}")
                block_statuses[var_name] = "error"

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
    # Fill any blocks that didn't reach a status (e.g. needs_live with no shape)
    for var_name, _ in build_blocks:
        block_statuses.setdefault(var_name, "error")
    _block_status[filepath] = block_statuses
    return actors, hits


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


_STATUS_COLOR = {"cached": "success", "rebuilt": "primary", "error": "error"}


def _build_ast_tree_data() -> list:
    """Build file → block tree for VTreeview.

    Each block node carries only: name, line number, status, visibility.
    Falls back to the bare cache list on SyntaxError.
    """
    all_paths = dict.fromkeys(list(_file_sources.keys()) + list(_block_status.keys()))
    groups = []

    for filepath in all_paths:
        fname    = os.path.basename(filepath)
        source   = _file_sources.get(filepath, b"")
        cache    = _file_cache.get(filepath, {})
        statuses = _block_status.get(filepath, {})

        blocks: list = []
        try:
            tree = ast.parse(source)
            for stmt in tree.body:
                if not isinstance(stmt, ast.With):
                    continue
                for item in stmt.items:
                    if not isinstance(item.context_expr, ast.Call):
                        continue
                    func = item.context_expr.func
                    ctx  = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                    if ctx not in _BUILD_CTXS:
                        continue
                    as_name = (item.optional_vars.id
                               if isinstance(item.optional_vars, ast.Name) else None)
                    if not as_name:
                        continue
                    status = statuses.get(as_name, "rebuilt")
                    entry  = cache.get(as_name)
                    blocks.append({
                        "id":        f"{filepath}::{as_name}",
                        "label":     f"{as_name}  · L{stmt.lineno}",
                        "kind":      "block",
                        "status":    status,
                        "visible":   bool(entry[0].GetVisibility()) if entry else False,
                        "has_actor": entry is not None,
                        "color":     _STATUS_COLOR.get(status, "primary"),
                        "children":  [],
                    })
                    break
        except SyntaxError:
            for var_name, (actor, _) in cache.items():
                status = statuses.get(var_name, "rebuilt")
                blocks.append({
                    "id":        f"{filepath}::{var_name}",
                    "label":     var_name,
                    "kind":      "block",
                    "status":    status,
                    "visible":   bool(actor.GetVisibility()),
                    "has_actor": True,
                    "color":     _STATUS_COLOR.get(status, "primary"),
                    "children":  [],
                })

        if blocks:
            groups.append({
                "id":       f"file::{filepath}",
                "label":    fname,
                "kind":     "file",
                "color":    "",
                "children": blocks,
            })

    return groups


def _push_scene(all_actors: list, hits: int) -> None:
    """Replace all VTK actors and push to the browser."""
    _renderer.RemoveAllViewProps()
    for actor in all_actors:
        _renderer.AddActor(actor)
    _renderer.ResetCamera()
    _render_window.Render()
    if _view is not None:
        _view.update()
    print(f"[b3d] {len(all_actors)} shape(s), {hits} cached")
    if _server is not None:
        with _server.state:
            _server.state.shape_count = len(all_actors)
            _server.state.ast_tree    = _build_ast_tree_data()


def _build_lsp(filepaths: list[str], main_loop: asyncio.AbstractEventLoop):
    """Create the pygls LanguageServer that drives live reload from editor events."""
    from pygls.lsp.server import LanguageServer
    from lsprotocol.types import (
        TEXT_DOCUMENT_DID_OPEN, TEXT_DOCUMENT_DID_CHANGE, TEXT_DOCUMENT_DID_SAVE,
        DidOpenTextDocumentParams, DidChangeTextDocumentParams,
        DidSaveTextDocumentParams, TextDocumentSyncKind,
    )

    # mutable: grows as the editor opens new .py files
    watched: set[str] = {os.path.abspath(fp) for fp in filepaths}
    _DEBOUNCE = 0.3   # seconds
    _lsp_loop: list[asyncio.AbstractEventLoop] = []  # captured on first handler call

    b3d = LanguageServer("b3d-live", "v0.2",
                         text_document_sync_kind=TextDocumentSyncKind.Full)

    async def _debounced_reload(ls, fp: str, source: bytes, gen: int) -> None:
        await asyncio.sleep(_DEBOUNCE)
        if _lsp_gen.get(fp) != gen:   # superseded by a newer edit
            return
        actors, hits = await main_loop.run_in_executor(None, _load_actors, fp, source)
        all_actors = []
        for p_abs in list(watched):
            if p_abs == fp:
                all_actors.extend(actors)
            else:
                all_actors.extend(a for a, _ in _file_cache.get(p_abs, {}).values())
        _push_scene(all_actors, hits)
        # publish_diagnostics must run on the pygls event loop, not main_loop
        uri = pathlib.Path(fp).as_uri()
        diags = _file_diagnostics.get(fp, [])
        if _lsp_loop:
            _lsp_loop[0].call_soon_threadsafe(ls.publish_diagnostics, uri, diags)

    def _trigger(ls, fp: str, source: bytes) -> None:
        gen = _lsp_gen.get(fp, 0) + 1
        _lsp_gen[fp] = gen
        asyncio.run_coroutine_threadsafe(
            _debounced_reload(ls, fp, source, gen), main_loop
        )

    @b3d.feature(TEXT_DOCUMENT_DID_OPEN)
    def did_open(ls, params: DidOpenTextDocumentParams) -> None:
        if not _lsp_loop:
            _lsp_loop.append(asyncio.get_running_loop())
        fp = _uri_to_abspath(params.text_document.uri)
        if fp.endswith(".py") and os.path.basename(fp) not in _VIEWER_FILES:
            watched.add(fp)   # auto-watch any .py file the editor opens
        if fp in watched:
            _trigger(ls, fp, params.text_document.text.encode())

    @b3d.feature(TEXT_DOCUMENT_DID_CHANGE)
    def did_change(ls, params: DidChangeTextDocumentParams) -> None:
        if not _lsp_loop:
            _lsp_loop.append(asyncio.get_running_loop())
        fp = _uri_to_abspath(params.text_document.uri)
        if fp in watched:
            source = params.content_changes[-1].text.encode()
            _trigger(ls, fp, source)

    @b3d.feature(TEXT_DOCUMENT_DID_SAVE)
    def did_save(ls, params: DidSaveTextDocumentParams) -> None:
        if not _lsp_loop:
            _lsp_loop.append(asyncio.get_running_loop())
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
            total_hits = 0
            for fp in filepaths:
                if fp in to_reload:
                    actors, hits = await loop.run_in_executor(None, _load_actors, fp)
                    total_hits += hits
                else:
                    cache  = _file_cache.get(fp, {})
                    actors = [a for a, _ in cache.values()]
                    total_hits += len(actors)
                all_actors.extend(actors)

            if not all_actors:
                continue

            _push_scene(all_actors, total_hits)
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

    state.shape_count    = initial_count
    state.ast_tree       = _build_ast_tree_data()
    state.panel_open     = False
    state.panel_width    = 260
    state.activated_node = []
    state.wireframe   = False
    state.dark_bg     = True
    state.parallel    = False
    state.edges       = False

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

    @state.change("activated_node")
    def _on_activate(activated_node, **kwargs):
        if not activated_node:
            return
        node_id = activated_node[0]
        # Block node ids are filepath + "::" + var_name with no further "::"
        # Find by checking each known filepath as prefix
        entry = None
        for fp in _file_cache:
            prefix = fp + "::"
            if node_id.startswith(prefix):
                tail = node_id[len(prefix):]
                if "::" not in tail:          # var_name has no "::"
                    entry = _file_cache[fp].get(tail)
                    if entry:
                        break
        if entry:
            actor, _ = entry
            actor.SetVisibility(int(not bool(actor.GetVisibility())))
            _render_window.Render()
            if _view is not None:
                _view.update()
        with state:
            state.activated_node = []
            state.ast_tree       = _build_ast_tree_data()

    def toggle_projection():
        state.parallel = not state.parallel
        cam = _renderer.GetActiveCamera()
        if state.parallel:
            cam.ParallelProjectionOn()
        else:
            cam.ParallelProjectionOff()
        _render_window.Render()
        if _view is not None:
            _view.update()

    def toggle_edges():
        state.edges = not state.edges
        col = _renderer.GetActors()
        col.InitTraversal()
        actor = col.GetNextActor()
        while actor:
            actor.GetProperty().SetEdgeVisibility(int(state.edges))
            actor = col.GetNextActor()
        _render_window.Render()
        if _view is not None:
            _view.update()

    ctrl.reset_camera      = reset_camera
    ctrl.toggle_wireframe  = toggle_wireframe
    ctrl.toggle_background = toggle_background
    ctrl.toggle_projection = toggle_projection
    ctrl.toggle_edges      = toggle_edges
    ctrl.view_x    = lambda: _set_axis_view(( 1,  0,  0), (0, 0, 1))
    ctrl.view_nx   = lambda: _set_axis_view((-1,  0,  0), (0, 0, 1))
    ctrl.view_y    = lambda: _set_axis_view(( 0, -1,  0), (0, 0, 1))
    ctrl.view_ny   = lambda: _set_axis_view(( 0,  1,  0), (0, 0, 1))
    ctrl.view_z    = lambda: _set_axis_view(( 0,  0,  1), (0, 1, 0))
    ctrl.view_nz   = lambda: _set_axis_view(( 0,  0, -1), (0, 1, 0))
    ctrl.view_iso  = lambda: _set_axis_view(( 1, -1,  1), (0, 0, 1))

    _btn = dict(variant="text", density="compact", size="small")

    with SinglePageLayout(server) as layout:
        layout.title.set_text("build123d")

        layout.root.add_child("""<style id="b3d-panel-styles">
/* ── Navigation drawer: glass-morphism ── */
.b3d-drawer {
  background: rgba(15, 15, 20, 0.97) !important;
  border-right: 1px solid rgba(255,255,255,0.07) !important;
  box-shadow: 6px 0 32px rgba(0,0,0,0.5) !important;
}

/* ── File group header ── */
.b3d-file-header {
  letter-spacing: 0.09em !important;
  opacity: 0.55;
  font-size: 0.68rem !important;
}

/* ── Block items: smooth colour transitions ── */
.b3d-item {
  cursor: pointer;
  transition: background-color 0.25s ease,
              color 0.25s ease,
              opacity 0.2s ease !important;
}
.b3d-item:hover {
  background: rgba(255,255,255,0.06) !important;
}

/* ── "Rebuilt" flash on status change ── */
@keyframes b3d-flash {
  0%   { box-shadow: inset 0 0 0 1px rgba(var(--v-theme-primary), 0.9); }
  100% { box-shadow: inset 0 0 0 1px transparent; }
}
.b3d-item-rebuilt {
  animation: b3d-flash 0.7s ease-out;
}

/* ── Error status: subtle red left border ── */
.b3d-item-error {
  border-left: 2px solid rgba(var(--v-theme-error), 0.7) !important;
}

/* ── Resize handle: highlight on hover ── */
.b3d-resize {
  transition: background-color 0.15s ease !important;
}
.b3d-resize:hover {
  background-color: rgba(255,255,255,0.18) !important;
}

/* ── Shape count header ── */
.b3d-shape-count {
  letter-spacing: 0.08em !important;
  opacity: 0.8;
}
</style>""")

        with layout.toolbar as tb:
            tb.density = "compact"

            vuetify3.VSpacer()

            # ── Display mode ─────────────────────────────────────────────────
            vuetify3.VBtn(
                icon="mdi-vector-square", title="Wireframe",
                click=ctrl.toggle_wireframe,
                color=("wireframe ? 'primary' : ''",),
                **_btn,
            )
            vuetify3.VBtn(
                icon="mdi-border-all-variant", title="Show edges",
                click=ctrl.toggle_edges,
                color=("edges ? 'primary' : ''",),
                **_btn,
            )
            vuetify3.VBtn(
                icon="mdi-perspective-less", title="Parallel projection",
                click=ctrl.toggle_projection,
                color=("parallel ? 'primary' : ''",),
                **_btn,
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

            # ── Named views ──────────────────────────────────────────────────
            vuetify3.VBtn("X",  title="Right view (+X)",  click=ctrl.view_x,  **_btn)
            vuetify3.VBtn("-X", title="Left view (-X)",   click=ctrl.view_nx, **_btn)
            vuetify3.VBtn("Y",  title="Front view (+Y)",  click=ctrl.view_y,  **_btn)
            vuetify3.VBtn("-Y", title="Back view (-Y)",   click=ctrl.view_ny, **_btn)
            vuetify3.VBtn("Z",  title="Top view (+Z)",    click=ctrl.view_z,  **_btn)
            vuetify3.VBtn("-Z", title="Bottom view (-Z)", click=ctrl.view_nz, **_btn)
            vuetify3.VBtn(
                icon="mdi-axis-arrow", title="Isometric",
                click=ctrl.view_iso, **_btn,
            )

            vuetify3.VSpacer()

        # Hamburger button opens the left panel
        layout.icon.click = "panel_open = !panel_open"

        with vuetify3.VNavigationDrawer(
            v_model=("panel_open", False),
            location="left",
            width=("panel_width", 260),
            classes="b3d-drawer",
        ):
            # ── Drag-resize handle on the right edge ─────────────────────────
            vuetify3.VSheet(
                classes="b3d-resize",
                style=(
                    "position:absolute;right:0;top:0;bottom:0;width:5px;"
                    "cursor:col-resize;z-index:10;"
                ),
                mousedown=(
                    "(e => {"
                    " const sx=e.clientX, sw=panel_width;"
                    " const mv=e=>{ panel_width=Math.max(180,Math.min(600,sw+(e.clientX-sx))); };"
                    " window.addEventListener('mousemove',mv);"
                    " window.addEventListener('mouseup',"
                    "  ()=>window.removeEventListener('mousemove',mv),{once:true});"
                    "})($event)"
                ),
            )
            # ── Header ───────────────────────────────────────────────────────
            with vuetify3.VList(density="compact"):
                vuetify3.VListSubheader(
                    "{{ shape_count }} shape(s)",
                    classes="text-caption font-weight-bold text-uppercase b3d-shape-count",
                )
            vuetify3.VDivider()
            # ── File groups ──────────────────────────────────────────────────
            with vuetify3.VList(density="compact", nav=True, classes="pa-2"):
                with vuetify3.Template(
                    v_for="group in ast_tree",
                    key=("group.id",),
                ):
                    vuetify3.VListSubheader(
                        "{{ group.label }}",
                        classes="text-caption font-weight-bold text-uppercase b3d-file-header mt-1",
                    )
                    vuetify3.VListItem(
                        v_for="block in group.children",
                        key=("block.id",),
                        prepend_icon=(
                            "block.has_actor && !block.visible ? 'mdi-eye-off-outline'"
                            " : block.status === 'error'   ? 'mdi-alert-circle-outline'"
                            " : block.status === 'cached'  ? 'mdi-check-circle-outline'"
                            " : 'mdi-refresh'",
                        ),
                        append_icon=(
                            "block.has_actor"
                            " ? (block.visible ? 'mdi-eye-outline' : 'mdi-eye-off-outline')"
                            " : ''",
                        ),
                        title=("block.label",),
                        base_color=("block.color",),
                        rounded="lg",
                        classes=(
                            "'mb-1 b3d-item'"
                            " + (block.status === 'rebuilt' ? ' b3d-item-rebuilt' : '')"
                            " + (block.status === 'error'   ? ' b3d-item-error'   : '')",
                        ),
                        click="activated_node = [block.id]",
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
        actors, _ = _load_actors(filepath)
        all_actors.extend(actors)
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
            import time as _time
            main_loop = asyncio.get_running_loop()
            def _lsp_thread():
                while True:
                    try:
                        _build_lsp(filepaths, main_loop).start_tcp(
                            "127.0.0.1", args.lsp_port
                        )
                    except Exception:
                        traceback.print_exc()
                    _time.sleep(0.5)

            threading.Thread(target=_lsp_thread, daemon=True).start()
            print(f"[b3d] LSP       : 127.0.0.1:{args.lsp_port}")
        await _server.start(exec_mode="task", open_browser=False, port=args.port)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


# ── Combined viewer + stdio LSP (editor-as-launcher mode) ────────────────────

def lsp_main():
    """Self-contained entry point: starts the viewer AND serves LSP on stdio.

    The editor runs this as its language server.  No separate b3d-live process
    needed — the viewer opens automatically in the browser.

    Helix config example:
        [language-server.b3d-live]
        command = "/path/to/.venv/bin/b3d-lsp"
        args    = ["body.py", "cab.py"]
    """
    global _server
    global vtk, awatch, Change, get_server, SinglePageLayout, vuetify3, vtklocal
    global Language, Parser, Query, QueryCursor
    global _PY_LANGUAGE, _ts_parser, _BUILD_BLOCK_QUERY, _IMPORT_QUERY

    # Steal the real stdout before anything can pollute it — LSP uses raw bytes
    # on stdout.  All print() calls from here on go to stderr (editor log).
    _lsp_out   = sys.stdout.buffer
    sys.stdout = sys.stderr

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
    parser.add_argument("files", nargs="*", default=[])
    parser.add_argument("--port", type=int, default=1234)
    args = parser.parse_args()

    filepaths = [os.path.abspath(f) for f in args.files]
    _server = get_server(client_type="vue3")

    _setup_vtk()

    all_actors: list = []
    for filepath in filepaths:
        actors, _ = _load_actors(filepath)
        all_actors.extend(actors)
    for actor in all_actors:
        _renderer.AddActor(actor)
    if all_actors:
        _renderer.ResetCamera()
        _render_window.Render()
        print(f"[b3d] {len(all_actors)} shape(s) loaded from {len(filepaths)} file(s)")

    _build_ui(_server, filepaths, initial_count=len(all_actors))

    watching = ', '.join(args.files) if args.files else "any .py file opened in editor"
    print(f"[b3d] Watching  : {watching}")
    print(f"[b3d] Browser   : http://localhost:{args.port}")
    print(f"[b3d] LSP       : stdio")

    async def _run():
        if filepaths:
            asyncio.create_task(_watch_and_reload(filepaths))
        main_loop = asyncio.get_running_loop()

        def _lsp_thread():
            try:
                _build_lsp(filepaths, main_loop).start_io(
                    stdin=sys.stdin.buffer, stdout=_lsp_out
                )
            except Exception:
                traceback.print_exc()

        threading.Thread(target=_lsp_thread, daemon=True).start()

        def _open_browser():
            import time, webbrowser
            time.sleep(2)
            webbrowser.open(f"http://localhost:{args.port}")
        threading.Thread(target=_open_browser, daemon=True).start()

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

    import time
    _RETRY_SECS = 30
    sock = None
    deadline = time.monotonic() + _RETRY_SECS
    while time.monotonic() < deadline:
        try:
            sock = socket.create_connection(("127.0.0.1", args.port), timeout=1)
            break
        except (ConnectionRefusedError, TimeoutError, OSError):
            time.sleep(1)
    if sock is None:
        sys.stderr.write(
            f"[b3d-lsp] Cannot connect to 127.0.0.1:{args.port} after {_RETRY_SECS}s\n"
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
    finally:
        os._exit(0)  # skip interpreter cleanup — daemon stdin thread holds buffered lock


if __name__ == "__main__":
    main()
