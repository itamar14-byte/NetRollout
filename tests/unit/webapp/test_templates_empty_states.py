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


ICON_RULE = re.compile(r"\.empty-(?:state|wrap)\s*>\s*i\s*\{([^}]*)\}")


def test_every_empty_state_icon_is_visible():
	"""Each empty state's big icon takes the theme's colour for decorative icons
	(--nr-text-faint, at least 3:1 on the page): the near-black ones (#1e1e1e,
	#222 on a #1c1c1c card) were invisible."""
	rules = [(path.name, body) for path in sorted(TEMPLATES.rglob("*.html"))
	         for body in ICON_RULE.findall(path.read_text(encoding="utf-8"))]
	assert len(rules) >= 6
	assert [name for name, body in rules if "color: var(--nr-text-faint)" not in body] == []
