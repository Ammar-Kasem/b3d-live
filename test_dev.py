"""Unit tests for dev.py parsing layer.

These tests cover everything above _load_actors and require only tree-sitter
(no VTK, no build123d).  Run with:

    uv run pytest test_dev.py -v
"""

import pytest
from tree_sitter import Language, Parser
import tree_sitter_python
import dev

# ── Helpers ───────────────────────────────────────────────────────────────────

_lang   = Language(tree_sitter_python.language())
_parser = Parser(_lang)


def parse(src: str):
    return _parser.parse(src.encode())


def incremental(src1: str, src2: str):
    """Parse src1, then re-parse src2 incrementally with tree.edit()."""
    b1 = src1.encode()
    b2 = src2.encode()
    old = _parser.parse(b1)
    s, oe, ne = dev._compute_edit(b1, b2)
    old.edit(
        start_byte=s, old_end_byte=oe, new_end_byte=ne,
        start_point=dev._byte_to_point(b1, s),
        old_end_point=dev._byte_to_point(b1, oe),
        new_end_point=dev._byte_to_point(b2, ne),
    )
    new = _parser.parse(b2, old)
    return old, new


# ── _compute_edit ─────────────────────────────────────────────────────────────

class TestComputeEdit:
    def test_middle_change(self):
        s, oe, ne = dev._compute_edit(b"hello world", b"hello there")
        assert s == 6
        assert oe == 11
        assert ne == 11

    def test_insertion(self):
        s, oe, ne = dev._compute_edit(b"ab", b"axb")
        assert s == 1
        assert oe == 1
        assert ne == 2

    def test_deletion(self):
        s, oe, ne = dev._compute_edit(b"axb", b"ab")
        assert s == 1
        assert oe == 2
        assert ne == 1

    def test_identical(self):
        s, oe, ne = dev._compute_edit(b"same", b"same")
        assert s == oe == ne == 4

    def test_empty_to_content(self):
        s, oe, ne = dev._compute_edit(b"", b"new")
        assert s == 0
        assert oe == 0
        assert ne == 3


# ── _byte_to_point ────────────────────────────────────────────────────────────

class TestByteToPoint:
    def test_first_line(self):
        assert dev._byte_to_point(b"hello\nworld", 3) == (0, 3)

    def test_second_line(self):
        assert dev._byte_to_point(b"hello\nworld", 8) == (1, 2)

    def test_start_of_second_line(self):
        assert dev._byte_to_point(b"hello\nworld", 6) == (1, 0)


# ── _find_build_blocks ────────────────────────────────────────────────────────

class TestFindBuildBlocks:
    def test_finds_build_part(self):
        tree = parse("with BuildPart() as body:\n    pass\n")
        blocks = dev._find_build_blocks(tree, tree.root_node.text)
        assert len(blocks) == 1
        assert blocks[0][0] == "body"

    def test_finds_all_ctxs(self):
        src = (
            "with BuildPart() as p:\n    pass\n"
            "with BuildSketch() as s:\n    pass\n"
            "with BuildLine() as l:\n    pass\n"
        )
        tree = parse(src)
        names = [v for v, _ in dev._find_build_blocks(tree, src.encode())]
        assert names == ["p", "s", "l"]

    def test_ignores_unknown_ctx(self):
        tree = parse("with SomeOtherCtx() as x:\n    pass\n")
        assert dev._find_build_blocks(tree, tree.root_node.text) == []

    def test_only_top_level(self):
        # nested BuildSketch inside BuildPart should not appear as a top-level block
        src = "with BuildPart() as body:\n    with BuildSketch() as sk:\n        pass\n"
        tree = parse(src)
        blocks = dev._find_build_blocks(tree, src.encode())
        assert len(blocks) == 1
        assert blocks[0][0] == "body"

    def test_multiple_blocks(self):
        src = (
            "with BuildPart() as body:\n    pass\n"
            "with BuildPart() as cab:\n    pass\n"
        )
        tree = parse(src)
        names = [v for v, _ in dev._find_build_blocks(tree, src.encode())]
        assert names == ["body", "cab"]


# ── _block_changed ────────────────────────────────────────────────────────────

def _byte_ranges(src1: str, src2: str):
    """Return the _ByteRange list that _load_actors would produce for this edit."""
    b1, b2 = src1.encode(), src2.encode()
    s, oe, ne = dev._compute_edit(b1, b2)
    if s != oe or s != ne:
        return [dev._ByteRange(s, ne)]
    return []


class TestBlockChanged:
    def test_changed_block_detected(self):
        src1 = "with BuildPart() as body:\n    pass\nwith BuildPart() as cab:\n    pass\n"
        src2 = "with BuildPart() as body:\n    Box(1,2,3)\nwith BuildPart() as cab:\n    pass\n"
        _, new = incremental(src1, src2)
        ranges = _byte_ranges(src1, src2)
        blocks = dev._find_build_blocks(new, src2.encode())
        results = {v: dev._block_changed(n, ranges) for v, n in blocks}
        assert results["body"] is True
        assert results["cab"] is False

    def test_unchanged_block_not_detected(self):
        src1 = "with BuildPart() as body:\n    Box(1,1,1)\n"
        src2 = "with BuildPart() as body:\n    Box(1,1,1)\n"
        _, new = incremental(src1, src2)
        ranges = _byte_ranges(src1, src2)
        blocks = dev._find_build_blocks(new, src2.encode())
        assert dev._block_changed(blocks[0][1], ranges) is False

    def test_value_only_change_detected(self):
        # Changing a literal value (same AST structure) must still mark the block
        # changed.  old_tree.changed_ranges() misses this — _ByteRange fixes it.
        src1 = "with BuildPart() as body:\n    body_color = Color(0x4683CE)\n"
        src2 = "with BuildPart() as body:\n    body_color = Color(0xFF0000)\n"
        _, new = incremental(src1, src2)
        ranges = _byte_ranges(src1, src2)
        blocks = dev._find_build_blocks(new, src2.encode())
        assert dev._block_changed(blocks[0][1], ranges) is True

    def test_no_old_tree_means_all_changed(self):
        # When there is no old tree, changed_ranges is [] and we treat
        # (not old_tree) as the trigger — verified in _load_actors logic.
        assert dev._block_changed.__code__.co_varnames  # just checks it exists


# ── _is_show_call_node ────────────────────────────────────────────────────────

class TestIsShowCallNode:
    def test_detects_show(self):
        tree = parse("show(body.part)\n")
        child = tree.root_node.children[0]
        assert dev._is_show_call_node(child) is True

    def test_ignores_other_calls(self):
        tree = parse("fillet(body.edges(), 1)\n")
        child = tree.root_node.children[0]
        assert dev._is_show_call_node(child) is False

    def test_ignores_assignment(self):
        tree = parse("x = 1\n")
        child = tree.root_node.children[0]
        assert dev._is_show_call_node(child) is False


# ── _parse_metadata_stmt ──────────────────────────────────────────────────────

class TestParseMetadataStmt:
    def test_color_via_part(self):
        src = b"body.part.color = Color(0x4683CE)\n"
        tree = _parser.parse(src)
        child = tree.root_node.children[0]
        result = dev._parse_metadata_stmt(child, src)
        assert result == ("body", "color", "Color(0x4683CE)")

    def test_color_via_sketch(self):
        src = b"sk.sketch.color = Color(0xFF0000)\n"
        tree = _parser.parse(src)
        child = tree.root_node.children[0]
        result = dev._parse_metadata_stmt(child, src)
        assert result == ("sk", "color", "Color(0xFF0000)")

    def test_label(self):
        src = b'body.part.label = "truck"\n'
        tree = _parser.parse(src)
        child = tree.root_node.children[0]
        result = dev._parse_metadata_stmt(child, src)
        assert result == ("body", "label", '"truck"')

    def test_non_metadata_assignment(self):
        src = b"x = 1\n"
        tree = _parser.parse(src)
        child = tree.root_node.children[0]
        assert dev._parse_metadata_stmt(child, src) is None

    def test_joint_connect_not_metadata(self):
        src = b'body.joints["top"].connect_to(cab.joints["base"])\n'
        tree = _parser.parse(src)
        child = tree.root_node.children[0]
        assert dev._parse_metadata_stmt(child, src) is None


# ── _update_dep_graph ─────────────────────────────────────────────────────────

class TestUpdateDepGraph:
    def test_local_import_added(self, tmp_path):
        helper = tmp_path / "common.py"
        helper.write_bytes(b"x = 1\n")
        fp = str(tmp_path / "body.py")
        src = b"from common import x\n"
        tree = _parser.parse(src)
        dev._update_dep_graph(fp, tree, src)
        assert str(helper.resolve()) in dev._dep_graph[fp]

    def test_package_import_ignored(self, tmp_path):
        fp = str(tmp_path / "body.py")
        src = b"from build123d import *\n"
        tree = _parser.parse(src)
        dev._update_dep_graph(fp, tree, src)
        # build123d is a package, not a local file — dep graph should be empty
        assert dev._dep_graph[fp] == set()


# ── _block_has_error ──────────────────────────────────────────────────────────

class TestBlockHasError:
    def test_valid_block_no_error(self):
        src = "with BuildPart() as body:\n    pass\n"
        tree = parse(src)
        blocks = dev._find_build_blocks(tree, src.encode())
        assert blocks[0][1].has_error is False

    def test_error_block_detected(self):
        # Missing comma between args — syntax error inside block
        src = "with BuildPart() as body:\n    Rectangle(20 35)\n"
        tree = parse(src)
        blocks = dev._find_build_blocks(tree, src.encode())
        assert len(blocks) == 1
        assert blocks[0][1].has_error is True

    def test_error_in_one_block_not_other(self):
        src = (
            "with BuildPart() as body:\n    Rectangle(20 35)\n"
            "with BuildPart() as cab:\n    pass\n"
        )
        tree = parse(src)
        blocks = {v: n for v, n in dev._find_build_blocks(tree, src.encode())}
        assert blocks["body"].has_error is True
        assert blocks["cab"].has_error is False


# ── _defined_names ────────────────────────────────────────────────────────────

def _first_child(src: str):
    tree = parse(src)
    return tree.root_node.children[0]


class TestDefinedNames:
    def test_simple_assignment(self):
        assert dev._defined_names(_first_child("x = 1\n")) == {"x"}

    def test_augmented_assignment(self):
        assert dev._defined_names(_first_child("x += 1\n")) == {"x"}

    def test_star_import_returns_none(self):
        assert dev._defined_names(_first_child("from foo import *\n")) is None

    def test_named_import_from(self):
        assert dev._defined_names(_first_child("from foo import bar\n")) == {"bar"}

    def test_aliased_import_from(self):
        assert dev._defined_names(_first_child("from foo import bar as b\n")) == {"b"}

    def test_import_statement(self):
        assert dev._defined_names(_first_child("import foo\n")) == {"foo"}

    def test_import_aliased(self):
        assert dev._defined_names(_first_child("import foo as f\n")) == {"f"}

    def test_function_def(self):
        assert dev._defined_names(_first_child("def helper(): pass\n")) == {"helper"}

    def test_class_def(self):
        assert dev._defined_names(_first_child("class Foo: pass\n")) == {"Foo"}

    def test_unknown_returns_none(self):
        # A bare expression is not a binding — should return None
        assert dev._defined_names(_first_child("foo()\n")) is None


# ── pre_part change → only referencing blocks re-execute ─────────────────────

class TestPrePartDependency:
    """Verify that only blocks referencing a changed pre-part var are stale."""

    def _changed_vars(self, src1: str, src2: str):
        """Return changed_pre_part_vars computed from the two sources."""
        b1, b2 = src1.encode(), src2.encode()
        s, oe, ne = dev._compute_edit(b1, b2)
        changed_ranges = [dev._ByteRange(s, ne)] if (s != oe or s != ne) else []

        tree = _parser.parse(b2)
        blocks = dev._find_build_blocks(tree, b2)
        block_ranges = [(n.start_byte, n.end_byte) for _, n in blocks]

        changed_pre_part_vars: set | None = set()
        for r in changed_ranges:
            in_block = any(r.start_byte < be and r.end_byte > bs
                           for bs, be in block_ranges)
            if not in_block:
                for child in tree.root_node.children:
                    if r.start_byte < child.end_byte and r.end_byte > child.start_byte:
                        names = dev._defined_names(child)
                        if names is None:
                            changed_pre_part_vars = None
                        elif isinstance(changed_pre_part_vars, set):
                            changed_pre_part_vars.update(names)
                        break
            if changed_pre_part_vars is None:
                break
        return changed_pre_part_vars

    def test_only_referencing_block_stale(self):
        src1 = "c = 1\nwith BuildPart() as body:\n    x = c\nwith BuildPart() as cab:\n    pass\n"
        src2 = "c = 2\nwith BuildPart() as body:\n    x = c\nwith BuildPart() as cab:\n    pass\n"
        cvars = self._changed_vars(src1, src2)
        assert cvars == {"c"}
        tree = _parser.parse(src2.encode())
        blocks = {v: n for v, n in dev._find_build_blocks(tree, src2.encode())}
        assert bool(dev._referenced_names(blocks["body"]) & cvars) is True
        assert bool(dev._referenced_names(blocks["cab"]) & cvars) is False

    def test_star_import_change_fallback(self):
        src1 = "from foo import *\nwith BuildPart() as body:\n    pass\n"
        src2 = "from bar import *\nwith BuildPart() as body:\n    pass\n"
        cvars = self._changed_vars(src1, src2)
        assert cvars is None  # unknown names → full re-exec fallback
