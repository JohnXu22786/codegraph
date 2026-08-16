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

    def test_bom_first_line_import_survives(self):
        src = "\ufeffimport os\n\ndef f():\n    pass\n"
        scan = quick.quick_scan(src, "python")
        self.assertIn("os", [i.module for i in scan.imports])
        self.assertIn("f", [s.name for s in scan.symbols])


if __name__ == "__main__":
    unittest.main()
