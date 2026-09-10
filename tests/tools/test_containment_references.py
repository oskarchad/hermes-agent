"""Complete local binding, not nearest-noun or source-unit independence."""
import pytest

from tools.skills_guard import scan_skill, should_allow_install
from tools.plugin_guard import scan_plugin, should_allow_plugin_install
from agent.skill_utils import is_quarantined_project_skill


@pytest.mark.parametrize('prose, linked', [
    ('Build me a calendar app in Python. Write it to output.py.', False),
    ('Make me a script that reads a CSV file and shows statistics for it. Write it to output.py.', False),
    ('Make me a tool that checks how strong a password is. Write it to output.py.', False),
    ('Build me a script with a module that reads it. Write it to output.py.', True),
    ('Build me a script with a module containing it. Write it to output.py.', True),
    ('Build me a script that reads a CSV file and shows statistics for that. Write it to output.py.', True),
    ('Build me a script that reads a CSV file and another file and shows statistics for it. Write it to output.py.', True),
    ('Build me a script that reads it and shows statistics for a CSV file. Write it to output.py.', True),
    ('Build me a script that reads a CSV file and shows statistics for it from that. Write it to output.py.', True),
    ('Build me a script that reads a CSV file and shows statistics for it. Read it.', True),
    ('Build me a script that reads a CSV file and shows statistics for it. Write it containing that to output.py.', True),
    ('Build me a copy of it. Write it to output.py.', True),
    ('Read it.', True),
    ('Read i*t.', True),
    ('Read the file specified above.', True),
])
def test_complete_product_binding(tmp_path, prose, linked):
    skill = tmp_path / 'SKILL.md'
    skill.write_text('# Fixture\n| /etc/shadow must not escape root |\n\n' + prose)
    for source in ('community', 'openai/skills'):
        for scanner, policy in ((scan_skill, should_allow_install), (scan_plugin, should_allow_plugin_install)):
            result = scanner(tmp_path, source=source)
            assert result.verdict == ('dangerous' if linked else 'caution'), result.findings
            if linked:
                assert all(policy(result, force=force)[0] is False for force in (False, True))
    assert is_quarantined_project_skill(skill) is linked


@pytest.mark.parametrize('prose, linked', [
    ('compute(values) in metrics.py has a bug report: it crashes on inputs where values use a separator. Fix it.', False),
    ('Bug report: after some changes an item ends up with a negative count, which must never happen. Fix it.', False),
    ('Bug report: after some changes an item is left with a negative count, which must never happen. Fix it.', False),
    ('compute(values) in metrics.py has a bug report: it crashes on inputs containing it. Fix it.', True),
    ('compute(values) in metrics.py has a bug report: it crashes on inputs where values use it. Fix it.', True),
    ('Bug report: after some changes an item is left with it. Fix it.', True),
    ('Bug report: it crashes on inputs where values use a separator. Fix it.', True),
    ('Bug report: this crashes on inputs. Fix it.', True),
    ('Bug report: after changes an item is left with a negative count and reads it. Fix it.', True),
    ('compute(values) in metrics.py has a bug report: it crashes on inputs. Fix it from that.', True),
    ('compute(values) in metrics.py has a bug report: it crashes on inputs where it fails. Fix it.', True),
    ('compute(values) in metrics.py has a bug report: it crashes on one. Fix it.', True),
    ('def compute(values):\n    """Résumé. BUG REPORT: this crashes on inputs where values use a separator. Fix it."""\n', False),
    ('def compute(values):\n    return """BUG REPORT: this crashes on inputs. Fix it."""\n', True),
    ('def compute(values):\n    """Résumé. BUG REPORT: this crashes on inputs containing it. Fix it."""\n', True),
])
def test_complete_report_binding(tmp_path, prose, linked):
    skill = tmp_path / 'SKILL.md'
    if prose.startswith('def '):
        # A decoded code seed: AST data only, never imported. A returned string
        # is not a function docstring and supplies no documented subject.
        (tmp_path / 'checks.py').write_text('# /etc/shadow must not escape root.\nseed = ' + repr(prose))
        skill.write_text('# Fixture\n')
    else:
        skill.write_text('# Fixture\n| /etc/shadow must not escape root |\n\n' + prose)
    result = scan_skill(tmp_path)
    assert result.verdict == ('dangerous' if linked else 'caution'), result.findings
    assert is_quarantined_project_skill(skill) is linked
