"""Helpers for discovering runtime workspace skills."""

from pathlib import Path

from app.core.log import logger
from app.schema.ai import Skill

_FRONTMATTER_DELIMITER = "---"
_NAME_PREFIX = "name:"
_DESCRIPTION_PREFIX = "description:"
_QUOTE_CHARS = "\"'"

SKILL_FILE_NAME = "SKILL.md"
SKILLS_DIRECTORY_NAME = "skills"


def workspace_skill_name_for_path(path: Path) -> str | None:
    """Return the skill directory name when `path` is a workspace skills/<name>/SKILL.md file.

    Centralizes the path predicate so the read_file hook (plan P2 #18) and the
    discovery loop stay consistent if the layout changes. Returns None for anything
    that is not a workspace skill file.
    """

    if path.name != SKILL_FILE_NAME:
        return None
    skill_dir = path.parent
    if skill_dir.parent.name != SKILLS_DIRECTORY_NAME:
        return None
    skill_name = skill_dir.name
    if not skill_name or skill_name == SKILLS_DIRECTORY_NAME:
        return None
    return skill_name


def _strip_wrapping_quotes(value: str) -> str:
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in _QUOTE_CHARS:
        return normalized[1:-1].strip()
    return normalized


def _read_skill_metadata(skill_file: Path) -> Skill | None:
    try:
        lines = skill_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        logger.exception(f"Failed to read skill metadata from {skill_file}")
        return None

    name: str | None = None
    description: str | None = None
    in_frontmatter = False

    for raw_line in lines:
        line = raw_line.strip()

        if line == _FRONTMATTER_DELIMITER:
            if not in_frontmatter:
                in_frontmatter = True
                continue
            break

        if not in_frontmatter:
            continue

        if line.startswith(_NAME_PREFIX):
            name = _strip_wrapping_quotes(line[len(_NAME_PREFIX) :])
        elif line.startswith(_DESCRIPTION_PREFIX):
            description = _strip_wrapping_quotes(line[len(_DESCRIPTION_PREFIX) :])

        if name and description:
            return Skill(name=name, description=description, location=skill_file.parent)

    logger.warning(
        f"Skipping skill directory {skill_file.parent} because SKILL.md is missing name/description frontmatter"
    )
    return None


def discover_workspace_skills(workspace_root: Path) -> list[Skill]:
    skills_base_dir = workspace_root / "skills"
    if not skills_base_dir.is_dir():
        return []

    skill_directories = sorted(
        (candidate for candidate in skills_base_dir.iterdir() if candidate.is_dir()),
        key=lambda candidate: candidate.name.lower(),
    )
    skills: list[Skill] = []

    for skill_dir in skill_directories:
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            logger.warning(f"Skipping skill directory {skill_dir} because it does not contain a SKILL.md file")
            continue

        if skill := _read_skill_metadata(skill_file):
            skills.append(skill)

    return skills
