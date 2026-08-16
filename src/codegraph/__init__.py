"""codegraph — a code knowledge graph for agent harnesses.

Parses a codebase into a queryable index (SQLite): symbols, call sites and
module imports, plus cross-file resolution, full-text search, impact
analysis and DOT/JSON export. Ships a CLI and a stdio tool server
(MCP-style JSON-RPC) so a plugin harness can load it as a tool provider.
"""

from .builder import IndexReport, build_index
from .cache import TTLCache
from .config import ProjectConfig, load_config
from .queries import (
    query_callers,
    query_callees,
    query_dependents,
    query_deps,
    query_impact,
    query_search,
    query_stats,
)
from .store import IndexStore

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "IndexReport",
    "IndexStore",
    "ProjectConfig",
    "TTLCache",
    "build_index",
    "load_config",
    "query_callers",
    "query_callees",
    "query_dependents",
    "query_deps",
    "query_impact",
    "query_search",
    "query_stats",
]
