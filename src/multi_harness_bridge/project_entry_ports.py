"""Native skill-picker port shared by the supported harness adapters."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .propagation import HARNESS_DIRS


@dataclass(frozen=True)
class NativeSkillPicker:
    """Filesystem adapter for a host's native project skill discovery path."""
    harness: str
    relative_directory: str

    def destination(self, workspace: Path, name: str) -> Path:
        return workspace / self.relative_directory / name


def native_skill_picker(harness: str) -> NativeSkillPicker:
    try:
        relative_directory = HARNESS_DIRS[harness]
    except KeyError:
        raise ValueError("Unsupported project skill-entry host") from None
    return NativeSkillPicker(harness, relative_directory)
