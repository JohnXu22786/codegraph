"""Project configuration: discovery, file format, environment overrides."""

from __future__ import annotations

import errno
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = "codegraph.json"
ENV_PREFIX = "CODEGRAPH_"
IS_WINDOWS = os.name == "nt"


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


def load_config(root=None, config_path=None, db_path=None) -> ProjectConfig:
    """Resolve the effective config: flags > environment > file > defaults."""
    cwd = Path.cwd()
    env_root = os.environ.get(ENV_PREFIX + "ROOT")
    base_root = Path(root or env_root or cwd).resolve()
    root_is_scoped = bool(root or env_root)
    cfg = default_config(base_root)

    cfg_file = Path(config_path) if config_path is not None else Path(cfg.root) / CONFIG_NAME
    if config_path is not None and not cfg_file.is_file():
        raise FileNotFoundError(f"configuration file not found: {cfg_file}")
    if cfg_file.is_file():
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(
                f'configuration file must contain a JSON object: {cfg_file}'
            )
        if "root" in data and not root_is_scoped:
            if not isinstance(data["root"], str):
                raise ValueError(
                    'config field "root" must be a string, got '
                    f'{data["root"]!r}'
                )
            cfg.root = str(
                Path(data["root"]).resolve()
                if Path(data["root"]).is_absolute()
                else (cfg_file.parent / data["root"]).resolve()
            )
        for key in ("include", "exclude", "max_file_kb", "incremental", "engine",
                    "language_map"):
            if key in data:
                if key == "language_map":
                    if not isinstance(data[key], dict):
                        raise ValueError(
                            'config field "language_map" must be an object, got '
                            f'{data[key]!r}'
                        )
                    if any(not isinstance(value, str) for value in data[key].values()):
                        raise ValueError(
                            'config field "language_map" must map strings to strings'
                        )
                setattr(cfg, key, data[key])
        if "incremental" in data and not isinstance(cfg.incremental, bool):
            raise ValueError(
                'config field "incremental" must be a boolean, got '
                f'{cfg.incremental!r}'
            )
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
    if db_path is not None:
        cfg.db_path = db_path
    if os.environ.get(ENV_PREFIX + "MAX_FILE_KB"):
        cfg.max_file_kb = int(os.environ[ENV_PREFIX + "MAX_FILE_KB"])
    if os.environ.get(ENV_PREFIX + "ENGINE"):
        cfg.engine = os.environ[ENV_PREFIX + "ENGINE"]

    if isinstance(cfg.max_file_kb, bool) or not isinstance(cfg.max_file_kb, int):
        raise ValueError(
            'config field "max_file_kb" must be an integer, got '
            f'{cfg.max_file_kb!r}'
        )

    # relative paths are anchored at the project root
    if not isinstance(cfg.db_path, str):
        raise ValueError(
            'config field "db_path" must be a string, got '
            f'{cfg.db_path!r}'
        )
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
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root,
            prefix=f".{CONFIG_NAME}.", delete=False,
        ) as config_file:
            temp_path = Path(config_file.name)
            config_file.write(content)
            config_file.flush()
            os.fsync(config_file.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            raise FileExistsError(
                f"configuration already exists: {path}"
            ) from None
        except OSError as exc:
            unsupported = {
                errno.EACCES, errno.EPERM, errno.EXDEV, errno.ENOSYS,
            }
            unsupported.update(
                getattr(errno, name)
                for name in ("ENOTSUP", "EOPNOTSUPP")
                if hasattr(errno, name)
            )
            no_windows_link_privilege = getattr(exc, "winerror", None) == 1314
            if exc.errno not in unsupported and not no_windows_link_privilege:
                raise
            if os.path.lexists(path):
                raise FileExistsError(
                    f"configuration already exists: {path}"
                ) from None
            if IS_WINDOWS:
                try:
                    os.rename(temp_path, path)
                except FileExistsError:
                    raise FileExistsError(
                        f"configuration already exists: {path}"
                    ) from None
            else:
                raise OSError(
                    "cannot safely create configuration without replacing "
                    f"an existing file: {path}"
                ) from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path
