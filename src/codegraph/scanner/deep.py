"""Tree-sitter backed scanner (optional dependency).

Used automatically when the grammar packages are importable; falls back to
the regex scanner otherwise. Emits exactly the same FileScan shape as
:mod:`codegraph.scanner.quick`, so the rest of the pipeline is agnostic to
which provider produced the data.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import CallRec, FileScan, ImportRec, SymbolRec
from . import languages
from .quick import (
    _assign_callers,
    _finalize,
    _imports_go,
    _imports_java,
    _imports_javascript,
    _imports_python,
    _imports_rust,
    _rust_impl_owner,
    GO_EXCLUDE,
    JAVA_EXCLUDE,
    JS_EXCLUDE,
    PY_EXCLUDE,
    RUST_EXCLUDE,
)

_GRAMMAR_MODULES = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "go": "tree_sitter_go",
    "java": "tree_sitter_java",
    "rust": "tree_sitter_rust",
}

_GO_SCOPE_NODES = {
    "block", "function_declaration", "method_declaration", "func_literal",
    "for_statement", "if_statement", "expression_switch_statement",
    "type_switch_statement", "select_statement", "expression_case",
    "type_case", "communication_case", "type_spec", "type_alias",
}

_language_cache = {}


def _language(lang, rel_path=None):
    """Return the tree-sitter Language for ``lang`` or None.

    Grammar wheels expose different accessors depending on the package and
    core version: ``language()``, ``language_typescript()``/``language_tsx()``
    (typescript ships two variants), or the older ``get_language()``. For
    TypeScript, the file extension selects the TS or TSX grammar.
    """
    variant = None
    if lang == "typescript":
        suffix = Path(str(rel_path)).suffix.lower() if rel_path else ""
        variant = "tsx" if suffix == ".tsx" else "typescript"
        cache_key = (lang, variant)
    else:
        cache_key = lang
    if cache_key in _language_cache:
        return _language_cache[cache_key]
    result = None
    try:
        import tree_sitter  # noqa: F401  (grammar modules need the core lib)
        mod_name = _GRAMMAR_MODULES.get(lang)
        if mod_name:
            mod = __import__(mod_name, fromlist=["language"])
            if lang == "typescript":
                specific = "language_tsx" if variant == "tsx" else "language_typescript"
                alternate = "language_typescript" if variant == "tsx" else "language_tsx"
                accessors = (specific, "language", alternate, "get_language")
            else:
                accessors = ("language", "language_tsx", "language_typescript",
                             "get_language")
            for attr in accessors:
                if hasattr(mod, attr):
                    result = getattr(mod, attr)()
                    if result is not None:
                        break
    except Exception:
        result = None
    _language_cache[cache_key] = result
    return result


def available() -> bool:
    """True when at least one grammar is usable (best-effort check)."""
    for lang in _GRAMMAR_MODULES:
        if _language(lang) is not None:
            return True
    return False


def supports(lang) -> bool:
    return lang in _GRAMMAR_MODULES and _language(lang) is not None


# --------------------------------------------------------------------------
# per-language node maps: declaration kinds, call sites, import statements
# --------------------------------------------------------------------------

_DECL = {
    "python": {"class_definition": "class", "function_definition": "function"},
    "javascript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    },
    "typescript": {
        "class_declaration": "class",
        "function_declaration": "function",
        "function_signature": "function",
        "method_definition": "method",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
    },
    "go": {"function_declaration": "function", "method_declaration": "method",
           "type_spec": "type", "type_alias": "type"},
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "constructor_declaration": "method",
    },
    "rust": {"function_item": "function", "function_signature_item": "function",
             "struct_item": "type", "enum_item": "type",
             "trait_item": "interface", "impl_item": "impl"},
}

_CALLS = {
    "python": {"call"},
    "javascript": {"call_expression", "new_expression"},
    "typescript": {"call_expression", "new_expression"},
    "go": {"call_expression"},
    "java": {"method_invocation", "object_creation_expression"},
    "rust": {"call_expression"},
}

_IMPORTS = {
    "python": {"import_statement", "import_from_statement"},
    "javascript": {"import_statement"},
    "typescript": {"import_statement"},
    "go": {"import_declaration"},
    "java": {"import_declaration"},
    "rust": {"use_declaration", "mod_item"},
}

# declaration types whose children nest: they push onto the symbol stack
_CONTAINERS = {
    "python": {"class_definition", "function_definition"},
    "javascript": {"class_declaration"},
    "typescript": {"class_declaration"},
    "go": set(),
    "java": {"class_declaration", "interface_declaration"},
    "rust": {"impl_item", "trait_item"},
}

_EXCLUDE = {
    "python": PY_EXCLUDE,
    "javascript": JS_EXCLUDE,
    "typescript": JS_EXCLUDE,
    "go": GO_EXCLUDE,
    "java": JAVA_EXCLUDE,
    "rust": RUST_EXCLUDE,
}

_IMPORT_FNS = {
    "python": _imports_python,
    "javascript": _imports_javascript,
    "typescript": _imports_javascript,
    "go": _imports_go,
    "java": _imports_java,
    "rust": _imports_rust,
}

_FIELD_NAME = {"python": "name", "javascript": "name", "typescript": "name",
               "go": "name", "java": "name", "rust": "name"}
_PARAM_FIELDS = (("parameters",), ("parameters",), ("parameters",),
                 ("parameters",), ("formal_parameters", "parameters"), ("parameters",))


def _node_text(node, source: bytes) -> str:
    start, end = node.byte_range
    return source[start:end].decode("utf-8", "replace")


def _go_generic_function_names(node, source: bytes) -> set[str]:
    names = set()
    for child in node.children:
        if child.type != "function_declaration":
            continue
        if child.child_by_field_name("type_parameters") is None:
            continue
        name = child.child_by_field_name("name")
        if name is not None:
            names.add(_node_text(name, source).strip())
    return names


class _Walker:
    def __init__(self, lang, module, source: bytes, lines):
        self.lang = lang
        self.module = module
        self.source = source
        self.lines = lines
        self.go_generic_functions = set()
        self.go_name_scopes = [set()]
        self.stack = []  # (kind, qualname) of open containers
        self.items = []  # (start, depth, SymbolRec)
        self.raw_calls = []  # (callee, line)
        self.imports = []

    # -- helpers -----------------------------------------------------------

    def _name_of(self, node):
        named = node.child_by_field_name("name")
        if named is not None:
            return _node_text(named, self.source).strip()
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "field_identifier"):
                return _node_text(child, self.source).strip()
        return ""

    def _receiver_type(self, node):
        recv = node.child_by_field_name("receiver")
        if recv is None:
            return ""
        for child in recv.children:
            if child.type == "type_identifier":
                return _node_text(child, self.source).strip()
            if child.type != "parameter_declaration":
                continue
            type_node = child.child_by_field_name("type")
            if type_node is None:
                continue
            return self._type_identifier(type_node)
        return ""

    def _type_identifier(self, node):
        if node.type == "type_identifier":
            return _node_text(node, self.source).strip()
        for child in node.children:
            name = self._type_identifier(child)
            if name:
                return name
        return ""

    def _go_declared_names(self, node):
        if node.type in (
            "parameter_declaration", "var_spec", "const_spec",
            "type_parameter_declaration", "type_spec", "type_alias",
        ):
            return {
                _node_text(name, self.source).strip()
                for name in node.children_by_field_name("name")
                if _node_text(name, self.source).strip() != "_"
            }
        if node.type not in ("short_var_declaration", "range_clause"):
            return set()
        left = node.child_by_field_name("left")
        names = set()
        pending = [left] if left is not None else []
        while pending:
            current = pending.pop()
            if current.type == "identifier":
                name = _node_text(current, self.source).strip()
                if name != "_":
                    names.add(name)
            else:
                pending.extend(current.children)
        return names

    def _go_name_is_bound(self, name):
        return any(name in scope for scope in self.go_name_scopes)

    def _signature_of(self, node):
        for field in ("parameters", "formal_parameters"):
            params = node.child_by_field_name(field)
            if params is not None:
                return _node_text(params, self.source).strip().lstrip("(").rstrip(")")
        return ""

    def _doc_of(self, node):
        block = node.child_by_field_name("body")
        if block is None:
            return ""
        for child in block.children:
            if child.type != "expression_statement":
                continue
            for sub in child.children:
                if sub.type != "string":
                    continue
                raw = _node_text(sub, self.source)
                doc = re.sub(r"^(['\"]{3}|['\"])(.*?)\1$", r"\2", raw, flags=re.S)
                return next((x.strip() for x in doc.splitlines() if x.strip()), "")
            break
        return ""

    # -- main walk ---------------------------------------------------------

    def walk(self, node):
        t = node.type
        decl_kind = _DECL[self.lang].get(t)
        pushed = False
        go_scope = self.lang == "go" and t in _GO_SCOPE_NODES
        if go_scope:
            self.go_name_scopes.append(set())
        inline_module = (
            self.lang == "rust" and t == "mod_item" and
            node.child_by_field_name("body") is not None
        )
        if inline_module:
            name = self._name_of(node)
            if name:
                parent = self.stack[-1][1] if self.stack else ""
                qual = f"{parent}.{name}" if parent else \
                    (f"{self.module}.{name}" if self.module else name)
                self.stack.append(("module", qual))
                pushed = True
        if decl_kind is not None:
            kind, name, doc = self._declare(node, t, decl_kind)
            if name:
                self._emit(kind, name, doc, node)
                if t in _CONTAINERS[self.lang]:
                    self.stack.append((kind, self.items[-1][2].qualname))
                    pushed = True
        if t in _CALLS[self.lang]:
            self._record_call(node)
        if t in _IMPORTS[self.lang] and not inline_module:
            text = _node_text(node, self.source)
            for imp in _IMPORT_FNS[self.lang](text):
                imp.line = node.start_point[0] + 1
                self.imports.append(imp)
        for child in node.children:
            self.walk(child)
        if self.lang == "go":
            names = self._go_declared_names(node)
            self.go_name_scopes[-1].update(names)
        if go_scope:
            self.go_name_scopes.pop()
            if self.lang == "go" and t in ("type_spec", "type_alias"):
                self.go_name_scopes[-1].update(names)
        if pushed:
            self.stack.pop()

    def _declare(self, node, node_type, kind):
        """Return (kind, name, doc) for a declaration node."""
        lang = self.lang
        name = self._name_of(node)
        if not name:
            return None, "", ""
        if lang == "python":
            if node_type == "function_definition":
                kind = "method" if self.stack and self.stack[-1][0] == "class" \
                    else "function"
            return kind, name, self._doc_of(node)
        if lang == "rust":
            if node_type == "impl_item":
                target = node.child_by_field_name("type")
                if target is not None:
                    name = _rust_impl_owner(_node_text(target, self.source))
            if node_type in ("function_item", "function_signature_item"):
                kind = "method" if self.stack and self.stack[-1][0] in (
                    "impl", "interface"
                ) else "function"
            return kind, name, ""
        if lang == "go":
            if node_type == "method_declaration":
                rtype = self._receiver_type(node)
                parent = f"{self.module}.{rtype}" if self.module and rtype else rtype
                qual = f"{parent}.{name}" if parent else name
                start = node.start_point[0] + 1
                rec = SymbolRec("method", name, qual, parent, start, 0,
                                self._signature_of(node))
                self.items.append((start, len(self.stack), rec))
                return None, "", ""
            if node_type == "type_spec":
                type_node = node.child_by_field_name("type")
                if type_node is not None and type_node.type == "interface_type":
                    kind = "interface"
            return kind, name, ""
        if lang == "java" and node_type == "constructor_declaration":
            return kind, name, ""
        return kind, name, ""

    def _emit(self, kind, name, doc, node):
        parent = self.stack[-1][1] if self.stack else ""
        qual = f"{parent}.{name}" if parent else \
            (f"{self.module}.{name}" if self.module else name)
        sig = self._signature_of(node)
        start = node.start_point[0] + 1
        current = node.parent
        default_export = False
        while current is not None:
            if current.type == "export_statement":
                prefix = _node_text(current, self.source).lstrip()
                default_export = prefix.startswith("export default ")
                break
            current = current.parent
        rec = SymbolRec(kind, name, qual, parent, start, 0, sig, doc,
                        default_export)
        self.items.append((start, len(self.stack), rec))

    def _require_imports(self, node, text):
        imports = _imports_javascript(text)
        context = node
        statement_types = {
            "expression_statement", "lexical_declaration", "return_statement",
            "throw_statement", "variable_declaration",
        }
        while context.parent is not None and context.type not in statement_types:
            context = context.parent
        contextual = _imports_javascript(_node_text(context, self.source))
        for imp in imports:
            if imp.kind != "require":
                continue
            matches = [candidate for candidate in contextual
                       if candidate.kind == "require"
                       and candidate.module == imp.module]
            if matches:
                imp.names = matches[-1].names
        return imports

    def _record_call(self, node):
        text = _node_text(node, self.source)
        cut = text.split("(", 1)[0]
        function = node.child_by_field_name("function")
        if self.lang == "typescript" and function is not None \
                and node.child_by_field_name("type_arguments") is not None:
            cut = _node_text(function, self.source)
        elif self.lang == "rust" and function is not None \
                and function.type == "generic_function":
            target = function.child_by_field_name("function")
            if target is not None:
                cut = _node_text(target, self.source)
        elif self.lang == "go" and function is not None:
            # Go grammars may expose foo[T]() as indexing; avoid guessing selectors.
            target = function
            if function.type == "index_expression":
                target = function.child_by_field_name("operand")
            if target is not None and target.type == "identifier" \
                    and (callee := _node_text(target, self.source).strip()) \
                    in self.go_generic_functions \
                    and not self._go_name_is_bound(callee):
                cut = callee
        m = re.search(r"([A-Za-z_$][\w$]*(?:(?:::|\.)[A-Za-z_$][\w$]*)*)$", cut)
        if not m:
            return
        callee = m.group(1)
        head = callee.replace("::", ".").split(".")[0]
        # commonjs require(...) is an import, not a plain call — check this
        # before the generic exclude (require is in the exclude set)
        if head == "require" and self.lang in ("javascript", "typescript"):
            if (
                function is not None
                and function.type == "identifier"
                and _node_text(function, self.source) == "require"
            ):
                for imp in self._require_imports(node, text):
                    imp.line = node.start_point[0] + 1
                    self.imports.append(imp)
            return
        if head == "import" and self.lang in ("javascript", "typescript"):
            if function is not None and function.type == "import":
                for imp in _imports_javascript(text):
                    imp.line = node.start_point[0] + 1
                    self.imports.append(imp)
            return
        if head in _EXCLUDE[self.lang]:
            return
        self.raw_calls.append((callee, node.start_point[0] + 1))


def deep_scan(text: str, lang: str, rel_path=None) -> FileScan:
    """Parse ``text`` with tree-sitter and emit the standard FileScan shape."""
    grammar = _language(lang, rel_path)
    if grammar is None:
        raise RuntimeError(f"no tree-sitter grammar available for {lang!r}")

    import tree_sitter

    text = text.lstrip("\ufeff")
    # grammar wheels expose language() as either a Language or a raw capsule
    # depending on the core version; normalise before constructing the parser
    if not isinstance(grammar, tree_sitter.Language):
        grammar = tree_sitter.Language(grammar)

    source = text.encode("utf-8")
    parser = tree_sitter.Parser(grammar)
    tree = parser.parse(source)
    module = languages.module_of(rel_path, lang, text) if rel_path else ""
    lines = text.splitlines()

    walker = _Walker(lang, module, source, lines)
    if lang == "go":
        walker.go_generic_functions.update(
            _go_generic_function_names(tree.root_node, source)
        )
    for child in tree.root_node.children:
        walker.walk(child)

    recs = _finalize(walker.items, len(lines))
    if lang == "python":
        # match the regex scanner's body trimming so module-level calls after
        # the last declaration are never attributed to it
        from .quick import _indent, _python_body_end

        for r in recs:
            r.end = _python_body_end(lines, r.start, _indent(lines[r.start - 1]))
    calls = [CallRec("", callee, line) for callee, line in walker.raw_calls]
    _assign_callers(calls, recs)
    return FileScan(lang, module, recs, calls, walker.imports)
