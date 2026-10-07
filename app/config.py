"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"


def _load_dotenv() -> None:
    """Load a git-ignored .env file if present. Never overrides real environment variables."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.environ.get("DATABASE_URL", "postgresql://aieir_app@localhost:5432/aieir")
    eir_model: str = os.environ.get("EIR_MODEL", "claude-sonnet-5-5")
    classifier_model: str = os.environ.get("CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
    grader_model: str = os.environ.get("GRADER_MODEL", "claude-opus-5-5")
    eir_prompt_version: str = os.environ.get("EIR_PROMPT_VERSION", "eir_system_v1")
    max_tokens: int = int(os.environ.get("EIR_MAX_TOKENS", "4000"))


settings = Settings()


def prompt_path(version: str) -> Path:
    return PROMPTS_DIR / f"{version}.md"


def load_prompt(version: str) -> str:
    return prompt_path(version).read_text()
