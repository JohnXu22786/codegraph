"""Core data structures shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


class SymbolKind:
    """Stable kind strings stored in the database."""

    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    INTERFACE = "interface"
    TYPE = "type"

    ALL = (FUNCTION, METHOD, CLASS, INTERFACE, TYPE)


@dataclass
class SymbolRec:
    """One code symbol (function / method / class / interface / type alias)."""

    kind: str
    name: str
    qualname: str
    parent: str
    start: int  # 1-based line where the declaration starts
    end: int  # 1-based line where the declaration's body ends
    signature: str
    doc: str = ""


@dataclass
class CallRec:
    """One call site inside the scanned file."""

    caller: str  # qualname of the enclosing symbol, or "" at module level
    callee: str  # raw target text, e.g. "pkg.pricing.price" or "self._items.append"
    line: int  # 1-based line of the call site


@dataclass
class ImportRec:
    """One import / require / use statement."""

    module: str  # module identifier as written, e.g. "pkg.pricing", "./util", "std::fmt"
    names: list = field(default_factory=list)  # imported member names, if any
    kind: str = ""  # "module" | "from" | "import" | "require" | "use" | "mod"
    line: int = 0


@dataclass
class FileScan:
    """Everything extracted from a single source file."""

    lang: str
    module: str = ""  # dotted (python) or slash-separated (other) module id
    symbols: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    imports: list = field(default_factory=list)
