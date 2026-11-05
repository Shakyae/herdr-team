"""Skill entry-point contract: the frontmatter is a shipped interface.

`disable-model-invocation: true` made /herdr-team reachable only by a human
typing it. Users who have already authorized HTM work should not have to type
the command again, so the Skill is model-invocable and instead constrains
*when* the model may invoke it in the description.

These tests are the regression guard for that decision: if a later edit drops
`user-invocable` or reinstates `disable-model-invocation`, the installed Skill
silently becomes unreachable again.
"""
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'SKILL.md'


def frontmatter():
    text = SKILL.read_text(encoding='utf-8')
    assert text.startswith('---\n'), 'SKILL.md must open with a YAML frontmatter block'
    end = text.index('\n---\n', 4)
    block = text[4:end]
    fields = {}
    for line in block.splitlines():
        if not line.strip() or line.startswith(' '):
            continue
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip()
    return fields


class TestSkillFrontmatter:
    def test_model_may_invoke_the_skill(self):
        fields = frontmatter()
        assert fields.get('disable-model-invocation', 'false').lower() != 'true', (
            'disable-model-invocation: true makes /herdr-team human-only; '
            'an authorized Lead could not start a team'
        )

    def test_human_may_still_invoke_the_skill(self):
        fields = frontmatter()
        assert fields.get('user-invocable', 'true').lower() != 'false', (
            'user-invocable: false removes /herdr-team from the human entry point'
        )

    def test_description_limits_when_the_model_may_trigger(self):
        description = frontmatter().get('description', '')
        assert '明确' in description, (
            'the description must state that the model may only invoke this Skill '
            'on explicit user authorization; otherwise it fires on any task that '
            'merely looks parallelizable'
        )
        assert '不要' in description or 'do not' in description.lower(), (
            'the description must also say what is NOT authorization'
        )

    def test_body_separates_entry_authorization_from_execution_approval(self):
        text = SKILL.read_text(encoding='utf-8')
        assert 'Skill 入口授权' in text and '内部执行审批' in text, (
            'the body must state that allowing model invocation does not relax '
            'approval=boundary, run_approve fail-closed, gate evidence or identity checks'
        )


class TestInstallerPreservesFrontmatter:
    def test_installer_copies_skill_md_verbatim(self):
        installer = (SKILL.parent / 'install-local.sh').read_text(encoding='utf-8')
        assert '"$source/SKILL.md"' in installer, (
            'install-local.sh must copy SKILL.md from the package source; a generated '
            'or rewritten SKILL.md would drift from the reviewed frontmatter'
        )
        assert 'disable-model-invocation' not in installer, (
            'the installer must not patch the frontmatter; the shipped file is the '
            'single source of truth'
        )

    def test_installed_copy_matches_source_when_present(self):
        installed = Path.home() / '.agents/skills/herdr-team/SKILL.md'
        if not installed.exists():
            pytest.skip('no local installation to compare against')
        assert installed.read_bytes() == SKILL.read_bytes(), (
            'the installed SKILL.md differs from the source; re-run '
            'install-local.sh --upgrade from the committed tree'
        )
