"""The guide is a deliverable, so it gets tests too.

Every one of these caught something during the cold read: a station without a checkpoint, a
command a reader could not place, a screenshot gap with no caption, a link to a file that moved.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted(
    p
    for p in ROOT.rglob("*.md")
    if ".venv" not in p.parts
    and "node_modules" not in p.parts
    and p.name not in {"BUILD_LOG.md", "DANIEL_TODO.md", "MOCKS.md"}
)
SETUP = (ROOT / "docs" / "SETUP.md").read_text("utf-8")


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_point_at_files_that_exist(doc: Path):
    missing = []
    for target in re.findall(r"\]\(((?!https?:|#|mailto:)[^)]+)\)", doc.read_text("utf-8")):
        path = target.split("#")[0]
        if not path or path.startswith("setup/"):  # screenshots arrive with the operator
            continue
        if not (doc.parent / path).exists():
            missing.append(target)
    assert not missing, f"{doc.name} links to files that do not exist: {missing}"


def test_setup_has_seven_stations_each_with_a_checkpoint():
    stations = re.split(r"\n## Station ", SETUP)[1:]
    assert len(stations) == 7, "spec §15: stations 0 through 6"
    for station in stations:
        title = station.splitlines()[0]
        assert "Checkpoint" in station or "Skip" in station, f"no checkpoint in station: {title}"
        assert re.search(r"\*\*Time:", station), f"no time estimate in station: {title}"


def test_every_command_box_says_what_the_command_does():
    """A reader who has never used a terminal must not have to guess what they just pasted."""
    unexplained = []
    for match in re.finditer(r"```bash\n(.*?)```\n(.{0,400})", SETUP, re.S):
        following = match.group(2)
        if not following.lstrip().startswith("*What it does"):
            unexplained.append(match.group(1).strip().splitlines()[0][:60])
    assert not unexplained, f"command boxes with no explanation: {unexplained}"


def test_every_screenshot_gap_describes_the_picture():
    images = re.findall(r"!\[[^\]]*\]\(setup/([^)]+)\)", SETUP)
    captions = re.findall(r"!\[[^\]]*\]\(setup/[^)]+\)\s*\n\s*\*Screenshot to take:", SETUP)
    assert images, "the guide is supposed to be illustrated"
    assert len(captions) == len(images), "a placeholder without a caption is useless to whoever takes it"


def test_the_screenshot_list_matches_the_guide():
    listed = set(re.findall(r"\| `([^`]+\.png)` \|", (ROOT / "docs" / "setup" / "README.md").read_text("utf-8")))
    used = set(re.findall(r"!\[[^\]]*\]\(setup/([^)]+)\)", SETUP))
    assert listed == used, f"shot list and guide disagree: {listed ^ used}"


def test_the_guide_states_the_udm_caveat_honestly():
    assert "firmware" in SETUP.lower()
    assert "Raspberry Pi" in SETUP, "the non-technical reader must have the stable alternative"


def test_the_guide_has_a_get_help_section_asking_for_doctor_output():
    assert "If you get stuck" in SETUP
    assert "printbridge doctor" in SETUP.split("If you get stuck")[1]


def test_no_daniel_specific_values_leak_into_the_repository():
    """Generic: the operator's own values live in .env, not in the code or the docs."""
    forbidden = ("houseofstark", "hermes", "canon", "lbp673")
    offenders = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".venv" in path.parts or ".git" in path.parts:
            continue
        if path.name == "test_docs.py":  # this file has to name the words it forbids
            continue
        if path.suffix not in {".py", ".md", ".yml", ".yaml", ".toml", ".sh", ".caddy", ".conf"}:
            continue
        if path.name in {"BUILD_LOG.md", "DANIEL_TODO.md", "MOCKS.md", "CLAUDE.md"}:
            continue
        text = path.read_text("utf-8", errors="ignore").lower()
        for word in forbidden:
            if word in text:
                offenders.append(f"{path.relative_to(ROOT)}: {word}")
    assert not offenders, f"host-specific values in the repository: {offenders}"


def test_troubleshooting_covers_every_failure_doctor_can_print():
    """Spec §8.1: every troubleshooting entry maps to a doctor finding, and back."""
    doctor = (ROOT / "printbridge" / "doctor.py").read_text("utf-8")
    guide = (ROOT / "docs" / "troubleshooting.md").read_text("utf-8")
    check_names = {
        re.sub(r"\{[^}]+\}", "<name>", name)
        for name in re.findall(r'report\.add\(\s*f?"([^"]+)"', doctor)
    }
    skip = {"config warning"}  # covered by the "config" entry, which quotes its messages
    missing = [
        name
        for name in check_names - skip
        if name.replace("<name>", "") not in guide and name not in guide
    ]
    assert not missing, f"doctor can report these with no troubleshooting entry: {missing}"
