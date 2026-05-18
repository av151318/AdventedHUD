from pathlib import Path

from hud.onboarding import hud_soul_md_gate_issues


def test_soul_template_has_valid_part_12_13_tables():
    template = Path(__file__).resolve().parents[1] / "docs" / "soul-template.md"
    content = template.read_text(encoding="utf-8")
    issues = hud_soul_md_gate_issues(content)
    assert "part_12:no_populated_table_row" not in issues
    assert "part_13:no_populated_table_row" not in issues
