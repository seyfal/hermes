"""Skill hints in delegated child prompts (tools/delegate_tool_skill_hints.py)."""
from tools.delegate_tool_progress import _build_child_system_prompt
from tools.delegate_tool_skill_hints import build_skill_hints, match_skills

SKILLS = [
    {"name": "notion", "description": "Use when reading or writing Notion pages and databases.", "path": "/s/notion/SKILL.md"},
    {"name": "github", "description": "Use when driving the gh CLI: PR data, issues, CI, repos.", "path": "/s/github/SKILL.md"},
    {"name": "swe-code-review", "description": "Use when reviewing a diff or pull request for bugs.", "path": "/s/r/SKILL.md"},
    {"name": "gpu-cluster-ops", "description": "Use when running Slurm jobs on the B200 cluster.", "path": "/s/g/SKILL.md"},
]
ON = {"skill_hints": True}


def test_matches_rare_words_and_ranks():
    hits = match_skills("Update the rows in the Notion database from these payload files", SKILLS, 3)
    assert [h["name"] for h in hits][:1] == ["notion"]
    hits = match_skills("Submit the training job to Slurm on the cluster and wait", SKILLS, 3)
    assert [h["name"] for h in hits] == ["gpu-cluster-ops"]


def test_no_match_on_generic_text():
    assert match_skills("Please do the task and report back when you are done", SKILLS, 3) == []


def test_skips_skills_the_parent_named_and_caps():
    assert "github" not in [h["name"] for h in match_skills("Use the github skill to list PR issues and CI", SKILLS, 3)]
    assert len(match_skills("review the pull request diff, gh CLI PR issues, Notion pages databases", SKILLS, 1)) == 1


def test_block_lists_name_and_path_and_flag_turns_it_off():
    block = build_skill_hints("Review this pull request diff for bugs", None, ON, SKILLS)
    assert "RELEVANT SKILLS" in block and "swe-code-review (/s/r/SKILL.md)" in block
    assert build_skill_hints("Review this pull request diff for bugs", None, {"skill_hints": False}, SKILLS) == ""
    assert build_skill_hints("Review this pull request diff for bugs", None, {"skill_hints_max": 0}, SKILLS) == ""


def test_child_prompt_carries_the_block():
    block = build_skill_hints("Review this pull request diff for bugs", "repo at /tmp/x", ON, SKILLS)
    prompt = _build_child_system_prompt("Review this pull request diff for bugs", "repo at /tmp/x", skill_hints=block)
    assert "CONTEXT:" in prompt and "swe-code-review" in prompt
    assert "RELEVANT SKILLS" not in _build_child_system_prompt("x", "y")


def test_topic_word_is_not_naming_the_skill():
    assert [h["name"] for h in match_skills("Update the Notion database rows", SKILLS, 3)] == ["notion"]
    assert "notion" not in [h["name"] for h in match_skills("Read the notion skill, then update the Notion database rows", SKILLS, 3)]
    assert "swe-code-review" not in [h["name"] for h in match_skills("Follow swe-code-review on this pull request diff", SKILLS, 3)]
