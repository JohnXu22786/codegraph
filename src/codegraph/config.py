"""Project configuration: discovery, file format, environment overrides."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "codegraph.json"
ENV_PREFIX = "CODEGRAPH_"

DEFAULT_EXCLUDES = [
    ".git",
    ".hg",
    ".svn",
    ".cg",  # this tool's own data directory
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    ".tox",
    ".pytest_cache",
    "coverage",
    "*.min.js",
    "*.min.css",
    "*.lock",
]


@dataclass
class ProjectConfig:
    """Effective settings for one indexing run."""

    root: str  # absolute path of the project being indexed
    db_path: str  # absolute path of the SQLite index database
    include: list = field(default_factory=list)  # if non-empty, only paths matching these stay
    exclude: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    max_file_kb: int = 512  # files larger than this are skipped
    incremental: bool = True  # skip files whose content hash is unchanged
    engine: str = "auto"  # "auto" | "quick" | "deep"
    language_map: dict = field(default_factory=dict)  # extra extension -> language entries


def default_config(root) -> ProjectConfig:
    """Build a config with built-in defaults for ``root`` (no file, no env)."""
    root_abs = str(Path(root).resolve())
    return ProjectConfig(
        root=root_abs,
        db_path=str(Path(root_abs) / ".cg" / "cg.sqlite"),
    )


def load_config(root=None, config_path=None) -> ProjectConfig:
    """Resolve the effective config: flags > environment > file > defaults."""
    cwd = Path.cwd()
    base_root = Path(root or os.environ.get(ENV_PREFIX + "ROOT") or cwd).resolve()
    cfg = default_config(base_root)

    cfg_file = Path(config_path) if config_path else Path(cfg.root) / CONFIG_NAME
    if cfg_file.is_file():
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        if "root" in data:
            cfg.root = str(Path(data["root"]).resolve() if Path(data["root"]).is_absolute()
                           else (cfg_file.parent / data["root"]).resolve())
        for key in ("include", "exclude", "max_file_kb", "incremental", "engine",
                    "language_map"):
            if key in data:
                setattr(cfg, key, data[key])
        if "db_path" in data:
            cfg.db_path = data["db_path"]
        elif cfg.root != str(base_root):
            cfg.db_path = str(Path(cfg.root) / ".cg" / "cg.sqlite")

    # a string include/exclude (e.g. "src" instead of ["src"]) would be iterated
    # character-by-character by the walker; fail fast so the user notices
    for key, fallback in (("include", []), ("exclude", DEFAULT_EXCLUDES)):
        if not isinstance(getattr(cfg, key), list) or \
                any(not isinstance(p, str) for p in getattr(cfg, key)):
            if cfg_file.is_file() and key in data:
                raise ValueError(
                    f'config field "{key}" must be a list of strings, got '
                    f'{getattr(cfg, key)!r}'
                )
            setattr(cfg, key, fallback)

    # environment overrides beat the file
    if os.environ.get(ENV_PREFIX + "DB"):
        cfg.db_path = os.environ[ENV_PREFIX + "DB"]
    if os.environ.get(ENV_PREFIX + "MAX_FILE_KB"):
        cfg.max_file_kb = int(os.environ[ENV_PREFIX + "MAX_FILE_KB"])
    if os.environ.get(ENV_PREFIX + "ENGINE"):
        cfg.engine = os.environ[ENV_PREFIX + "ENGINE"]

    # relative paths are anchored at the project root
    db = Path(cfg.db_path)
    if not db.is_absolute():
        db = Path(cfg.root) / db
    cfg.db_path = str(db.resolve())

    if cfg.max_file_kb <= 0:
        cfg.max_file_kb = 512
    if cfg.engine not in ("auto", "quick", "deep"):
        cfg.engine = "auto"
    return cfg


def write_default_config(root) -> Path:
    """Write a starter codegraph.json next to the project root; returns its path."""
    root = Path(root)
    path = root / CONFIG_NAME
    payload = {
        "root": ".",
        "include": [],
        "exclude": DEFAULT_EXCLUDES,
        "max_file_kb": 512,
        "incremental": True,
        "engine": "auto",
        "language_map": {},
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path
