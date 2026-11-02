"""A whole-file ``cat`` in the terminal counts as a full read for write_file.

The write guard refused write_file after the model had read the file with
``cat FILE``: only read_file recorded a full-content baseline, so the model re-read
and regenerated the file. A plain whole-file cat whose output the model saw verbatim
now records the same state a full read_file does. Partial views (head, grep, sed -n,
pipes) still record nothing, and a change after the cat still refuses the write.
"""

import json
import os

import pytest

from tools import terminal_tool as tt
from tools.file_tools import clear_file_ops_cache
from tools.file_tools_read_tracking import _cat_renderings, _whole_file_cat_paths
from tools.registry import registry

ORIGINAL = "first line\nsecond line\nthird line\n"


@pytest.fixture
def task(request):
    task_id = f"cat-read-{request.node.name}"
    yield task_id
    clear_file_ops_cache(task_id)


def _terminal(command, task_id, **kwargs):
    result = json.loads(tt.terminal_tool(command=command, task_id=task_id, **kwargs))
    assert result.get("exit_code") == 0, result
    return result


def _write(path, task_id, content="replacement\n"):
    result = registry.dispatch("write_file", {"path": str(path), "content": content}, task_id=task_id)
    return json.loads(result)


def _file(tmp_path, name="notes.txt", content=ORIGINAL):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.parametrize("command", ["cat {p}", "cat -n {p}", "cat -- {p}", "cat '{p}'"])
def test_write_after_full_cat_is_allowed(tmp_path, task, command):
    path = _file(tmp_path)
    _terminal(command.format(p=path), task)
    written = _write(path, task)
    assert "error" not in written, written
    assert path.read_text(encoding="utf-8") == "replacement\n"


def test_cat_of_several_files_covers_each_of_them(tmp_path, task):
    first, second = _file(tmp_path, "a.txt", "alpha\n"), _file(tmp_path, "b.txt", "beta\n")
    _terminal(f"cat -n {first} {second}", task)
    for path in (first, second):
        written = _write(path, task)
        assert "error" not in written, (path, written)


@pytest.mark.parametrize(
    "command",
    ["head -n 2 {p}", "tail -n 1 {p}", "grep second {p}", "sed -n 1,2p {p}", "cat {p} | head -n 1", "cat -s {p}"],
)
def test_write_after_partial_view_is_still_refused(tmp_path, task, command):
    path = _file(tmp_path)
    _terminal(command.format(p=path), task)
    refused = _write(path, task)
    assert refused.get("stale_write_blocked"), refused
    assert path.read_text(encoding="utf-8") == ORIGINAL


def test_write_after_cat_then_external_edit_is_refused(tmp_path, task):
    path = _file(tmp_path)
    _terminal(f"cat {path}", task)
    stamp = path.stat()
    path.write_text("someone else's edit\nsecond line\nthird line\n", encoding="utf-8")
    # The baseline is a byte hash: even a preserved mtime does not hide the edit.
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    refused = _write(path, task)
    assert refused.get("stale_write_blocked"), refused
    assert path.read_text(encoding="utf-8").startswith("someone else's edit")


def test_relative_cat_resolves_against_the_terminal_cwd(tmp_path, task):
    workdir = tmp_path / "project"
    workdir.mkdir()
    path = _file(workdir, "config.ini", "[core]\nname = demo\n")
    _terminal("cat config.ini", task, workdir=str(workdir))
    written = _write(path, task, content="[core]\nname = renamed\n")
    assert "error" not in written, written
    assert path.read_text(encoding="utf-8") == "[core]\nname = renamed\n"


def test_relative_cat_of_another_directory_does_not_bless_a_same_named_file(tmp_path, task):
    elsewhere, target = tmp_path / "elsewhere", tmp_path / "target"
    elsewhere.mkdir()
    target.mkdir()
    _file(elsewhere, "same.txt", "same\n")
    path = _file(target, "same.txt", "same\n")
    _terminal("cat same.txt", task, workdir=str(elsewhere))
    refused = _write(path, task)
    assert refused.get("stale_write_blocked"), refused


def test_parser_accepts_only_plain_whole_file_cat():
    assert _whole_file_cat_paths("cat a.txt") == (["a.txt"], False)
    assert _whole_file_cat_paths("cat -n a.txt 'b c.txt'") == (["a.txt", "b c.txt"], True)
    assert _whole_file_cat_paths("cat -- -n") == (["-n"], False)
    for command in ("cat", "cat -", "cat -v a", "cat -b a", "cat a > b", "cat a; rm b", "cat *.txt",
                    "cat ~/a", "cat $HOME/a", "cat a | head", "head a", "tac a", "cat a && echo"):
        assert _whole_file_cat_paths(command) is None, command


def test_numbered_rendering_matches_gnu_and_bsd_cat_n():
    assert _cat_renderings(["a\nb\n"], True) == {"     1\ta\n     2\tb\n"}
    assert _cat_renderings(["a\n", "b\n"], False) == {"a\nb\n"}
    # GNU numbers the concatenated stream (an unterminated line joins the next file);
    # BSD/macOS restarts at 1 for each file.
    assert _cat_renderings(["a", "b\n"], True) == {"     1\tab\n", "     1\ta     1\tb\n"}
    assert "     1\tx\n     2\ty" in _cat_renderings(["x\n", "y"], True)
