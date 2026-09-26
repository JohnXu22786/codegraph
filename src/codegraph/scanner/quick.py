"""Dependency-free scanner: regular-expression based AST approximation.

This scanner needs no third-party packages. It extracts the structural
skeleton of a source file — imports, declarations, call sites — with
per-language patterns. Precision is lower than a real parser (no string /
comment awareness, single-line signatures only), which is why the tree-sitter
based scanner is preferred automatically when the optional grammars are
installed. Both scanners emit the same data shape.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize

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
RE_PY_IMP_START = re.compile(r"^[ \t]*import\b")
PY_DOTTED_MODULE = r"\w+(?:\s*\.\s*\w+)*"
RE_PY_IMP_MODULE_NAME = re.compile(PY_DOTTED_MODULE)
RE_PY_IMP_MODULE = re.compile(
    rf"^[ \t]*import\s+({PY_DOTTED_MODULE}(?:\s+as\s+\w+)?"
    rf"(?:\s*,\s*{PY_DOTTED_MODULE}(?:\s+as\s+\w+)?)*)")
RE_PY_IMP_FROM_START = re.compile(
    r"^[ \t]*from[ \t]+([\w.]+)[ \t]+import\b"
)


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


def _imports_python_heuristic(text):
    imports = []
    lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", text)
    idx = 0
    while idx < len(lines):
        if not RE_PY_IMP_START.match(lines[idx]):
            idx += 1
            continue
        start = idx
        statement = []
        while idx < len(lines):
            segment = lines[idx].rstrip("\r\n").split("#", 1)[0]
            continued = segment.endswith("\\")
            statement.append(segment[:-1] if continued else segment)
            idx += 1
            if not continued:
                break
        m = RE_PY_IMP_MODULE.match("".join(statement))
        if m:
            line = start + 1
            for item in m.group(1).split(","):
                name = RE_PY_IMP_MODULE_NAME.match(item.strip())
                if name:
                    module = re.sub(r"\s+", "", name.group(0))
                    imports.append(ImportRec(module, [], "module", line))
    source_lines = text.splitlines()
    idx = 0
    while idx < len(source_lines):
        m = RE_PY_IMP_FROM_START.match(source_lines[idx])
        if not m:
            idx += 1
            continue
        start = idx
        first = source_lines[idx][m.end():].split("#", 1)[0].rstrip()
        parenthesized = first.lstrip().startswith("(")
        depth = 0
        parts = []
        while idx < len(source_lines):
            raw = source_lines[idx][m.end():] if idx == start else source_lines[idx]
            code = raw.split("#", 1)[0].rstrip()
            if parenthesized:
                if code.endswith("\\"):
                    code = code[:-1]
                complete = False
                for pos, char in enumerate(code):
                    if char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                        if depth == 0:
                            code = code[:pos + 1]
                            complete = True
                            break
                parts.append(code)
                idx += 1
                if complete:
                    break
            else:
                continued = code.endswith("\\")
                parts.append(code[:-1] if continued else code)
                idx += 1
                if not continued:
                    break

        names_text = "\n".join(parts).strip()
        if parenthesized:
            if not (names_text.startswith("(") and names_text.endswith(")")):
                continue
            names_text = names_text[1:-1]
        names = [x.strip() for x in names_text.split(",") if x.strip()]
        if names or parenthesized:
            imports.append(ImportRec(m.group(1), names, "from", start + 1))
    return imports


def _imports_from_ast(tree, line_offset=0):
    nodes = sorted(
        (node for node in ast.walk(tree)
         if isinstance(node, (ast.Import, ast.ImportFrom))),
        key=lambda node: (node.lineno, node.col_offset),
    )
    imports = []
    for node in nodes:
        line = node.lineno + line_offset
        if isinstance(node, ast.Import):
            imports.extend(
                ImportRec(alias.name, [], "module", line)
                for alias in node.names
            )
        else:
            module = "." * node.level + (node.module or "")
            names = [
                alias.name if alias.asname is None
                else f"{alias.name} as {alias.asname}"
                for alias in node.names
            ]
            imports.append(ImportRec(module, names, "from", line))
    return imports


def _imports_python_tokenized(text):
    imports = []
    statement = []
    source_lines = text.splitlines(keepends=True)

    def append_statement():
        nonlocal statement
        tokens = [
            token for token in statement
            if token.type not in (tokenize.COMMENT, tokenize.NL)
        ]
        statement = []
        if not tokens:
            return
        if tokens[0].type != tokenize.NAME or tokens[0].string not in ("import", "from"):
            depth = 0
            colon_idx = None
            for idx, token in enumerate(tokens):
                if token.type != tokenize.OP:
                    continue
                if token.string in ("(", "[", "{"):
                    depth += 1
                elif token.string in (")", "]", "}"):
                    depth = max(0, depth - 1)
                elif token.string == ":" and depth == 0:
                    colon_idx = idx
                    break
            if colon_idx is None:
                return
            import_idx = colon_idx + 1
            if import_idx >= len(tokens) or tokens[import_idx].type != tokenize.NAME:
                return
            if tokens[import_idx].string not in ("import", "from"):
                return
            tokens = tokens[import_idx:]

        start_line, start_col = tokens[0].start
        end_line, end_col = tokens[-1].end
        if start_line == end_line:
            source = source_lines[start_line - 1][start_col:end_col]
        else:
            source = (
                source_lines[start_line - 1][start_col:]
                + "".join(source_lines[start_line:end_line - 1])
                + source_lines[end_line - 1][:end_col]
            )
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return
        imports.extend(_imports_from_ast(tree, start_line - 1))

    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT):
                continue
            if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                append_statement()
            elif token.type == tokenize.OP and token.string == ";":
                append_statement()
            else:
                statement.append(token)
    except (IndentationError, SyntaxError, tokenize.TokenError):
        append_statement()
    return imports


def _imports_python(text):
    if ";" not in text:
        return _imports_python_heuristic(text)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _imports_python_tokenized(text)
    return _imports_from_ast(tree)


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
    r"^[ \t]*import\s+(?:([^'\";]+?)\s+from\s+)?['\"]([^'\"]+)['\"]", re.M)
RE_JS_REQUIRE = re.compile(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)")
RE_JS_REQ_NAMES = re.compile(r"(?:const|let|var)\s*\{?\s*([^=\n]*?)\s*\}?\s*=\s*require")
RE_JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _strip_js_comment(line: str) -> str:
    """Remove a trailing // comment (keeps the caller's original line intact)."""
    return re.split(r"//", line, maxsplit=1)[0] if "//" in line else line


def _skip_js_trivia(text, index):
    while index < len(text):
        if text[index].isspace():
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index + 2)
            if newline < 0:
                return len(text)
            index = newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return len(text)
            index = end + 2
        else:
            break
    return index


def _skip_js_quoted(text, index):
    quote = text[index]
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == quote:
            return index + 1
        else:
            index += 1
    return len(text)


def _read_js_import_specifier(text, index):
    index = _skip_js_trivia(text, index)
    if index >= len(text) or text[index] not in "'\"`":
        return None
    quote = text[index]
    start = index + 1
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif quote == "`" and text.startswith("${", index):
            return None
        elif text[index] == quote:
            specifier = text[start:index]
            if not specifier:
                return None
            boundary = _skip_js_trivia(text, index + 1)
            return specifier if boundary < len(text) and text[boundary] in ",)" else None
        else:
            index += 1
    return None


def _skip_js_regex(text, index):
    index += 1
    in_class = False
    while index < len(text) and text[index] not in "\r\n":
        if text[index] == "\\":
            index += 2
        elif text[index] == "[":
            in_class = True
            index += 1
        elif text[index] == "]" and in_class:
            in_class = False
            index += 1
        elif text[index] == "/" and not in_class:
            index += 1
            while index < len(text) and text[index].isalpha():
                index += 1
            return index
        else:
            index += 1
    return index


def _is_jsx_tag_start(text, index):
    if index + 1 >= len(text):
        return False
    char = text[index + 1]
    if char in "/>":
        return True
    return char in "_$" or char.isidentifier()


def _scan_jsx_tag(text, index, imports, jsx):
    closing = text.startswith("</", index)
    index += 2 if closing else 1
    if index < len(text) and text[index] == ">":
        return index + 1, False, closing
    while index < len(text):
        char = text[index]
        if char in "'\"`":
            index = _skip_js_quoted(text, index)
        elif char == "{":
            index = _scan_js_dynamic_imports_code(
                text, index + 1, imports, template_expression=True, jsx=jsx
            )
        elif text.startswith("/>", index):
            return index + 2, True, closing
        elif char == ">":
            return index + 1, False, closing
        else:
            index += 1
    return len(text), True, closing


def _skip_jsx_element(text, index, imports, jsx):
    index, self_closing, closing = _scan_jsx_tag(text, index, imports, jsx)
    if self_closing or closing:
        return index
    while index < len(text):
        if text.startswith("</", index):
            index, _, _ = _scan_jsx_tag(text, index, imports, jsx)
            return index
        if text[index] == "{":
            index = _scan_js_dynamic_imports_code(
                text, index + 1, imports, template_expression=True, jsx=jsx
            )
        elif text[index] == "<" and _is_jsx_tag_start(text, index):
            index = _skip_jsx_element(text, index, imports, jsx)
        else:
            index += 1
    return index


def _find_js_closing_paren(text, index):
    depth = 0
    while index < len(text):
        char = text[index]
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
        elif char in "'\"`":
            index = _skip_js_quoted(text, index)
        elif char == "(":
            depth += 1
            index += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return index
            index += 1
        else:
            index += 1
    return None


def _looks_like_ts_generic_arrow(text, index):
    cursor = index + 1
    angle_depth = brace_depth = paren_depth = bracket_depth = 0
    has_parameter_comma = False
    while cursor < len(text):
        cursor = _skip_js_trivia(text, cursor)
        if cursor >= len(text):
            return False
        char = text[cursor]
        if char in "'\"`":
            cursor = _skip_js_quoted(text, cursor)
        elif char == "{":
            brace_depth += 1
            cursor += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
            cursor += 1
        elif char == "(":
            paren_depth += 1
            cursor += 1
        elif char == ")" and paren_depth:
            paren_depth -= 1
            cursor += 1
        elif char == "[":
            bracket_depth += 1
            cursor += 1
        elif char == "]" and bracket_depth:
            bracket_depth -= 1
            cursor += 1
        elif char == "<":
            angle_depth += 1
            cursor += 1
        elif char == ">" and cursor and text[cursor - 1] == "=":
            cursor += 1
        elif char == ">" and angle_depth:
            angle_depth -= 1
            cursor += 1
        elif char == ">":
            header = text[index + 1:cursor]
            if not has_parameter_comma and not re.search(r"\bextends\b", header):
                return False
            params = _skip_js_trivia(text, cursor + 1)
            if params >= len(text) or text[params] != "(":
                return False
            end = _find_js_closing_paren(text, params)
            if end is None:
                return False
            arrow = _skip_js_trivia(text, end + 1)
            return text.startswith("=>", arrow)
        elif char == "," and not (brace_depth or paren_depth or bracket_depth or angle_depth):
            has_parameter_comma = True
            cursor += 1
        else:
            cursor += 1
    return False


def _scan_js_dynamic_imports_code(
    text, index, imports, template_expression=False, jsx=False
):
    regex_prefixes = {
        "", "(", "[", "{", "=", ":", ",", ";", "!", "?", "new", "extends",
        "return",
        "throw", "case", "delete", "void", "typeof", "instanceof", "in",
        "of", "yield", "await", "else", "do", "control)", "block}",
        "+", "-", "*", "/", "%", "&", "|", "^", "~", "<", ">",
        "=>", "&&", "||", "??",
    }
    control_parens = {"if", "while", "for", "with", "switch", "catch"}
    jsx_prefixes = regex_prefixes | {"default", "new"}
    paren_stack = []
    brace_stack = []
    class_pending = False
    class_header_parens = 0
    brace_depth = 0
    previous = ""
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        if template_expression and char == "}":
            if not brace_depth:
                return index + 1
            brace_depth -= 1
        if char in "'\"":
            index = _skip_js_quoted(text, index)
            previous = "literal"
            continue
        if char == "`":
            index = _skip_js_template(text, index, imports, jsx)
            previous = "literal"
            continue
        if char == "/" and previous in regex_prefixes:
            index = _skip_js_regex(text, index)
            previous = "literal"
            continue
        if (
            jsx and char == "<" and previous in jsx_prefixes
            and not _looks_like_ts_generic_arrow(text, index)
            and _is_jsx_tag_start(text, index)
        ):
            index = _skip_jsx_element(text, index, imports, jsx)
            previous = "literal"
            continue
        if char in "_$" or char.isalpha():
            end = index + 1
            while end < len(text):
                next_char = text[end]
                if next_char in "_$" or next_char.isalnum() or (
                    ("a" + next_char).isidentifier()
                ):
                    end += 1
                else:
                    break
            word = text[index:end]
            if word == "import" and previous != ".":
                opening = _skip_js_trivia(text, end)
                if opening < len(text) and text[opening] == "(":
                    specifier = _read_js_import_specifier(text, opening + 1)
                    if specifier is not None:
                        imports.append(ImportRec(
                            specifier, [], "import", _line_no(text, index)
                        ))
            if word == "class":
                following = _skip_js_trivia(text, end)
                class_pending = previous != "." and (
                    following >= len(text) or text[following] not in ":("
                )
            previous = word
            index = end
            continue
        if text.startswith("?.", index):
            previous = "."
            index += 2
            continue
        if text.startswith(("++", "--"), index):
            previous = text[index:index + 2]
            index += 2
            continue
        if text.startswith(("=>", "&&", "||", "??"), index):
            previous = text[index:index + 2]
            index += 2
            continue
        if char == "(":
            paren_stack.append(previous in control_parens)
            if class_pending:
                class_header_parens += 1
            previous = char
            index += 1
            continue
        if char == ")":
            control = paren_stack.pop() if paren_stack else False
            if class_pending and class_header_parens:
                class_header_parens -= 1
            previous = "control)" if control else char
            index += 1
            continue
        if char == "{":
            if template_expression:
                brace_depth += 1
            class_body = class_pending and not class_header_parens
            is_block = class_body or previous in {
                "", ";", ")", "control)", "else", "do", "try", "finally",
                "=>", "block}",
            }
            brace_stack.append(is_block)
            if class_body:
                class_pending = False
            previous = char
            index += 1
            continue
        if char == "}":
            is_block = brace_stack.pop() if brace_stack else False
            previous = "block}" if is_block else char
            index += 1
            continue
        if char == ";":
            class_pending = False
        previous = char
        index += 1
    return index


def _skip_js_template(text, index, imports, jsx):
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == "`":
            return index + 1
        elif text.startswith("${", index):
            index = _scan_js_dynamic_imports_code(
                text, index + 2, imports, template_expression=True, jsx=jsx
            )
        else:
            index += 1
    return len(text)


def _imports_javascript_dynamic(text, jsx=False):
    imports = []
    _scan_js_dynamic_imports_code(text, 0, imports, jsx=jsx)
    return imports


def _javascript_import_names(clause):
    names = []
    named = re.search(r"\{([^}]*)\}", clause, re.S)
    prefix = clause[:named.start()] if named else clause
    names.extend(
        name for name in RE_JS_IDENT.findall(prefix)
        if name not in ("as", "type")
    )
    if named:
        for specifier in named.group(1).split(","):
            specifier = specifier.strip()
            if specifier.startswith("type "):
                specifier = specifier[5:].strip()
            alias = re.fullmatch(
                r"([A-Za-z_$][\w$]*)\s+as\s+([A-Za-z_$][\w$]*)",
                specifier,
            )
            if alias:
                names.append(f"{alias.group(1)} as {alias.group(2)}")
            else:
                names.extend(
                    name for name in RE_JS_IDENT.findall(specifier)
                    if name not in ("as", "type")
                )
    return names


def _javascript_require_names(clause):
    names = []
    for binding in clause.strip().strip("{} \t").split(","):
        binding = binding.strip().strip("{} \t")
        if not binding:
            continue
        alias = re.fullmatch(
            r"([A-Za-z_$][\w$]*)\s*:\s*([A-Za-z_$][\w$]*)", binding
        )
        if alias:
            names.append(f"{alias.group(1)} as {alias.group(2)}")
        else:
            names.append(binding)
    return names


def _imports_javascript(text, jsx=False):
    imports = []
    for m in RE_JS_ESM.finditer(text):
        clause = m.group(1) or ""
        names = _javascript_import_names(clause)
        imports.append(ImportRec(m.group(2), names, "import", _line_no(text, m.start())))
    imports.extend(_imports_javascript_dynamic(text, jsx=jsx))
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
            names = _javascript_require_names(stmt.group(1))
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
    jsx = str(rel_path).lower().endswith((".jsx", ".tsx")) if rel_path else False
    return FileScan(lang, module, recs, calls, _imports_javascript(text, jsx=jsx))


# --------------------------------------------------------------------------
# go
# --------------------------------------------------------------------------

RE_GO_FUNC = re.compile(r"^\s*func\s+(\w+)\s*\(([^)]*)\)")
RE_GO_METHOD = re.compile(r"^\s*func\s+\((\w+)\s+\*?(\w+)\)\s+(\w+)\s*\(([^)]*)\)")
RE_GO_TYPE = re.compile(r"^\s*type\s+(\w+)\s+(struct|interface)")
RE_GO_IMP_SINGLE = re.compile(
    r'^[ \t]*import[ \t]+(?:[\w.]+[ \t]+)?"([^"]+)"', re.M
)
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
# Constructors have no return type, so they need a separate declaration
# pattern.  The scanner verifies that the name matches the enclosing class.
RE_JAVA_CONSTRUCTOR = re.compile(
    r"^\s*(?:(?:public|private|protected)\s+)*(\w+)\s*\(([^)]*)\)"
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
    classes = []  # (open_depth, qualname, kind)
    items = []
    for idx, line in enumerate(lines, start=1):
        m = RE_JAVA_CLASS.match(line) or RE_JAVA_INTERFACE.match(line)
        if m:
            parent = classes[-1][1] if classes else ''
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            kind = "interface" if RE_JAVA_INTERFACE.match(line) else "class"
            classes.append((depth, qual, kind))
            items.append((idx, depth, SymbolRec(kind, m.group(1), qual, parent, idx, 0, "")))
            if kind == "class":
                open_brace = line.find("{", m.end())
                if open_brace >= 0:
                    candidate = RE_JAVA_CONSTRUCTOR.match(line[open_brace + 1:])
                    if candidate and candidate.group(1) == m.group(1):
                        items.append((
                            idx, depth + 1,
                            SymbolRec("method", candidate.group(1),
                                      f"{qual}.{candidate.group(1)}", qual, idx, 0,
                                      candidate.group(2).strip()),
                        ))
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
        ctor = None
        if (classes and classes[-1][2] == "class"
                and depth == classes[-1][0] + 1):
            candidate = RE_JAVA_CONSTRUCTOR.match(line)
            if (candidate
                    and candidate.group(1) == classes[-1][1].rsplit(".", 1)[-1]):
                ctor = candidate
        if ctor:
            parent = classes[-1][1]
            qual = f"{parent}.{ctor.group(1)}"
            items.append((idx, depth, SymbolRec("method", ctor.group(1), qual, parent,
                                                idx, 0, ctor.group(2).strip())))
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

RE_RS_USE = re.compile(
    r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?use\s+([^;]+;)", re.M)
RE_RS_MOD = re.compile(
    r"^[ \t]*(?:#\[[^\]]*\]\s*)*"
    r"(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+(\w+)\s*;", re.M)
RE_RS_INLINE_MOD = re.compile(
    r"^[ \t]*(?:#\[[^\]]*\]\s*)*"
    r"(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+((?:r#)?\w+)\s*\{")
RE_RS_FN = re.compile(r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?fn\s+(\w+)\s*\(([^)]*)\)")
RE_RS_TYPE = re.compile(r"^\s*(?:pub\s+)?(struct|enum)\s+(\w+)")
RE_RS_TRAIT = re.compile(r"^\s*(?:pub\s+)?trait\s+(\w+)")
RE_RS_IMPL = re.compile(r"^\s*(?:pub\s+)?(?:unsafe\s+)?impl\b")
RE_RS_IMPL_FOR = re.compile(r"\s+for\s+(?!<\s*')")
RE_RS_CHAR = re.compile(
    r"'(?:[^'\\\n]|\\(?:[nrt0\\'\"]|x[0-9a-fA-F]{2}|u\{[0-9a-fA-F_]+\}))'")


def _rust_use_tree_parts(text):
    parts = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _rust_mask_comments(text, mask_strings=True, preserve_path_strings=False):
    """Blank Rust comments and strings while preserving source positions."""
    chars = list(text)
    index = 0
    block_depth = 0
    line_comment = False
    string = None
    string_keep = False
    raw_end = None

    def blank(start, end):
        if not mask_strings:
            return
        for offset in range(start, end):
            if chars[offset] != "\n":
                chars[offset] = " "

    while index < len(chars):
        if line_comment:
            if chars[index] == "\n":
                line_comment = False
            else:
                chars[index] = " "
                index += 1
            continue
        if block_depth:
            if text.startswith("/*", index):
                blank(index, index + 2)
                block_depth += 1
                index += 2
            elif text.startswith("*/", index):
                blank(index, index + 2)
                block_depth -= 1
                index += 2
            else:
                if chars[index] != "\n":
                    chars[index] = " "
                index += 1
            continue
        if raw_end is not None:
            if text.startswith(raw_end, index):
                blank(index, index + len(raw_end))
                index += len(raw_end)
                raw_end = None
            else:
                blank(index, index + 1)
                index += 1
            continue
        if string is not None:
            if text[index] == "\\" and index + 1 < len(chars):
                if not string_keep:
                    blank(index, index + 2)
                index += 2
            else:
                if not string_keep:
                    blank(index, index + 1)
                if text[index] == string:
                    string = None
                    string_keep = False
                index += 1
            continue
        char_match = RE_RS_CHAR.match(text, index)
        if char_match:
            blank(index, char_match.end())
            index = char_match.end()
            continue
        raw_prefix = None
        if chars[index] == "r":
            raw_prefix = index + 1
        elif text.startswith("br", index):
            raw_prefix = index + 2
        if raw_prefix is not None:
            hash_end = raw_prefix
            while hash_end < len(chars) and chars[hash_end] == "#":
                hash_end += 1
            if hash_end < len(chars) and chars[hash_end] == '"':
                raw_end = '"' + ('#' * (hash_end - raw_prefix))
                blank(index, hash_end + 1)
                index = hash_end + 1
                continue
        if chars[index] == '"':
            string = '"'
            string_keep = bool(
                preserve_path_strings and
                re.search(r"#\[\s*path\s*=\s*$", "".join(chars[:index]))
            )
            if not string_keep:
                blank(index, index + 1)
            index += 1
        elif text.startswith("//", index):
            chars[index:index + 2] = "  "
            line_comment = True
            index += 2
        elif text.startswith("/*", index):
            chars[index:index + 2] = "  "
            block_depth = 1
            index += 2
        else:
            index += 1
    return "".join(chars)


def _rust_use_tree_bindings(text, prefix=""):
    """Flatten a Rust use tree into ``(path, alias)`` bindings."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("{") and text.endswith("}"):
        paths = []
        for part in _rust_use_tree_parts(text[1:-1]):
            paths.extend(_rust_use_tree_bindings(part, prefix))
        return paths

    brace = text.find("{")
    if brace >= 0 and text.endswith("}"):
        head = text[:brace].strip()
        head = re.sub(r"\s*::\s*", "::", head)
        if head.endswith("::"):
            head = head[:-2]
        joined = "::".join(part for part in (prefix, head) if part)
        paths = []
        for part in _rust_use_tree_parts(text[brace + 1:-1]):
            paths.extend(_rust_use_tree_bindings(part, joined))
        return paths

    alias = None
    alias_match = re.match(r"^(.*?)\s+as\s+([A-Za-z_]\w*)$", text)
    if alias_match:
        text = alias_match.group(1).strip()
        alias = alias_match.group(2)
    text = re.sub(r"\s*::\s*", "::", text)
    text = re.sub(r"::\*$", "", text)
    if text in ("self", "*"):
        return [(prefix, alias)] if prefix else []
    path = "::".join(part for part in (prefix, text) if part)
    return [(path, alias)] if path else []


def _rust_use_tree_paths(text, prefix=""):
    """Flatten a Rust use tree into paths that can be resolved separately."""
    return [path for path, _alias in _rust_use_tree_bindings(text, prefix)]


def _imports_rust(text):
    text = _rust_mask_comments(text)
    imports = []
    for m in RE_RS_MOD.finditer(text):
        imports.append(ImportRec(m.group(1), [], "mod", _line_no(text, m.start())))
    for m in RE_RS_USE.finditer(text):
        expression = re.sub(r"//[^\n]*", "", m.group(1))
        expression = re.sub(r"/\*.*?\*/", "", expression, flags=re.S)
        expression = expression.strip().rstrip(";").strip()
        seen = set()
        for module, alias in _rust_use_tree_bindings(expression):
            if module and module not in seen:
                seen.add(module)
                imports.append(ImportRec(
                    module, [alias] if alias else [], "use",
                    _line_no(text, m.start())))
    return imports


def _scan_rust(text, lang, rel_path=None):
    module = languages.module_of(rel_path, lang) if rel_path else ""
    lines = _rust_mask_comments(text).splitlines()
    n = len(lines)
    depth = 0
    containers = []  # (open_depth, kind, qualname)
    items = []
    for idx, line in enumerate(lines, start=1):
        m = RE_RS_INLINE_MOD.match(line)
        if m:
            parent = containers[-1][2] if containers else ""
            qual = f"{parent}.{m.group(1)}" if parent else \
                (f"{module}.{m.group(1)}" if module else m.group(1))
            containers.append((depth, "module", qual))
            # Keep a function declaration when a complete inline module fits
            # on one source line; the usual line-by-line pass skips this line.
            module_depth = 0
            module_close = None
            for pos in range(m.end() - 1, len(line)):
                if line[pos] == "{":
                    module_depth += 1
                elif line[pos] == "}":
                    module_depth -= 1
                    if module_depth == 0:
                        module_close = pos
                        break
            if module_close is not None:
                body = line[m.end():module_close]
                fn = RE_RS_FN.match(body)
                if fn:
                    name = fn.group(1)
                    fn_qual = f"{qual}.{name}"
                    items.append((idx, depth + 1, SymbolRec(
                        "function", name, fn_qual, qual, idx, 0,
                        fn.group(2).strip(),
                    )))
            depth += line.count("{") - line.count("}")
            while containers and depth <= containers[-1][0]:
                containers.pop()
            continue
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
            target = RE_RS_IMPL_FOR.search(line, m.end())
            if target:
                owner_text = _rust_impl_target(line, target.end())
            else:
                start = _rust_impl_type_start(line, m.end())
                owner_text = _rust_impl_target(line, start)
            owner = _rust_impl_owner(owner_text)
            qual = f"{parent}.{owner}" if parent else \
                (f"{module}.{owner}" if module else owner)
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
            kind = "method" if containers and containers[-1][1] in ("impl", "trait") \
                else "function"
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
        m = RE_RS_FN.match(line)
        if m:
            line = line[m.end():]
        for callee in _calls_in_line(line, RUST_EXCLUDE):
            calls.append(CallRec("", callee, idx))
    _assign_callers(calls, recs)
    return FileScan(lang, module, recs, calls, _imports_rust(text))


def _rust_impl_target(line, start):
    angle = paren = bracket = brace = 0
    index = start
    while index < len(line):
        char = line[index]
        top_level = not (angle or paren or bracket or brace)
        if top_level and char == "{":
            return line[start:index].strip()
        if top_level and line.startswith("where", index):
            before = line[index - 1] if index else " "
            after_index = index + len("where")
            after = line[after_index] if after_index < len(line) else " "
            if not (before.isalnum() or before in "_#") and not (
                after.isalnum() or after == "_"
            ):
                return line[start:index].strip()
        if char == "'":
            literal = RE_RS_CHAR.match(line, index)
            if literal:
                index = literal.end()
                continue
        if char == "<" and not brace:
            angle += 1
        elif char == ">" and angle and not brace and not (
            index and line[index - 1] == "-"
        ):
            angle -= 1
        elif char == "(":
            paren += 1
        elif char == ")" and paren:
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]" and bracket:
            bracket -= 1
        elif char == "{":
            brace += 1
        elif char == "}" and brace:
            brace -= 1
        index += 1
    return line[start:].strip()


def _rust_impl_type_start(line, start):
    index = start
    while index < len(line) and line[index].isspace():
        index += 1
    if index == len(line) or line[index] != "<":
        return index

    angle = paren = bracket = brace = 0
    while index < len(line):
        char = line[index]
        if char == "'":
            literal = RE_RS_CHAR.match(line, index)
            if literal:
                index = literal.end()
                continue
        if char == "<" and not brace:
            angle += 1
        elif char == ">" and angle and not brace and not (
            index and line[index - 1] == "-"
        ):
            angle -= 1
            if not angle:
                index += 1
                while index < len(line) and line[index].isspace():
                    index += 1
                return index
        elif char == "(":
            paren += 1
        elif char == ")" and paren:
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]" and bracket:
            bracket -= 1
        elif char == "{":
            brace += 1
        elif char == "}" and brace:
            brace -= 1
        index += 1
    return index


def _rust_impl_owner(target):
    target = " ".join(target.split())
    path = target
    while path.startswith("&"):
        path = re.sub(r"^&\s*(?:'\w+\s*)?(?:mut\s+)?", "", path)
    path = re.sub(r"\s*::\s*", "::", path)
    if path.startswith("::"):
        path = path[2:]
    if path.startswith(("dyn ", "for<", "impl ")):
        return target
    base_path = path.split("<", 1)[0].strip()
    parts = base_path.split("::")
    if all(re.fullmatch(r"(?:r#)?[^\W\d]\w*", part) for part in parts):
        return parts[-1]
    return target


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
