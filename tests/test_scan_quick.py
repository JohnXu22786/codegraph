"""Tests for the dependency-free regex scanner (scanner.quick)."""

import unittest

from codegraph.scanner import quick

from .fixtures import PROJ


def _read(rel):
    return (PROJ / rel).read_text(encoding="utf-8")


_EXT_LANG = {".py": "python", ".ts": "typescript", ".js": "javascript",
             ".go": "go", ".java": "java", ".rs": "rust"}


def _scan(rel):
    """Scan a fixture file with its module path attached."""
    lang = _EXT_LANG["." + rel.rsplit(".", 1)[-1]]
    return quick.quick_scan(_read(rel), lang, rel)


class QuickPythonTest(unittest.TestCase):
    def test_cart_symbols(self):
        scan = _scan("pkg/cart.py")
        by_name = {s.name: s for s in scan.symbols}
        self.assertIn("Cart", by_name)
        cart = by_name["Cart"]
        self.assertEqual(cart.kind, "class")
        self.assertEqual(cart.qualname, "pkg.cart.Cart")
        self.assertEqual(cart.parent, "")
        self.assertEqual(cart.doc, "A simple shopping cart.")
        # methods inside the class
        add = by_name["add"]
        self.assertEqual(add.kind, "method")
        self.assertEqual(add.qualname, "pkg.cart.Cart.add")
        self.assertEqual(add.parent, "pkg.cart.Cart")
        total = by_name["total"]
        self.assertEqual(total.qualname, "pkg.cart.Cart.total")
        add = by_name["add"]
        self.assertIn("sku", add.signature)
        # module-level function
        creator = by_name["create_cart"]
        self.assertEqual(creator.kind, "function")
        self.assertEqual(creator.qualname, "pkg.cart.create_cart")

    def test_cart_imports(self):
        scan = _scan("pkg/cart.py")
        modules = sorted((i.module, i.kind) for i in scan.imports)
        self.assertIn(("os", "module"), modules)
        self.assertIn(("pkg", "from"), modules)
        from_pkg = next(i for i in scan.imports if i.module == "pkg")
        self.assertEqual(from_pkg.names, ["pricing"])

    def test_comma_separated_imports(self):
        scan = quick.quick_scan("\nimport os, sys as system\n", "python")
        self.assertEqual(
            [(i.module, i.kind, i.line) for i in scan.imports],
            [("os", "module", 2), ("sys", "module", 2)],
        )

    def test_semicolon_separated_imports(self):
        source = (
            "import os; import sys\n"
            "from package import alpha; from other import beta\n"
        )
        scan = quick.quick_scan(source, "python")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [
                ("os", [], "module", 1),
                ("sys", [], "module", 1),
                ("package", ["alpha"], "from", 2),
                ("other", ["beta"], "from", 2),
            ],
        )

    def test_semicolon_imports_survive_unrelated_syntax_error(self):
        source = (
            "import os; import sys\n"
            "from package import alpha; from other import beta\n"
            "from . import local as alias; from ..pkg import Thing as T\n"
            "def broken(:\n"
        )
        scan = quick.quick_scan(source, "python")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [
                ("os", [], "module", 1),
                ("sys", [], "module", 1),
                ("package", ["alpha"], "from", 2),
                ("other", ["beta"], "from", 2),
                (".", ["local as alias"], "from", 3),
                ("..pkg", ["Thing as T"], "from", 3),
            ],
        )

    def test_inline_semicolon_imports_survive_unrelated_syntax_error(self):
        source = "if enabled: import os; import sys\ndef broken(:\n"
        scan = quick.quick_scan(source, "python")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [("os", [], "module", 1), ("sys", [], "module", 1)],
        )

    def test_backslash_continued_comma_separated_imports(self):
        scan = quick.quick_scan("import os, \\\n    sys as system\n", "python")
        self.assertEqual(
            [(i.module, i.kind, i.line) for i in scan.imports],
            [("os", "module", 1), ("sys", "module", 1)],
        )

    def test_multiline_parenthesized_from_import(self):
        source = "\n".join((
            "# leading comment",
            "from package.submodule import (",
            "    alpha,  # comment containing )",
            "    beta, " + chr(92),
            "    gamma,",
            ")  # trailing comment",
        )) + "\n"
        scan = quick.quick_scan(source, "python")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [("package.submodule", ["alpha", "beta", "gamma"], "from", 2)],
        )

    def test_backslash_continuations_at_other_import_boundaries(self):
        sources = (
            "import \\\n    os, sys as system\n",
            "import os \\\n, sys\n",
        )
        for source in sources:
            with self.subTest(source=source):
                scan = quick.quick_scan(source, "python")
                self.assertEqual(
                    [(i.module, i.kind, i.line) for i in scan.imports],
                    [("os", "module", 1), ("sys", "module", 1)],
                )

    def test_form_feed_whitespace_in_imports(self):
        scan = quick.quick_scan("import\f os,\f sys\n", "python")
        self.assertEqual(
            [(i.module, i.kind, i.line) for i in scan.imports],
            [("os", "module", 1), ("sys", "module", 1)],
        )

    def test_backslash_continuation_preserves_dotted_module_names(self):
        sources = (
            "import foo\\\n.bar\n",
            "import foo.\\\nbar\n",
            "import foo\\\n    .bar\n",
            "import foo.\\\n    bar\n",
        )
        for source in sources:
            with self.subTest(source=source):
                scan = quick.quick_scan(source, "python")
                self.assertEqual([i.module for i in scan.imports], ["foo.bar"])

    def test_cart_calls(self):
        scan = _scan("pkg/cart.py")
        calls = {(c.caller, c.callee) for c in scan.calls}
        self.assertIn(("pkg.cart.Cart.add", "pricing.discount"), calls)
        self.assertIn(("pkg.cart.Cart.total", "pricing.price"), calls)
        self.assertIn(("pkg.cart.create_cart", "Cart"), calls)
        self.assertIn(("pkg.cart.Cart.add", "self._items.append"), calls)
        # no call assigned outside any symbol in this file
        self.assertFalse(any(c.caller == "" for c in scan.calls))

    def test_app(self):
        scan = _scan("app.py")
        names = {s.name for s in scan.symbols}
        self.assertEqual(names, {"main"})
        self.assertEqual(scan.symbols[0].qualname, "app.main")
        modules = [i.module for i in scan.imports]
        self.assertIn("pkg.cart", modules)
        self.assertIn("pkg.pricing", modules)
        from_cart = next(i for i in scan.imports if i.module == "pkg.cart")
        self.assertEqual(sorted(from_cart.names), ["Cart", "create_cart"])
        calls = {(c.callee) for c in scan.calls}
        self.assertIn("create_cart", calls)
        self.assertIn("cart.add", calls)
        # module-level call has no caller
        self.assertIn("", {c.caller for c in scan.calls})

    def test_pricing(self):
        scan = _scan("pkg/pricing.py")
        by_name = {s.name: s for s in scan.symbols}
        self.assertEqual(by_name["price"].qualname, "pkg.pricing.price")
        self.assertEqual(by_name["discount"].doc, "Apply the standing discount.")
        self.assertIn(("pkg.pricing.discount", "price"), {(c.caller, c.callee) for c in scan.calls})

    def test_import_line_numbers_exact(self):
        """Regression: MULTILINE anchors used to misreport the previous
        blank line as the import's line."""
        scan = _scan("pkg/cart.py")
        os_imp = next(i for i in scan.imports if i.module == "os")
        self.assertEqual(os_imp.line, 3)
        from_pkg = next(i for i in scan.imports if i.module == "pkg")
        self.assertEqual(from_pkg.line, 4)
        go = _scan("main.go")
        self.assertEqual(next(i for i in go.imports if i.module == "fmt").line, 3)
        ts = _scan("web/index.ts")
        self.assertEqual(ts.imports[0].line, 1)

    def test_async_decorator_and_nested_class(self):
        src = (
            "import threading\n"
            "async def fetch(url):\n"
            "    return url\n"
            "@decorator\n"
            "class Outer:\n"
            "    class Inner:\n"
            "        def deep(self):\n"
            "            pass\n"
            "    def top(self):\n"
            "        return fetch(self.url)\n"
        )
        scan = quick.quick_scan(src, "python")
        by_name = {s.qualname: s for s in scan.symbols}
        self.assertIn("fetch", by_name)
        self.assertEqual(by_name["fetch"].kind, "function")
        self.assertEqual(by_name["Outer"].kind, "class")
        self.assertEqual(by_name["Outer.Inner"].kind, "class")
        self.assertEqual(by_name["Outer.Inner.deep"].kind, "method")
        self.assertEqual(by_name["Outer.Inner.deep"].parent, "Outer.Inner")
        self.assertEqual(by_name["Outer.top"].kind, "method")
        self.assertIn(("Outer.top", "fetch"), {(c.caller, c.callee) for c in scan.calls})

    def test_keywords_not_reported_as_calls(self):
        src = (
            "def f(x):\n"
            "    if x and not x:\n"
            "        return\n"
            "    for i in range(3):\n"
            "        print(i)\n"
        )
        scan = quick.quick_scan(src, "python")
        callees = [c.callee for c in scan.calls]
        self.assertNotIn("if", callees)
        self.assertNotIn("not", callees)
        self.assertNotIn("return", callees)
        self.assertIn("range", callees)
        self.assertIn("print", callees)


class QuickJavascriptTest(unittest.TestCase):
    def test_esm_imports_and_class(self):
        scan = _scan("web/index.ts")
        by_name = {s.name: s for s in scan.symbols}
        self.assertEqual(by_name["Api"].kind, "class")
        self.assertEqual(by_name["Api"].qualname, "web/index.Api")
        self.assertEqual(by_name["constructor"].parent, "web/index.Api")
        self.assertEqual(by_name["fetch"].kind, "method")
        self.assertEqual(by_name["fetch"].qualname, "web/index.Api.fetch")
        self.assertEqual(by_name["serve"].qualname, "web/index.serve")
        self.assertEqual(by_name["serve"].kind, "function")
        modules = sorted(i.module for i in scan.imports)
        self.assertEqual(modules, ["./logger", "./util"])
        calls = {(c.caller, c.callee) for c in scan.calls}
        self.assertIn(("web/index.Api.fetch", "fmt"), calls)
        self.assertIn(("web/index.Api.fetch", "logger.info"), calls)
        self.assertIn(("web/index.serve", "Api"), calls)
        self.assertIn(("web/index.serve", "api.fetch"), calls)

    def test_multiline_esm_named_import(self):
        src = "import {\n  useFoo,\n} from './foo';\n"
        scan = quick.quick_scan(src, "javascript")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [("./foo", ["useFoo"], "import", 1)],
        )

    def test_esm_named_import_preserves_alias_binding(self):
        scan = quick.quick_scan(
            "import { foo as bar } from './util.js';\n",
            "javascript",
            "app.js",
        )

        self.assertEqual(scan.imports[0].names, ["foo as bar"])

    def test_dynamic_import_is_a_module_dependency(self):
        src = (
            "async function load() {\n"
            "  return await import /* split */ (\n"
            "    /* webpackChunkName: 'dynamic' */\n"
            "    './dynamic.js',\n"
            "    { with: { type: 'json' } }\n"
            "  );\n"
            "}\n"
            "// import('./line-comment.js')\n"
            "/* import('./block-comment.js') */\n"
            "const example = \"import('./string.js')\";\n"
            "const computed = import('./' + name);\n"
            "loader . import('./method.js');\n"
            "const matcher = /import\\('\\.\\/regex\\.js'\\)/;\n"
            "const unaryRegex = +/import('unary-ghost.js')/;\n"
            "const divisionRegex = value / /import('division-ghost.js')/.test(source);\n"
            "const constructor = new /import('new-ghost.js')/.constructor();\n"
            "if (ready) /import\\('\\.\\/control-regex\\.js'\\)/.test(source);\n"
            "if (ready) {} /import\\('\\.\\/block-regex\\.js'\\)/.test(source);\n"
            "class Box {} /import\\('\\.\\/class-regex\\.js'\\)/.test(source);\n"
            "class Invalid extends /import('extends-ghost.js')/ {}\n"
            "const legacy = { class: 1 }\n"
            "const quotient = {} / import('./division.js');\n"
            "const nested = `${await import('./template.js')}`;\n"
        )
        scan = quick.quick_scan(src, "javascript")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [
                ("./dynamic.js", [], "import", 2),
                ("./division.js", [], "import", 22),
                ("./template.js", [], "import", 23),
            ],
        )

    def test_dynamic_import_skips_jsx_text_but_scans_expressions(self):
        src = (
            "const element = component + <p>Run import('ghost.js') for details</p>;\n"
            "async function load() { return await <p>import('await-ghost.js')</p>; }\n"
            "function* generate() { yield <p>import('yield-ghost.js')</p>; }\n"
            "function fail() { throw <p>import('throw-ghost.js')</p>; }\n"
            "const dynamic = <Widget>{import('./child.js')}</Widget>;\n"
            "const attr = <Lazy load={() => import('./attribute.js')} />;\n"
        )
        scan = quick.quick_scan(src, "javascript", "view.jsx")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [
                ("./child.js", [], "import", 5),
                ("./attribute.js", [], "import", 6),
            ],
        )

    def test_dynamic_import_scans_tsx_generic_arrow_and_skips_text(self):
        src = (
            "const generic = <T,>(value: T) => import('./generic.js');\n"
            "const element = <p>import('./tsx-ghost.js')</p>;\n"
        )
        scan = quick.quick_scan(src, "typescript", "view.tsx")
        self.assertEqual(
            [(item.module, item.names, item.kind, item.line) for item in scan.imports],
            [("./generic.js", [], "import", 1)],
        )

    def test_esm_util(self):
        scan = _scan("web/util.ts")
        self.assertEqual(scan.symbols[0].qualname, "web/util.fmt")

    def test_commonjs_require_and_function(self):
        scan = _scan("web/app.js")
        self.assertEqual(scan.symbols[0].qualname, "web/app.greet")
        self.assertEqual(scan.imports[0].module, "./util.js")
        self.assertEqual(scan.imports[0].names, ["fmt"])
        self.assertEqual(scan.imports[0].kind, "require")
        self.assertIn(("web/app.greet", "fmt"), {(c.caller, c.callee) for c in scan.calls})

    def test_commonjs_destructuring_preserves_alias_binding(self):
        scan = quick.quick_scan(
            "const { foo: bar } = require('./util.js');\n",
            "javascript",
            "app.js",
        )

        self.assertEqual(scan.imports[0].names, ["foo as bar"])

    def test_typescript_declaration_function_is_indexed(self):
        scan = quick.quick_scan(
            "export declare function make(): void;\n",
            "typescript",
            "util/index.d.ts",
        )

        self.assertEqual(scan.symbols[0].qualname, "util/index.make")
        self.assertEqual(scan.symbols[0].kind, "function")

    def test_arrow_function_and_interface(self):
        src = (
            "import { b } from './x';\n"
            "export const square = (n: number) => n * n;\n"
            "export interface Point { x: number; y: number }\n"
            "export type Maybe = Point | null;\n"
            "function use() { return square(2); }\n"
        )
        scan = quick.quick_scan(src, "typescript")
        by_name = {s.name: s for s in scan.symbols}
        self.assertEqual(by_name["square"].kind, "function")
        self.assertEqual(by_name["Point"].kind, "interface")
        self.assertEqual(by_name["Maybe"].kind, "type")
        self.assertEqual(by_name["use"].kind, "function")
        self.assertIn("square", [c.callee for c in scan.calls])


class QuickGoJavaRustTest(unittest.TestCase):
    def test_go(self):
        scan = _scan("main.go")
        self.assertEqual(scan.symbols[0].qualname, "main.main")
        self.assertEqual(sorted(i.module for i in scan.imports), ["fmt", "proj/helper"])
        calls = {(c.callee) for c in scan.calls}
        self.assertIn("fmt.Println", calls)
        self.assertIn("helper.Greet", calls)

    def test_go_aliased_single_import(self):
        src = (
            'package main\nimport h "example.com/acme/helper"\n'
            "func main() { h.Greet() }\n"
        )

        scan = quick.quick_scan(src, "go", "main.go")

        self.assertEqual(
            [(imp.module, imp.kind) for imp in scan.imports],
            [("example.com/acme/helper", "module")],
        )

    def test_go_method_and_interface(self):
        src = (
            "package svc\n\n"
            "type Store struct { db string }\n"
            "type Finder interface { Find(id int) bool }\n"
            "func (s *Store) Find(id int) bool { return s.find(id) }\n"
            "func (s *Store) find(id int) bool { return false }\n"
        )
        scan = quick.quick_scan(src, "go")
        by_q = {s.qualname: s for s in scan.symbols}
        self.assertIn("svc.Store", by_q)
        self.assertEqual(by_q["svc.Store"].kind, "type")
        self.assertIn("svc.Finder", by_q)
        self.assertEqual(by_q["svc.Finder"].kind, "interface")
        self.assertEqual(by_q["svc.Store.Find"].kind, "method")
        self.assertEqual(by_q["svc.Store.Find"].parent, "svc.Store")
        self.assertEqual(by_q["svc.Store.find"].parent, "svc.Store")
        self.assertIn(("svc.Store.Find", "s.find"), {(c.caller, c.callee) for c in scan.calls})

    def test_java(self):
        scan = _scan("Calc.java")
        by_q = {s.qualname: s for s in scan.symbols}
        self.assertIn("com.demo.Calc", by_q)
        self.assertEqual(by_q["com.demo.Calc"].kind, "class")
        self.assertEqual(by_q["com.demo.Calc.sum"].kind, "method")
        self.assertEqual(by_q["com.demo.Calc.sum"].parent, "com.demo.Calc")
        self.assertIn("sum", [s.name for s in scan.symbols])

        runner = _scan("Runner.java")
        calls = {(c.caller, c.callee) for c in runner.calls}
        self.assertIn(("com.demo.Runner.main", "Calc.sum"), calls)
        self.assertIn(("com.demo.Runner.main", "System.out.println"), calls)

    def test_rust(self):
        scan = _scan("rustx/lib.rs")
        by_q = {s.qualname: s for s in scan.symbols}
        self.assertEqual(by_q["rustx/lib.dist"].kind, "function")
        self.assertEqual(by_q["rustx/lib.Point"].kind, "type")
        self.assertEqual(by_q["rustx/lib.Point.origin"].kind, "method")
        self.assertEqual(by_q["rustx/lib.Point.origin"].parent, "rustx/lib.Point")
        self.assertIn(("std::fmt", "use"), [(i.module, i.kind) for i in scan.imports])

        main = _scan("rustx/main.rs")
        self.assertEqual(main.imports[0].module, "lib")
        self.assertEqual(main.imports[0].kind, "mod")
        calls = {(c.callee) for c in main.calls}
        self.assertIn("lib::Point::origin", calls)
        self.assertIn("lib::dist", calls)
        # macros like println! must not be reported as calls
        self.assertNotIn("println", {c.callee.split("::")[0] for c in main.calls})

    def test_rust_inline_module_scopes_functions_and_calls(self):
        src = (
            "mod util {\n"
            "    pub fn helper() { dependency(); }\n"
            "    fn internal() { helper(); }\n"
            "}\n"
            "fn caller() { util::helper(); }\n"
        )

        scan = quick.quick_scan(src, "rust", "lib.rs")
        by_q = {symbol.qualname: symbol for symbol in scan.symbols}
        self.assertEqual(by_q["lib.util.helper"].kind, "function")
        self.assertEqual(by_q["lib.util.helper"].parent, "lib.util")
        self.assertEqual(by_q["lib.util.internal"].kind, "function")
        calls = {(call.caller, call.callee) for call in scan.calls}
        self.assertIn(("lib.util.helper", "dependency"), calls)
        self.assertIn(("lib.util.internal", "helper"), calls)
        self.assertIn(("lib.caller", "util::helper"), calls)

    def test_rust_single_line_inline_module_scopes_function_and_calls(self):
        src = (
            "mod util { pub fn helper() { dependency(); } }\n"
            "fn caller() { util::helper(); }\n"
        )

        scan = quick.quick_scan(src, "rust", "lib.rs")

        by_q = {symbol.qualname: symbol for symbol in scan.symbols}
        self.assertEqual(by_q["lib.util.helper"].parent, "lib.util")
        self.assertEqual(
            {(call.caller, call.callee) for call in scan.calls},
            {
                ("lib.util.helper", "dependency"),
                ("lib.caller", "util::helper"),
            },
        )

    def test_rust_single_line_inline_module_scans_multiple_functions(self):
        src = (
            "mod util { pub fn one() {} pub fn two() { one(); } }\n"
            "fn caller() { util::two(); }\n"
        )

        scan = quick.quick_scan(src, "rust", "lib.rs")

        self.assertEqual(
            {symbol.qualname for symbol in scan.symbols},
            {"lib.util.one", "lib.util.two", "lib.caller"},
        )
        self.assertEqual(
            {(call.caller, call.callee) for call in scan.calls},
            {
                ("lib.util.two", "one"),
                ("lib.caller", "util::two"),
            },
        )

    def test_rust_explicit_module_paths_are_calls(self):
        src = (
            "mod util { pub fn helper() {} }\n"
            "mod outer {\n"
            "    fn caller() { self::helper(); super::util::helper(); "
            "crate::util::helper(); }\n"
            "}\n"
        )

        scan = quick.quick_scan(src, "rust", "lib.rs")

        self.assertEqual(
            [call.callee for call in scan.calls],
            ["self::helper", "super::util::helper", "crate::util::helper"],
        )

    def test_rust_trait(self):
        src = (
            "pub trait Shape {\n"
            "    fn area(&self) -> f64;\n"
            "}\n"
            "impl Shape for Square {\n"
            "    fn area(&self) -> f64 { 0.0 }\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "rust")
        by_q = {s.qualname: s for s in scan.symbols}
        self.assertIn("Shape", by_q)
        self.assertEqual(by_q["Shape"].kind, "interface")
        self.assertEqual(by_q["Shape.area"].kind, "method")
        self.assertEqual(by_q["Shape.area"].parent, "Shape")
        self.assertEqual(by_q["Square.area"].kind, "method")
        self.assertEqual(by_q["Square.area"].parent, "Square")

    def test_rust_trait_impl_non_path_targets(self):
        src = (
            "trait LocalTrait {}\n"
            "impl LocalTrait for *const Marker {\n"
            "    fn pointer_method(&self) {}\n"
            "}\n"
            "impl LocalTrait for [u8] {\n"
            "    fn slice_method(&self) {}\n"
            "}\n"
            "impl LocalTrait for (A, B) {\n"
            "    fn tuple_method(&self) {}\n"
            "}\n"
            "impl<'a, T> LocalTrait for &'a mut ::module::Borrowed<T> {\n"
            "    fn borrow_method(&self) {}\n"
            "}\n"
            "impl<T> LocalTrait for &&module::Nested<T> {\n"
            "    fn nested_ref_method(&self) {}\n"
            "}\n"
            "impl LocalTrait for Buffer<{1 + 1}> {\n"
            "    fn const_method(&self) {}\n"
            "}\n"
            "impl LocalTrait for crate::École {\n"
            "    fn unicode_method(&self) {}\n"
            "}\n"
            "impl LocalTrait for crate::r#type {\n"
            "    fn raw_method(&self) {}\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "rust")
        by_q = {symbol.qualname: symbol for symbol in scan.symbols}
        for owner, method in (
            ("*const Marker", "pointer_method"),
            ("[u8]", "slice_method"),
            ("(A, B)", "tuple_method"),
            ("Borrowed", "borrow_method"),
            ("Nested", "nested_ref_method"),
            ("Buffer", "const_method"),
            ("École", "unicode_method"),
            ("r#type", "raw_method"),
        ):
            symbol = by_q[f"{owner}.{method}"]
            self.assertEqual(symbol.kind, "method")
            self.assertEqual(symbol.parent, owner)

    def test_rust_impl_nested_generic_bounds(self):
        src = (
            "trait LocalTrait {}\n"
            "impl<F: for<'a> Fn(&'a str)> Widget<F> {\n"
            "    fn run(&self) {}\n"
            "}\n"
            "impl<T: Iterator<Item = U>, U> GenericWidget<T> {\n"
            "    fn next_item(&self) {}\n"
            "}\n"
            "impl<F: for<'a> Fn(&'a str)> LocalTrait for Handler<F> {\n"
            "    fn handle(&self) {}\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "rust")
        by_q = {symbol.qualname: symbol for symbol in scan.symbols}
        self.assertEqual(by_q["Widget.run"].parent, "Widget")
        self.assertEqual(by_q["GenericWidget.next_item"].parent, "GenericWidget")
        self.assertEqual(by_q["Handler.handle"].parent, "Handler")

    def test_rust_char_literal_brace_does_not_extend_impl(self):
        src = (
            "impl Widget {\n"
            "    fn render(&self) {\n"
            "        let brace = '{';\n"
            "        self.draw();\n"
            "    }\n"
            "}\n"
            "fn standalone(_value: &'static str) {\n"
            "    external_call();\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "rust")
        by_q = {symbol.qualname: symbol for symbol in scan.symbols}
        self.assertEqual(by_q["Widget.render"].kind, "method")
        self.assertEqual(by_q["standalone"].kind, "function")
        self.assertEqual(by_q["standalone"].parent, "")
        calls = {(call.caller, call.callee) for call in scan.calls}
        self.assertIn(("Widget.render", "self.draw"), calls)
        self.assertIn(("standalone", "external_call"), calls)

    def test_rust_public_mod_imports(self):
        src = "pub mod shared;\npub(crate) mod internal;\n"
        scan = quick.quick_scan(src, "rust")
        imports = scan.imports
        self.assertEqual(
            [(item.module, item.kind) for item in imports],
            [("shared", "mod"), ("internal", "mod")],
        )

    def test_rust_public_use_imports(self):
        src = "pub use crate::shared::item;\npub(crate) use self::internal::item;\n"
        scan = quick.quick_scan(src, "rust")
        imports = scan.imports
        self.assertEqual(
            [(item.module, item.kind) for item in imports],
            [("crate::shared::item", "use"), ("self::internal::item", "use")],
        )

    def test_rust_public_use_trees_import_each_path(self):
        src = (
            "pub use {crate::shared::Item, crate::other::Other};\n"
            "pub(crate) use crate::{internal::Item, shared::Other};\n"
        )
        imports = quick.quick_scan(src, "rust").imports
        self.assertEqual(
            [(item.module, item.kind) for item in imports],
            [
                ("crate::shared::Item", "use"),
                ("crate::other::Other", "use"),
                ("crate::internal::Item", "use"),
                ("crate::shared::Other", "use"),
            ],
        )

    def test_rust_public_use_tree_ignores_comments(self):
        src = (
            "pub use crate::{\n"
            "    shared::Item, // first re-export\n"
            "    other::Other, /* second; re-export */\n"
            "};\n"
        )
        imports = quick.quick_scan(src, "rust").imports
        self.assertEqual(
            [item.module for item in imports],
            ["crate::shared::Item", "crate::other::Other"],
        )

    def test_rust_use_paths_ignore_raw_strings_and_comment_gaps(self):
        src = (
            'const TEXT: &str = r#"\n'
            'use crate::fake;\n'
            'mod fake;\n'
            'fn fake() {}\n'
            '"#;\n'
            'use crate::real /* path comment */ :: item;\n'
        )
        scan = quick.quick_scan(src, "rust")
        imports = scan.imports
        self.assertEqual(
            [(item.module, item.kind) for item in imports],
            [("crate::real::item", "use")],
        )
        self.assertNotIn("fake", {item.name for item in scan.symbols})

    def test_rust_use_aliases_are_preserved(self):
        src = (
            "use crate::a::foo as bar;\n"
            "pub use crate::b::{baz as qux, plain};\n"
        )
        imports = quick.quick_scan(src, "rust").imports
        self.assertEqual(
            [(item.module, item.names) for item in imports],
            [
                ("crate::a::foo", ["bar"]),
                ("crate::b::baz", ["qux"]),
                ("crate::b::plain", []),
            ],
        )

    def test_get_set_are_valid_method_names(self):
        src = (
            "class Store {\n"
            "  get(key) { return key }\n"
            "  set(key, value) { this.map[key] = value }\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "javascript")
        names = {s.name for s in scan.symbols}
        self.assertIn("get", names)
        self.assertIn("set", names)
        pairs = {(s.parent, s.name) for s in scan.symbols}
        self.assertIn(("Store", "get"), pairs)
        self.assertIn(("Store", "set"), pairs)

    def test_rust_self_and_js_this_calls_captured(self):
        rust = quick.quick_scan(
            "impl Store {\n    fn open(&self) { self.connect() }\n}\n", "rust")
        self.assertIn(("Store.open", "self.connect"),
                      {(c.caller, c.callee) for c in rust.calls})
        js = quick.quick_scan(
            "class A {\n  run() { this.step() }\n}\n", "javascript")
        self.assertIn(("A.run", "this.step"), {(c.caller, c.callee) for c in js.calls})

    def test_multiple_requires_bind_their_own_names(self):
        src = (
            "const { first } = require('mod-a');\n"
            "function f() {}\n"
            "const second = require('mod-b');\n"
        )
        scan = quick.quick_scan(src, "javascript")
        names = {i.module: i.names for i in scan.imports}
        self.assertEqual(names["mod-a"], ["first"])
        self.assertEqual(names["mod-b"], ["second"])

    def test_asi_bare_call_is_not_a_method(self):
        src = (
            "class A {\n"
            "  m() {\n"
            "    helper(x)\n"
            "  }\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "javascript")
        by_q = {s.qualname: s for s in scan.symbols}
        self.assertNotIn("helper", {s.name for s in scan.symbols})
        self.assertIn("A.m", by_q)
        self.assertIn(("A.m", "helper"), {(c.caller, c.callee) for c in scan.calls})

    def test_java_new_statement_is_not_a_method(self):
        src = (
            "public class A {\n"
            "  public void m() {\n"
            "    new Thread(() -> {}).start();\n"
            "    return;\n"
            "  }\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "java")
        names = {s.name for s in scan.symbols}
        self.assertNotIn("Thread", names)
        self.assertIn("A.m", {s.qualname for s in scan.symbols})
        self.assertIn("start", [c.callee for c in scan.calls])

    def test_java_qualified_return_type_is_a_method(self):
        src = (
            "import java.util.Map;\n"
            "public class A {\n"
            "  public java.util.Map<String, Integer> counts() {\n"
            "    System.out.println(\"x\");\n"
            "    return null;\n"
            "  }\n"
            "}\n"
        )
        scan = quick.quick_scan(src, "java")
        names = {s.name for s in scan.symbols}
        self.assertIn("counts", names)
        self.assertNotIn("println", names)
        self.assertIn(("A.counts", "System.out.println"),
                      {(c.caller, c.callee) for c in scan.calls})

    def test_java_constructor_is_indexed_as_a_method(self):
        src = "class A { A() {} }\n"
        scan = quick.quick_scan(src, "java")
        by_q = {s.qualname: s for s in scan.symbols}
        self.assertIn("A.A", by_q)
        self.assertEqual(by_q["A.A"].kind, "method")
        self.assertEqual(by_q["A.A"].parent, "A")
        self.assertEqual(by_q["A.A"].signature, "")

    def test_bom_first_line_import_survives(self):
        src = "\ufeffimport os\n\ndef f():\n    pass\n"
        scan = quick.quick_scan(src, "python")
        self.assertIn("os", [i.module for i in scan.imports])
        self.assertIn("f", [s.name for s in scan.symbols])


if __name__ == "__main__":
    unittest.main()
