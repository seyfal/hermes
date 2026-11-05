"""skills/memory ``write_approval_origins``: scope the write-approval gate to some write origins.

``skills.write_approval: true`` + ``skills.write_approval_origins: [background_review]`` holds the
after-session review's (and the curator's) skill writes in pending/skills while the session's own
skill writes land at once. Unset/empty keeps today's behavior (every origin), and an unknown name
fails closed (every origin)."""

import importlib
import json
import os
import shutil
import tempfile

import pytest

SK = "---\nname: {n}\ndescription: Use when probing the origin scope.\n---\n# Probe\nStep 1.\n"


@pytest.fixture
def hermes_home(monkeypatch):
    d = tempfile.mkdtemp(prefix="hermes_wa_origins_")
    home = os.path.join(d, ".hermes")
    os.makedirs(os.path.join(home, "skills"))
    monkeypatch.setenv("HERMES_HOME", home)
    monkeypatch.setenv("HERMES_YOLO_MODE", "1")
    yield home
    shutil.rmtree(d, ignore_errors=True)


def _set(subsystem, **kv):
    import hermes_cli.config as cfg
    c = cfg.load_config()
    c.setdefault(subsystem, {}).update(kv)
    cfg.save_config(c)


def _as_origin(origin, fn):
    from tools.skill_provenance import reset_current_write_origin, set_current_write_origin
    tok = set_current_write_origin(origin)
    try:
        return fn()
    finally:
        reset_current_write_origin(tok)


def _smt():
    import tools.skill_manager_tool as smt
    return importlib.reload(smt)


def test_parse_scope_values(hermes_home):
    from tools import write_approval as wa
    assert wa.write_approval_origins("skills") is None  # unset = every origin
    _set("skills", write_approval_origins=[])
    assert wa.write_approval_origins("skills") is None
    _set("skills", write_approval_origins=["background_review"])
    assert wa.write_approval_origins("skills") == {"background_review"}
    _set("skills", write_approval_origins="curator, assistant_tool")
    assert wa.write_approval_origins("skills") == {"background_review", "foreground"}
    _set("skills", write_approval_origins=["background_reveiw"])  # typo: fail closed
    assert wa.write_approval_origins("skills") is None
    assert wa.origin_in_scope("skills", "assistant_tool") is True


def test_scope_background_only_gates_review_not_session(hermes_home):
    from tools import write_approval as wa
    _set("skills", write_approval=True, write_approval_origins=["background_review"])
    assert _as_origin("assistant_tool", lambda: wa.evaluate_gate(wa.SKILLS)).allow is True
    assert _as_origin("foreground", lambda: wa.evaluate_gate(wa.SKILLS)).allow is True
    assert _as_origin("background_review", lambda: wa.evaluate_gate(wa.SKILLS)).stage is True


def test_skill_manage_session_write_lands_review_write_stages(hermes_home):
    from tools import write_approval as wa
    _set("skills", write_approval=True, write_approval_origins=["background_review"])
    smt = _smt()
    live = json.loads(_as_origin("assistant_tool", lambda: smt.skill_manage(
        action="create", name="probe", content=SK.format(n="probe"))))
    assert live["success"] is True and not live.get("staged"), live
    md = os.path.join(hermes_home, "skills", "probe", "SKILL.md")
    assert os.path.exists(md)  # the session sees its own edit at once
    held = json.loads(_as_origin("background_review", lambda: smt.skill_manage(
        action="patch", name="probe", old_string="Step 1.", new_string="Step 1.\nStep 2.")))
    assert held.get("staged") is True, held
    assert "Step 2." not in open(md).read()  # held: nothing on disk until approved
    rec = wa.get_pending("skills", held["pending_id"])
    assert rec["origin"] == "background_review" and rec["payload"]["action"] == "patch"


def test_unscoped_gate_still_stages_every_origin(hermes_home):
    from tools import write_approval as wa
    _set("skills", write_approval=True)
    assert _as_origin("assistant_tool", lambda: wa.evaluate_gate(wa.SKILLS)).stage is True
    assert _as_origin("background_review", lambda: wa.evaluate_gate(wa.SKILLS)).stage is True


def test_gate_off_ignores_scope(hermes_home):
    from tools import write_approval as wa
    _set("skills", write_approval=False, write_approval_origins=["foreground"])
    assert _as_origin("assistant_tool", lambda: wa.evaluate_gate(wa.SKILLS)).allow is True


def test_status_line_names_the_scope(hermes_home):
    from hermes_cli.write_approval_commands import _fmt_state
    _set("skills", write_approval=True, write_approval_origins=["background_review"])
    assert _fmt_state("skills") == "skills.write_approval = on (background_review writes only)"
