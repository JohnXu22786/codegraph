"""Tests for the tree-sitter scanner (scanner.deep)."""

import unittest

from codegraph.scanner import deep

from .fixtures import PROJ

DEEP_AVAILABLE = deep.available()


@unittest.skipUnless(DEEP_AVAILABLE, "tree-sitter grammars not installed")
class DeepPythonTest(unittest.TestCase):
    def test_cart_symbols_match_quick(self):
        from codegraph.scanner import quick

        text = (PROJ / "pkg/cart.py").read_text(encoding="utf-8")
        quick_scan = quick.quick_scan(text, "python", "pkg/cart.py")
        deep_scan = deep.deep_scan(text, "python", "pkg/cart.py")

        def keyed(scan):
            return {s.qualname: (s.kind, s.name, s.parent) for s in scan.symbols}

        self.assertEqual(keyed(deep_scan), keyed(quick_scan))
        # deep scanner also extracts the docstring
        cart = next(s for s in deep_scan.symbols if s.qualname == "pkg.cart.Cart")
        self.assertEqual(cart.doc, "A simple shopping cart.")
        # calls: deep must see the same core edges
        deep_calls = {(c.caller, c.callee) for c in deep_scan.calls}
        self.assertIn(("pkg.cart.Cart.add", "pricing.discount"), deep_calls)
        self.assertIn(("pkg.cart.Cart.total", "pricing.price"), deep_calls)
        # imports
        modules = {i.module for i in deep_scan.imports}
        self.assertIn("os", modules)
        self.assertIn("pkg", modules)

    def test_provider_selection_prefers_deep(self):
        from codegraph.scanner import provider_for

        prov = provider_for("python", "auto")
        self.assertEqual(prov.__name__, "deep_scan")

    def test_module_level_calls_not_attributed_to_last_symbol(self):
        """Regression: a trailing ``if __name__`` block must not be claimed by
        the last declared function (quick and deep must agree)."""
        from codegraph.scanner import quick

        text = (PROJ / "app.py").read_text(encoding="utf-8")
        deep_scan = deep.deep_scan(text, "python", "app.py")
        module_call = next(c for c in deep_scan.calls if c.callee == "main")
        self.assertEqual(module_call.caller, "")  # module level, no owner

        quick_scan = quick.quick_scan(text, "python", "app.py")
        quick_call = next(c for c in quick_scan.calls if c.callee == "main")
        self.assertEqual(quick_call.caller, "")
        deep_sig = {(c.caller, c.callee) for c in deep_scan.calls}
        quick_sig = {(c.caller, c.callee) for c in quick_scan.calls}
        self.assertEqual(deep_sig, quick_sig)

    def test_require_becomes_import_in_deep(self):
        """Regression: the require->import conversion was dead code, so the
        default (deep) engine lost CommonJS dependency edges."""
        text = (PROJ / "web/app.js").read_text(encoding="utf-8")
        scan = deep.deep_scan(text, "javascript", "web/app.js")
        modules = [(i.module, i.kind) for i in scan.imports]
        self.assertIn(("./util.js", "require"), modules)

    def test_new_expression_recorded_as_call(self):
        text = (PROJ / "web/index.ts").read_text(encoding="utf-8")
        scan = deep.deep_scan(text, "typescript", "web/index.ts")
        self.assertIn("Api", [c.callee for c in scan.calls])
        self.assertIn(("web/index.Api.fetch", "fmt"),
                      {(c.caller, c.callee) for c in scan.calls})

    def test_typescript_grammar_available(self):
        self.assertTrue(deep.supports("typescript"),
                        "tree_sitter_typescript exposes language_typescript()")

    def test_nested_function_inside_method(self):
        from codegraph.scanner import quick

        src = (
            "class A:\n"
            "    def m(self):\n"
            "        def helper():\n"
            "            return 1\n"
            "        return helper()\n"
        )
        deep_scan = deep.deep_scan(src, "python")
        by_q = {s.qualname: s for s in deep_scan.symbols}
        self.assertEqual(by_q["A.m.helper"].kind, "function")
        self.assertEqual(by_q["A.m.helper"].parent, "A.m")
        self.assertIn(("A.m", "helper"), {(c.caller, c.callee) for c in deep_scan.calls})

        quick_scan = quick.quick_scan(src, "python")
        deep_sig = {(s.qualname, s.kind, s.parent) for s in deep_scan.symbols}
        quick_sig = {(s.qualname, s.kind, s.parent) for s in quick_scan.symbols}
        self.assertEqual(deep_sig, quick_sig)


@unittest.skipUnless(deep.supports("javascript"), "tree-sitter JavaScript grammar not installed")
class DeepJavascriptTest(unittest.TestCase):
    def test_dynamic_import_is_recorded_as_an_import(self):
        src = 'async function load() { return import("./lazy.js"); }'

        scan = deep.deep_scan(src, "javascript", "app.js")

        self.assertEqual(
            [(imp.module, imp.kind, imp.line) for imp in scan.imports],
            [("./lazy.js", "import", 1)],
        )
        self.assertEqual(scan.calls, [])

    def test_chained_dynamic_import_is_recorded_once(self):
        src = 'async function load() { return import("./lazy.js").then(run); }'

        scan = deep.deep_scan(src, "javascript", "app.js")

        self.assertEqual(
            [(imp.module, imp.kind, imp.line) for imp in scan.imports],
            [("./lazy.js", "import", 1)],
        )

    def test_chained_commonjs_import_is_recorded_once(self):
        src = 'function load() { return require("./lazy.js").run(); }'

        scan = deep.deep_scan(src, "javascript", "app.js")

        self.assertEqual(
            [(imp.module, imp.kind, imp.line) for imp in scan.imports],
            [("./lazy.js", "require", 1)],
        )

    def test_commonjs_alias_binding_is_preserved(self):
        src = (
            "const { foo: bar } = require('./util.js');\n"
            "function caller() { return bar(); }\n"
        )

        scan = deep.deep_scan(src, "javascript", "app.js")

        self.assertEqual(
            [(imp.module, imp.names, imp.kind) for imp in scan.imports],
            [("./util.js", ["foo as bar"], "require")],
        )


@unittest.skipUnless(
    deep.supports("typescript"), "tree-sitter TypeScript grammar not installed"
)
class DeepTypescriptTest(unittest.TestCase):
    def test_dynamic_import_is_recorded_as_an_import(self):
        src = 'async function load() { return import("./lazy.js"); }'

        scan = deep.deep_scan(src, "typescript", "app.ts")

        self.assertEqual(
            [(imp.module, imp.kind, imp.line) for imp in scan.imports],
            [("./lazy.js", "import", 1)],
        )
        self.assertEqual(scan.calls, [])

    def test_chained_dynamic_import_is_recorded_once(self):
        src = 'async function load() { return import("./lazy.js").then(run); }'

        scan = deep.deep_scan(src, "typescript", "app.ts")

        self.assertEqual(
            [(imp.module, imp.kind, imp.line) for imp in scan.imports],
            [("./lazy.js", "import", 1)],
        )

    def test_declaration_function_is_indexed(self):
        src = "export declare function make(): void;\n"

        scan = deep.deep_scan(src, "typescript", "util/index.d.ts")

        self.assertEqual(scan.symbols[0].qualname, "util/index.make")
        self.assertEqual(scan.symbols[0].kind, "function")

    def test_generic_calls_are_recorded(self):
        src = "function caller() { foo<T>(); obj.foo<T>(); ns.foo<T>(); }"

        scan = deep.deep_scan(src, "typescript", "caller.ts")

        self.assertEqual(
            {(call.caller, call.callee) for call in scan.calls},
            {
                ("caller.caller", "foo"),
                ("caller.caller", "obj.foo"),
                ("caller.caller", "ns.foo"),
            },
        )

    def test_type_assertion_in_ts_keeps_calls(self):
        src = "function caller() { const x = <Item>make(); consume(x); }"

        scan = deep.deep_scan(src, "typescript", "caller.ts")

        self.assertEqual(
            {(call.caller, call.callee) for call in scan.calls},
            {("caller.caller", "make"), ("caller.caller", "consume")},
        )

    def test_tsx_extension_uses_jsx_grammar(self):
        src = "function render() { const x = <Item>{make()}</Item>; consume(x); }"

        scan = deep.deep_scan(src, "typescript", "render.tsx")

        self.assertEqual(
            {(call.caller, call.callee) for call in scan.calls},
            {("render.render", "make"), ("render.render", "consume")},
        )


@unittest.skipUnless(deep.supports("go"), "tree-sitter Go grammar not installed")
class DeepGoTest(unittest.TestCase):
    def test_generic_calls_are_recorded(self):
        src = (
            "package demo\n"
            "func foo[T any]() {}\n"
            "func caller[T any]() { foo[T](); foo[int]() }"
        )

        scan = deep.deep_scan(src, "go", "caller.go")

        self.assertEqual(
            [(call.caller, call.callee) for call in scan.calls],
            [("demo.caller", "foo"), ("demo.caller", "foo")],
        )

    def test_indexed_function_values_are_not_generic_calls(self):
        src = "package demo\nfunc caller(i int) { callbacks[i]() }"

        scan = deep.deep_scan(src, "go", "caller.go")

        self.assertEqual(scan.calls, [])

    def test_local_values_shadow_generic_function_names(self):
        src = (
            "package demo\n"
            "func foo[T any]() {}\n"
            "func caller[T any]() { foo[T](); foo := make([]func(), 1); foo[0]() }\n"
            "func shadow(foo map[int]func()) { foo[0]() }"
        )

        scan = deep.deep_scan(src, "go", "caller.go")

        self.assertEqual(
            [(call.caller, call.callee) for call in scan.calls],
            [("demo.caller", "foo"), ("demo.caller", "make")],
        )

    def test_local_types_and_type_parameters_shadow_generic_function_names(self):
        src = (
            "package demo\n"
            "func Item[T any]() {}\n"
            "func typeParameter[Item any]() {\n"
            "    Item[int]()\n"
            "}\n"
            "func localType() {\n"
            "    type Item[T any] int\n"
            "    Item[int]()\n"
            "}\n"
        )

        scan = deep.deep_scan(src, "go", "caller.go")

        self.assertEqual(scan.calls, [])

    def test_generic_type_parameters_do_not_leak_to_enclosing_scope(self):
        src = (
            "package demo\n"
            "func T[A any]() {}\n"
            "func localType() {\n"
            "    type Local[T any] int\n"
            "    T[int]()\n"
            "}\n"
            "func localAlias() {\n"
            "    type Alias[T any] = []T\n"
            "    T[int]()\n"
            "}\n"
        )

        scan = deep.deep_scan(src, "go", "caller.go")

        self.assertEqual([call.callee for call in scan.calls], ["T", "T"])

    def test_imported_generic_call_and_conversion_shapes_are_not_guessed(self):
        src = (
            "package demo\n"
            "import \"example.com/other\"\n"
            "func caller[T any]() { other.Foo[T](); other.Type[T](value) }"
        )

        scan = deep.deep_scan(src, "go", "caller.go")

        self.assertEqual(scan.calls, [])

    def test_type_and_receiver_method_symbols(self):
        src = (
            "package demo\n"
            "\n"
            "type Widget struct {\n"
            "    value int\n"
            "}\n"
            "\n"
            "func (w *Widget) Reset() {\n"
            "    w.value = 0\n"
            "}\n"
        )

        scan = deep.deep_scan(src, "go", "widget.go")
        symbols = {symbol.qualname: symbol for symbol in scan.symbols}

        self.assertEqual(symbols["demo.Widget"].kind, "type")
        self.assertEqual(symbols["demo.Widget"].parent, "")
        self.assertEqual(symbols["demo.Widget.Reset"].kind, "method")
        self.assertEqual(symbols["demo.Widget.Reset"].parent, "demo.Widget")

    def test_grouped_type_declarations_emit_each_type(self):
        src = (
            "package demo\n"
            "\n"
            "type (\n"
            "    Widget struct{}\n"
            "    Runner interface { Run() }\n"
            ")\n"
        )

        scan = deep.deep_scan(src, "go", "widget.go")
        symbols = {symbol.qualname: symbol for symbol in scan.symbols}

        self.assertEqual(
            {
                name: (symbol.kind, symbol.name, symbol.parent)
                for name, symbol in symbols.items()
            },
            {
                "demo.Widget": ("type", "Widget", ""),
                "demo.Runner": ("interface", "Runner", ""),
            },
        )

    def test_method_metadata_excludes_calls_before_method(self):
        src = (
            "package demo\n"
            "\n"
            "var _ = setup()\n"
            "\n"
            "type Widget struct{}\n"
            "\n"
            "func (w *Widget) Reset() {\n"
            "    helper()\n"
            "}\n"
        )

        scan = deep.deep_scan(src, "go", "widget.go")
        method = next(symbol for symbol in scan.symbols
                      if symbol.qualname == "demo.Widget.Reset")
        calls = {call.callee: call.caller for call in scan.calls}

        self.assertEqual(method.start, 7)
        self.assertEqual(calls["setup"], "")
        self.assertEqual(calls["helper"], "demo.Widget.Reset")


@unittest.skipUnless(deep.supports("rust"), "tree-sitter Rust grammar not installed")
class DeepRustTest(unittest.TestCase):
    def test_generic_calls_are_recorded(self):
        src = "fn caller() { foo::<T>(); obj.foo::<T>(); ns::foo::<T>(); }"

        scan = deep.deep_scan(src, "rust", "caller.rs")

        self.assertEqual(
            {(call.caller, call.callee) for call in scan.calls},
            {
                ("caller.caller", "foo"),
                ("caller.caller", "obj.foo"),
                ("caller.caller", "ns::foo"),
            },
        )

    def test_inline_module_scopes_functions_and_calls(self):
        src = (
            "mod util {\n"
            "    pub fn helper() { dependency(); }\n"
            "    fn internal() { helper(); }\n"
            "}\n"
            "fn caller() { util::helper(); }\n"
        )

        scan = deep.deep_scan(src, "rust", "lib.rs")
        by_q = {symbol.qualname: symbol for symbol in scan.symbols}
        self.assertEqual(by_q["lib.util.helper"].kind, "function")
        self.assertEqual(by_q["lib.util.helper"].parent, "lib.util")
        self.assertEqual(by_q["lib.util.internal"].kind, "function")
        calls = {(call.caller, call.callee) for call in scan.calls}
        self.assertIn(("lib.util.helper", "dependency"), calls)
        self.assertIn(("lib.util.internal", "helper"), calls)
        self.assertIn(("lib.caller", "util::helper"), calls)

    def test_explicit_inline_module_paths_are_calls(self):
        src = (
            "mod util { pub fn helper() {} }\n"
            "mod outer {\n"
            "    fn caller() { self::helper(); super::util::helper(); "
            "crate::util::helper(); }\n"
            "}\n"
        )

        scan = deep.deep_scan(src, "rust", "lib.rs")

        self.assertEqual(
            [call.callee for call in scan.calls],
            ["self::helper", "super::util::helper", "crate::util::helper"],
        )

    def test_trait_impl_methods_belong_to_implementing_type(self):
        src = (
            "trait Shape {\n"
            "    fn area(&self) -> f64;\n"
            "    fn label(&self) { self.area(); }\n"
            "}\n"
            "struct Square;\n"
            "impl Shape for Square { fn area(&self) -> f64 { 1.0 } }\n"
            "impl Square { fn perimeter(&self) -> f64 { 4.0 } }\n"
        )

        scan = deep.deep_scan(src, "rust", "geometry.rs")
        by_q = {symbol.qualname: symbol for symbol in scan.symbols}

        self.assertEqual(by_q["geometry.Shape"].kind, "interface")
        self.assertEqual(by_q["geometry.Shape.area"].kind, "method")
        self.assertEqual(
            by_q["geometry.Shape.area"].parent, "geometry.Shape"
        )
        self.assertEqual(by_q["geometry.Shape.label"].kind, "method")
        self.assertEqual(
            by_q["geometry.Shape.label"].parent, "geometry.Shape"
        )
        self.assertEqual(by_q["geometry.Square.area"].kind, "method")
        self.assertEqual(by_q["geometry.Square.area"].parent, "geometry.Square")
        self.assertEqual(by_q["geometry.Square.perimeter"].kind, "method")
        self.assertEqual(
            by_q["geometry.Square.perimeter"].parent, "geometry.Square"
        )


if __name__ == "__main__":
    unittest.main()
