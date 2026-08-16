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


if __name__ == "__main__":
    unittest.main()
