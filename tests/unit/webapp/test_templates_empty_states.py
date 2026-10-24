"""An empty page's big icon is styled by a rule on the empty state's own icon
(".empty-state > i"), never on every icon inside it (".empty-state i"): that
also caught the icon in its button, which grew to the big icon's size and
stretched the button into a square (Active Jobs' "Start a Rollout",
Security Profiles' "Add First Profile", Variable Mappings' "Add First
Mapping" - B on rc9)."""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
LOOSE = re.compile(r"\.empty-(?:state|wrap)\s+i\b")


def test_no_empty_state_rule_styles_the_icons_in_its_buttons():
	loose = [f"{path.name}: {line.strip()}" for path in sorted(TEMPLATES.rglob("*.html"))
	         for line in path.read_text(encoding="utf-8").splitlines() if LOOSE.search(line)]
	assert loose == []
