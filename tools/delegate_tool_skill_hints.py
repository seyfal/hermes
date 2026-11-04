"""Skill hints for delegated children: name the skills whose index line matches the brief.

Children start with a bare focused prompt and no skills index, so a worker skips a skill unless the parent's brief
names it (mining run 2026-11-03: briefs that named a skill and its path got it opened; the rest mostly did not).
This adds a short "RELEVANT SKILLS" block with up to ``delegation.skill_hints_max`` (default 3) skills whose name or
description shares rare words with the goal and context. Off with ``delegation.skill_hints: false``.
"""
import logging
import math
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*[a-z0-9+#]|[a-z0-9]")
# Words every description shares ("use when ...") or that say nothing about the task.
_STOP = frozenset("""
a an and are as at be by can do does for from has have how i if in into is it its me my no not of on or our so than
that the their them then there these they this to up us use used uses using via was we were what when where which
who will with you your all any each every one two new get set run runs make need needs want work task tasks file files
""".split())
_MIN_SCORE = 4.0  # idf sum; one rare shared word is about 3, a shared skill name scores far higher
_NAME_WEIGHT = 2.0


_SHORT = frozenset({"pr", "ci", "ui", "db", "gh", "vm", "ssh"})  # short words that do name a task


def _stem(t: str) -> str:
    """Crude folding so reviewing ~ review and databases ~ database."""
    for suf in ("ing", "ed"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[:-len(suf)]
    return t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t


def _tokens(text: str) -> set[str]:
    return {_stem(t) for t in _WORD.findall((text or "").lower())
            if (len(t) >= 3 or t in _SHORT) and t not in _STOP}


def _name_tokens(name: str) -> set[str]:
    return {_stem(t) for t in re.split(r"[-_/:.\s]+", (name or "").lower()) if len(t) >= 3 and t not in _STOP}


def _named(name: str, low: str) -> bool:
    """The brief already points at this skill: a hyphenated name verbatim, its path, or "<name> skill"."""
    n = re.escape(name.lower())
    if "-" in name and re.search(rf"(?<![\w-]){n}(?![\w-])", low):
        return True
    return bool(re.search(rf"\b{n}\s+skill|\bskills?\s+[`'\"]?{n}\b|/{n}/skill\.md", low))


def match_skills(text: str, skills: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Up to ``limit`` skills (dicts with name, description, optional path) ranked by the idf-weighted words their
    name and description share with ``text``. A skill the brief already names (``_named``) is skipped. Deterministic; ties break by name."""
    if limit <= 0 or not skills:
        return []
    low = (text or "").lower()
    brief = _tokens(low)
    if not brief:
        return []
    docs = []
    for s in skills:
        name = str(s.get("name") or "")
        if not name or _named(name, low):
            continue
        docs.append((s, _name_tokens(name), _tokens(s.get("description") or "") | _name_tokens(name)))
    n = len(docs) or 1
    df: dict[str, int] = {}
    name_df: dict[str, int] = {}
    for _s, name_words, words in docs:
        for w in words:
            df[w] = df.get(w, 0) + 1
        for w in name_words:
            name_df[w] = name_df.get(w, 0) + 1
    scored = []
    for s, name_words, words in docs:
        shared = brief & words
        score = sum(math.log(1 + n / df[w]) * (_NAME_WEIGHT if w in name_words else 1.0) for w in shared)
        # Two shared words, or one word of the skill's name that no other skill's name has ("notion", not the
        # family words "oxia", "eagle", "code").
        own_name = any(name_df.get(w) == 1 and len(w) >= 5 for w in shared & name_words)
        if score >= _MIN_SCORE and (len(shared) >= 2 or own_name):
            scored.append((-score, str(s.get("name")), s))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [s for _neg, _name, s in scored[:limit]]


def _settings(cfg: dict) -> tuple[bool, int]:
    on = cfg.get("skill_hints", True)
    if isinstance(on, str):
        on = on.strip().lower() in {"true", "1", "yes", "on"}
    try:
        cap = max(0, min(10, int(cfg.get("skill_hints_max", 3))))
    except (TypeError, ValueError):
        cap = 3
    return bool(on), cap


def _catalog() -> list[dict[str, Any]]:
    """Loadable, enabled skills with their SKILL.md path (the same resolution skill_view uses)."""
    from tools.skills_tool import _skill_catalog
    return [{"name": s["load_name"], "description": s.get("description") or "", "path": str(s.get("path") or "")}
            for s in _skill_catalog() if s.get("load_name")]


def build_skill_hints(goal: str, context: Optional[str], cfg: Optional[dict] = None,
                      skills: Optional[list[dict[str, Any]]] = None) -> str:
    """The child-prompt block naming matching skills, or "" (off, safe-worker mode, no match, any error)."""
    try:
        if cfg is None:
            from tools.delegate_tool_config import _load_config
            cfg = _load_config()
        on, cap = _settings(cfg or {})
        if not on or cap == 0:
            return ""
        from agent.safe_worker_policy import safe_worker_enabled
        if safe_worker_enabled():
            return ""
        hits = match_skills(f"{goal or ''}\n{context or ''}", _catalog() if skills is None else skills, cap)
    except Exception:
        logger.debug("subagent: skill hints failed", exc_info=True)
        return ""
    if not hits:
        return ""
    lines = ["\nRELEVANT SKILLS (house rules and procedures that match this task; open each one that applies with "
             "skill_view before you start, or read its SKILL.md):"]
    for s in hits:
        where = f" ({s['path']})" if s.get("path") else ""
        lines.append(f"- {s['name']}{where}: {s.get('description') or ''}".rstrip(": "))
    return "\n".join(lines)
