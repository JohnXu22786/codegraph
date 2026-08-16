"""Dependency-free scanner: regular-expression based AST approximation.

This scanner needs no third-party packages. It extracts the structural
skeleton of a source file — imports, declarations, call sites — with
per-language patterns. Precision is lower than a real parser (no string /
comment awareness, single-line signatures only), which is why the tree-sitter
based scanner is preferred automatically when the optional grammars are
installed. Both scanners emit the same data shape.
"""

from __future__ import annotations

import re

from ..models import CallRec, FileScan, ImportRec, SymbolRec
from . import languages

# --------------------------------------------------------------------------
# shared pieces
# --------------------------------------------------------------------------

# a call target: identifier chains like "fmt.Println", "lib::dist", "self.x.y"
CALL_CHAIN = re.compile(r"([A-Za-z_$][\w$]*(?:(?:::|\.)[A-Za-z_$][\w$]*)*)\s*\(")

# note: "self" is intentionally NOT excluded — calls like self.items.append()
# carry the object chain and are resolved by their final segment
PY_EXCLUDE = {
    "def", "class", "if", "elif", "else", "for", "while", "with", "return",
    "import", "from", "raise", "except", "assert", "yield", "lambda", "not",
    "and", "or", "in", "is", "pass", "break", "continue", "del", "global",
    "nonlocal", "try", "finally", "match", "case", "async", "await", "type",
}

JS_EXCLUDE = {
    "function", "if", "for", "while", "switch", "catch", "return", "typeof",
    "instanceof", "delete", "void", "import", "require", "export", "in", "of",
    "do", "else", "try", "finally", "throw", "case", "default", "extends",
    "yield", "await", "async", "class", "const", "let", "var", "static",
    "get", "set", "new", "super", "interface", "type", "enum",
    "namespace", "declare",
}

GO_EXCLUDE = {
    "if", "for", "switch", "func", "go", "defer", "return", "case", "select",
    "range", "var", "const", "type", "import", "package", "else", "break",
    "continue", "fallthrough", "map", "chan", "interface", "struct", "goto",
    "default",
}

JAVA_EXCLUDE = {
    "if", "for", "while", "switch", "return", "new", "try", "catch", "finally",
    "throw", "import", "package", "class", "interface", "extends", "implements",
    "public", "private", "protected", "static", "void", "super", "this",
    "assert", "synchronized", "instanceof", "do", "else", "case", "default",
    "break", "continue", "enum", "record", "sealed", "final", "abstract",
    "native", "volatile", "transient", "strictfp",
}

RUST_EXCLUDE = {
    "if", "for", "while", "fn", "return", "impl", "struct", "enum", "trait",
    "match", "let", "mut", "use", "mod", "pub", "unsafe", "ref", "where",
    "loop", "else", "move", "const", "static", "async", "await",
    "dyn", "type", "in", "union", "crate", "super", "break", "continue",
    "extern", "macro_rules",
}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _calls_in_line(line: str, exclude: set) -> list:
    out = []
    for m in CALL_CHAIN.finditer(line):
        callee = m.group(1)
        head = callee.replace("::", ".").split(".")[0]
        if head in exclude:
            continue
        out.append(callee)
    return out


def _finalize(items, nlines):
    """Turn (start, depth, SymbolRec) triples into ordered SymbolRecs with ends."""
    items = sorted(items, key=lambda t: t[0])
    recs = []
    for i, (start, depth, rec) in enumerate(items):
        end = nlines
        for j in range(i + 1, len(items)):
            if items[j][1] <= depth:
                end = items[j][0] - 1
                break
        rec.end = end
        recs.append(rec)
    return recs


def _assign_callers(calls, recs):
    spans = sorted(recs, key=lambda r: r.start)
    for c in calls:
        for r in spans:
            if r.start <= c.line <= r.end:
                c.caller = r.qualname
    return calls


def _line_no(text, pos) -> int:
    return text.count("\n", 0, pos) + 1


# --------------------------------------------------------------------------
# python
# --------------------------------------------------------------------------

RE_PY_DEF = re.compile(r"^[ \t]*(?:async\s+)?def\s+(\w+)\s*\(([^)]*)\)[^:]*:")
RE_PY_CLASS = re.compile(r"^[ \t]*class\s+(\w+)\s*(?:\([^)]*\))?\s*:")
# note: [ \t] anchors (not \s) so MULTILINE matches cannot cross newlines and
# report the line of a previous blank line
RE_PY_IMP_MODULE = re.compile(r"^[ \t]*import\s+([\w.]+)", re.M)
RE_PY_IMP_FROM = re.compile(r"^[ \t]*from\s+([\w.]+)\s+import\s+(.+)$", re.M)


def _python_doc(lines, header_idx):
    """Docstring right after a def/class header (1-based header line)."""
    if header_idx >= len(lines):
        return ""
    j = header_idx
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j >= len(lines):
        return ""
    line = lines[j].lstrip()
    if not (line.startswith('"""') or line.startswith("'''")):
        return ""
    quote = '"""' if line.startswith('"""') else "'''"
    body = line[len(quote):]
    k = j
    while quote not in body and k + 1 < len(lines):
        k += 1
        body += "\n" + lines[k].lstrip()
    doc = body.split(quote, 1)[0]
    return next((x.strip() for x in doc.splitlines() if x.strip()), "")


def _imports_python(text):
    imports = []
    for m in RE_PY_IMP_MODULE.finditer(text):
        imports.append(ImportRec(m.group(1), [], "module", _line_no(text, m.start())))
    for m in RE_PY_IMP_FROM.finditer(text):
        names = [x.strip() for x in m.group(2).strip("()").split(",") if x.strip()]
        imports.append(ImportRec(m.group(1), names, "from", _line_no(text, m.start())))
    return imports


def _python_body_end(lines, start, indent):
    """Last line of a python declaration body starting at 1-based ``start``."""
    end = start
    for idx in range(start, len(lines)):  # 0-based index of line after header
        line = lines[idx]
        if not line.strip() or line.lstrip().startswith("#"):
            end = idx + 1  # blank / comment lines stay inside the body
            continue
        if _indent(line) > indent:
            end = idx + 1
        else:
            break
    return end


def _scan_python(text, lang, rel_path=None):
    module = languages.module_of(rel_path, lang) if rel_path else ""
    lines = text.splitlines()
    n = len(lines)
    stack = []  # (indent, kind, name, qualname)
    items = []  # (start, depth, SymbolRec)
    for idx, line in enumerate(lines, start=1):
        m = RE_PY_CLASS.match(line)
        if m:
            indent = _indent(line)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            name = m.group(1)
            parent = stack[-1][3] if stack else ""
            qual = f"{parent}.{name}" if parent else (f"{module}.{name}" if module else name)
            stack.append((indent, "class", name, qual))
            items.append((idx, indent, SymbolRec("class", name, qual, parent, idx, 0, "")))
            continue
        m = RE_PY_DEF.match(line)
        if m:
            indent = _indent(line)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            name = m.group(1)
            parent = stack[-1][3] if stack else ""
            kind = "method" if (stack and stack[-1][1] == "class") else "function"
            qual = f"{parent}.{name}" if parent else (f"{module}.{name}" if module else name)
            stack.append((indent, kind, name, qual))
            items.append((idx, indent, SymbolRec(kind, name, qual, parent, idx, 0,
                                                 m.group(2).strip())))
            continue
    recs = _finalize(items, n)
    for r in recs:
        r.doc = _python_doc(lines, r.start)
        r.end = _python_body_end(lines, r.start, _indent(lines[r.start - 1]))
    calls = []
    for idx, line in enumerate(lines, start=1):
        m = RE_PY_DEF.match(line) or RE_PY_CLASS.match(line)
        if m:
            line = line[m.end():]
        for callee in _calls_in_line(line, PY_EXCLUDE):
            calls.append(CallRec("", callee, idx))
    _assign_callers(calls, recs)
    return FileScan(lang, module, recs, calls, _imports_python(text))


# --------------------------------------------------------------------------
# javascript / typescript
# --------------------------------------------------------------------------

RE_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)")
RE_JS_FUNC = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)")
RE_JS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?"
    r"(?:\(([^)]*)\)|\w+)\s*=>")
RE_JS_INTERFACE = re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)")
RE_JS_TYPE = re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*=")
# method-like lines: modifiers? name( params ) [optional return type] { body
# (the '{' may carry a one-line body; trailing comments are stripped first).
# A bare statement call like `helper(x)` has no '{' and never matches.
RE_JS_METHOD = re.compile(
    r"^\s{2,}(?:(?:public|private|protected|static|readonly|async|get|set|"
    r"abstract|override|declare)\s+)*(\w+)\s*\(([^)]*)\)[^{]*\{")
# words that can never be method names (get/set/require are legitimate ones)
JS_RESERVED_NAMES = {
    "if", "for", "while", "switch", "catch", "return", "function", "class",
    "import", "export", "const", "let", "var", "new", "delete", "typeof",
    "instanceof", "in", "of", "do", "else", "try", "finally", "throw",
    "case", "default", "extends", "yield", "await", "async", "interface",
    "type", "enum", "namespace", "declare", "super", "this", "void",
}
RE_JS_ESM = re.compile(
    r"^[ \t]*import\s+(?:([^'\"\n;]+?)\s+from\s+)?['\"]([^'\"]+)['\"]", re.M)
RE_JS_REQUIRE = re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)")
RE_JS_REQ_NAMES = re.compile(r"(?:const|let|var)\s*\{?\s*([^=\n]*?)\s*\}?\s*=\s*require")
RE_JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _strip_js_comment(line: str) -> str:
    """Remove a trailing // comment (keeps the caller's original line intact)."""
    return re.split(r"//", line, maxsplit=1)[0] if "//" in line else line


def _imports_javascript(text):
    imports = []
    for m in RE_JS_ESM.finditer(text):
        clause = m.group(1) or ""
        names = [x for x in RE_JS_IDENT.findall(clause) if x != "as"]
        imports.append(ImportRec(m.group(2), names, "import", _line_no(text, m.start())))
    # pair each require(...) with the *nearest preceding* binding statement;
    # searching from 0 would mis-bind names in files with several requires
    stmts = list(RE_JS_REQ_NAMES.finditer(text))
    for m in RE_JS_REQUIRE.finditer(text):
        names = []
        stmt = None
        for n in stmts:
            if n.start() < m.start():
                stmt = n
            else:
                break
        if stmt is not None:
            names = [x.strip() for x in stmt.group(1).split(",") if x.strip()]
        imports.append(ImportRec(m.group(1), names, "require", _line_no(text, m.start())))
    return imports


def _scan_javascript(text, lang, rel_path=None):
    module = languages.module_of(rel_path, lang) if rel_path else ""
    lines = text.splitlines()
    n = len(lines)
    depth = 0
    containers = []  # (open_depth, qualname) for class blocks
    items = []
    decl_pats = (RE_JS_CLASS, RE_JS_FUNC, RE_JS_ARROW, RE_JS_INTERFACE,
                 RE_JS_TYPE, RE_JS_METHOD)
    for idx, line in enumerate(lines, start=1):
        m = RE_JS_CLASS.match(line)
        if m:
            parent = containers[-1][1] if containers else ''
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            containers.append((depth, qual))
            items.append((idx, depth, SymbolRec("class", m.group(1), qual, parent, idx, 0, "")))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        m = RE_JS_INTERFACE.match(line) or RE_JS_TYPE.match(line)
        if m:
            parent = containers[-1][1] if containers else ''
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            kind = "interface" if line.lstrip().startswith(("interface", "export interface")) \
                else "type"
            items.append((idx, depth, SymbolRec(kind, m.group(1), qual, parent, idx, 0, "")))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        m = RE_JS_METHOD.match(_strip_js_comment(line))
        if m and containers and m.group(1) not in JS_RESERVED_NAMES:
            parent = containers[-1][1]
            qual = f"{parent}.{m.group(1)}"
            items.append((idx, depth, SymbolRec("method", m.group(1), qual, parent, idx, 0,
                                                m.group(2).strip())))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        m = RE_JS_FUNC.match(line) or RE_JS_ARROW.match(line)
        if m:
            parent = containers[-1][1] if containers else ''
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            sig = m.group(2) if (m.lastindex or 0) >= 2 and m.group(2) is not None else ""
            items.append((idx, depth, SymbolRec("function", m.group(1), qual, parent, idx, 0,
                                                sig)))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        depth += line.count("{") - line.count("}")
        while containers and depth <= containers[-1][0]:
            containers.pop()
    recs = _finalize(items, n)
    calls = []
    for idx, line in enumerate(lines, start=1):
        for pat in decl_pats:
            m = pat.match(line)
            if m:
                # scan the body only; single-line bodies like
                # `m() { this.step() }` keep their call sites
                brace = line.find("{")
                line = line[brace + 1:] if brace >= 0 else line[m.end():]
                break
        for callee in _calls_in_line(line, JS_EXCLUDE):
            calls.append(CallRec("", callee, idx))
    _assign_callers(calls, recs)
    return FileScan(lang, module, recs, calls, _imports_javascript(text))


# --------------------------------------------------------------------------
# go
# --------------------------------------------------------------------------

RE_GO_FUNC = re.compile(r"^\s*func\s+(\w+)\s*\(([^)]*)\)")
RE_GO_METHOD = re.compile(r"^\s*func\s+\((\w+)\s+\*?(\w+)\)\s+(\w+)\s*\(([^)]*)\)")
RE_GO_TYPE = re.compile(r"^\s*type\s+(\w+)\s+(struct|interface)")
RE_GO_IMP_SINGLE = re.compile(r"^[ \t]*import\s+\"([^\"]+)\"", re.M)
RE_GO_IMP_BLOCK = re.compile(r"import\s*\(([^)]*)\)", re.S)


def _imports_go(text):
    imports = []
    seen = set()
    for m in RE_GO_IMP_SINGLE.finditer(text):
        imports.append(ImportRec(m.group(1), [], "module", _line_no(text, m.start())))
        seen.add(m.group(1))
    for m in RE_GO_IMP_BLOCK.finditer(text):
        for mm in re.finditer(r"\"([^\"]+)\"", m.group(1)):
            if mm.group(1) not in seen:
                imports.append(ImportRec(mm.group(1), [], "module",
                                         _line_no(text, m.start() + mm.start())))
    return imports


def _scan_go(text, lang, rel_path=None):
    module = languages.module_of(rel_path, lang, text)
    lines = text.splitlines()
    n = len(lines)
    items = []
    for idx, line in enumerate(lines, start=1):
        m = RE_GO_METHOD.match(line)
        if m:
            parent = f"{module}.{m.group(2)}" if module else m.group(2)
            qual = f"{parent}.{m.group(3)}"
            items.append((idx, 0, SymbolRec("method", m.group(3), qual, parent, idx, 0,
                                            m.group(4).strip())))
            continue
        m = RE_GO_FUNC.match(line)
        if m:
            qual = f"{module}.{m.group(1)}" if module else m.group(1)
            items.append((idx, 0, SymbolRec("function", m.group(1), qual, "", idx, 0,
                                            m.group(2).strip())))
            continue
        m = RE_GO_TYPE.match(line)
        if m:
            qual = f"{module}.{m.group(1)}" if module else m.group(1)
            kind = "interface" if m.group(2) == "interface" else "type"
            items.append((idx, 0, SymbolRec(kind, m.group(1), qual, "", idx, 0, "")))
            continue
    recs = _finalize(items, n)
    calls = []
    for idx, line in enumerate(lines, start=1):
        brace = line.find("{")
        if brace >= 0:
            line = line[brace + 1:]
        for callee in _calls_in_line(line, GO_EXCLUDE):
            calls.append(CallRec("", callee, idx))
    _assign_callers(calls, recs)
    return FileScan(lang, module, recs, calls, _imports_go(text))


# --------------------------------------------------------------------------
# java
# --------------------------------------------------------------------------

RE_JAVA_CLASS = re.compile(
    r"^\s*(?:(?:public|final|abstract|sealed|non-sealed|static|strictfp)\s+)*"
    r"class\s+(\w+)")
RE_JAVA_INTERFACE = re.compile(
    r"^\s*(?:(?:public|static|sealed)\s+)*interface\s+(\w+)")
# method-like lines: modifiers? Type [Type ...] name( params ) [throws ...]
# The type segment may hold several space-separated tokens (generics like
# "Map<String, Integer>"); the method name is the token right before '('.
# Statement calls (System.out.println(x)) fail because their whole chain is
# one token followed directly by '('.
RE_JAVA_METHOD = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract|synchronized|"
    r"native|default|transient|volatile|strictfp)\s+)*"
    r"([\w<>\[\],.?]+(?:\s+[\w<>\[\],.?]+)*)\s+(\w+)\s*\(([^)]*)\)"
    r"\s*(?:throws\s+[\w.,\s]+)?")
# statement keywords that can never introduce a method declaration
_JAVA_STMT_HEADS = ("new", "return", "throw", "switch", "if", "for",
                    "while", "catch", "synchronized")
RE_JAVA_IMP = re.compile(r"^[ \t]*import\s+(?:static\s+)?([\w.*]+)\s*;", re.M)


def _imports_java(text):
    return [ImportRec(m.group(1), [], "import", _line_no(text, m.start()))
            for m in RE_JAVA_IMP.finditer(text)]


def _scan_java(text, lang, rel_path=None):
    module = languages.module_of(rel_path, lang, text)
    lines = text.splitlines()
    n = len(lines)
    depth = 0
    classes = []  # (open_depth, qualname)
    items = []
    for idx, line in enumerate(lines, start=1):
        m = RE_JAVA_CLASS.match(line) or RE_JAVA_INTERFACE.match(line)
        if m:
            parent = classes[-1][1] if classes else ''
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            kind = "interface" if RE_JAVA_INTERFACE.match(line) else "class"
            classes.append((depth, qual))
            items.append((idx, depth, SymbolRec(kind, m.group(1), qual, parent, idx, 0, "")))
            depth += line.count("{") - line.count("}")
            while classes and depth <= classes[-1][0]:
                classes.pop()
            continue
        m = RE_JAVA_METHOD.match(line)
        if m and classes:
            ret = m.group(1)
            if ret.split()[0] in _JAVA_STMT_HEADS:
                m = None  # statement, not a declaration
        if m and classes:
            parent = classes[-1][1]
            qual = f"{parent}.{m.group(2)}"
            items.append((idx, depth, SymbolRec("method", m.group(2), qual, parent, idx, 0,
                                                m.group(3).strip())))
            depth += line.count("{") - line.count("}")
            while classes and depth <= classes[-1][0]:
                classes.pop()
            continue
        depth += line.count("{") - line.count("}")
        while classes and depth <= classes[-1][0]:
            classes.pop()
    recs = _finalize(items, n)
    calls = []
    for idx, line in enumerate(lines, start=1):
        brace = line.find("{")
        if brace >= 0:
            line = line[brace + 1:]
        for callee in _calls_in_line(line, JAVA_EXCLUDE):
            calls.append(CallRec("", callee, idx))
    _assign_callers(calls, recs)
    return FileScan(lang, module, recs, calls, _imports_java(text))


# --------------------------------------------------------------------------
# rust
# --------------------------------------------------------------------------

RE_RS_USE = re.compile(r"^[ \t]*use\s+([A-Za-z_][\w:]*(?:::\{[^;]*)?\s*;?)", re.M)
RE_RS_MOD = re.compile(r"^[ \t]*mod\s+(\w+)\s*;", re.M)
RE_RS_FN = re.compile(r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?fn\s+(\w+)\s*\(([^)]*)\)")
RE_RS_TYPE = re.compile(r"^\s*(?:pub\s+)?(struct|enum)\s+(\w+)")
RE_RS_TRAIT = re.compile(r"^\s*(?:pub\s+)?trait\s+(\w+)")
RE_RS_IMPL = re.compile(r"^\s*(?:pub\s+)?(?:unsafe\s+)?impl\s+(?:<\s*[^>]*\s*>)?\s*(\w+)")


def _imports_rust(text):
    imports = []
    for m in RE_RS_MOD.finditer(text):
        imports.append(ImportRec(m.group(1), [], "mod", _line_no(text, m.start())))
    for m in RE_RS_USE.finditer(text):
        module = m.group(1).split("::{")[0].strip().rstrip(";")
        imports.append(ImportRec(module, [], "use", _line_no(text, m.start())))
    return imports


def _scan_rust(text, lang, rel_path=None):
    module = languages.module_of(rel_path, lang) if rel_path else ""
    lines = text.splitlines()
    n = len(lines)
    depth = 0
    containers = []  # (open_depth, kind, qualname)
    items = []
    for idx, line in enumerate(lines, start=1):
        m = RE_RS_TYPE.match(line)
        if m:
            parent = containers[-1][2] if containers else ""
            qual = f"{parent}.{m.group(2)}" if parent else \
                (f"{module}.{m.group(2)}" if module else m.group(2))
            items.append((idx, depth, SymbolRec("type", m.group(2), qual, parent, idx, 0, "")))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        m = RE_RS_TRAIT.match(line)
        if m:
            parent = containers[-1][2] if containers else ""
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            containers.append((depth, "trait", qual))
            items.append((idx, depth, SymbolRec("interface", m.group(1), qual, parent, idx, 0,
                                                "")))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        m = RE_RS_IMPL.match(line)
        if m:
            parent = containers[-1][2] if containers else ""
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            containers.append((depth, "impl", qual))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        m = RE_RS_FN.match(line)
        if m:
            parent = containers[-1][2] if containers else ""
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            kind = "method" if containers else "function"
            items.append((idx, depth, SymbolRec(kind, m.group(1), qual, parent, idx, 0,
                                                m.group(2).strip())))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
        depth += line.count("{") - line.count("}")
        while containers and depth <= containers[-1][0]:
            containers.pop()
    recs = _finalize(items, n)
    calls = []
    for idx, line in enumerate(lines, start=1):
        brace = line.find("{")
        if brace >= 0:
            line = line[brace + 1:]
        else:
            m = RE_RS_FN.match(line)
            if m:
                line = line[m.end():]
        for callee in _calls_in_line(line, RUST_EXCLUDE):
            calls.append(CallRec("", callee, idx))
    _assign_callers(calls, recs)
    return FileScan(lang, module, recs, calls, _imports_rust(text))


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def quick_scan(text: str, lang: str, rel_path=None) -> FileScan:
    """Scan ``text`` of language ``lang`` and return a FileScan."""
    text = text.lstrip("\ufeff")  # some editors/CI keep a BOM
    if lang == "python":
        return _scan_python(text, lang, rel_path)
    if lang in ("javascript", "typescript"):
        return _scan_javascript(text, lang, rel_path)
    if lang == "go":
        return _scan_go(text, lang, rel_path)
    if lang == "java":
        return _scan_java(text, lang, rel_path)
    if lang == "rust":
        return _scan_rust(text, lang, rel_path)
    raise ValueError(f"quick scanner does not support language {lang!r}")
