"""Released prompts are versioned and immutable: edits must go into a new version file."""
import hashlib
import json
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def test_released_prompts_are_unchanged():
    manifest = json.loads((PROMPTS / "MANIFEST.json").read_text())["released"]
    for name, digest in manifest.items():
        actual = hashlib.sha256((PROMPTS / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name} changed after release; create a new version instead"


def test_production_prompts_are_released():
    from app.classifier import CLASSIFIER_VERSION
    from app.config import settings
    manifest = json.loads((PROMPTS / "MANIFEST.json").read_text())["released"]
    assert f"{settings.eir_prompt_version}.md" in manifest
    assert f"{CLASSIFIER_VERSION}.md" in manifest


def test_eir_prompt_covers_every_spec_rule():
    text = (PROMPTS / "eir_system_v1.md").read_text()
    for must in ("988", "911", "I'd rather not say", "keep every founder's information private", "<flow>pause</flow>",
                 "Never diagnose", "cumulative", "sleep deprivation", "Never mention internal safety checks"):
        assert must in text, must
