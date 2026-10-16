"""Environment-driven configuration.

Everything the app needs to know about paths and language comes from here, so
a different machine (or the later deployment) only needs environment
variables — never a code edit.
"""

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Frozen so nobody mutates config by accident."""

    database_path: Path
    data_dir: Path
    target_lang: str

    @property
    def dict_tei_path(self) -> Path:
        """FreeDict TEI file produced by scripts/import_dict.py's download step."""
        return self.data_dir / "deu-eng.tei"

    @property
    def freq_path(self) -> Path:
        """de_50k.txt frequency list used by scripts/import_freq.py."""
        return self.data_dir / "de_50k.txt"


def _env_path(name: str, default: Path) -> Path:
    """Read a path from the environment, resolving relative paths against the repo root."""
    raw = os.environ.get(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings, built from the environment on first use."""
    return Settings(
        database_path=_env_path("DATABASE_PATH", PROJECT_ROOT / "data" / "wortgut.sqlite3"),
        data_dir=_env_path("DATA_DIR", PROJECT_ROOT / "data"),
        target_lang=os.environ.get("TARGET_LANG", "en"),
    )
