"""Command line interface: index, query, export, serve."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_config, write_default_config
from .exporter import export_dot, export_json
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


def _open_store(cfg, require_indexed=True):
    db = Path(cfg.db_path)
    if not db.exists():
        raise SystemExit(
            f"error: no index at {cfg.db_path}; run 'codegraph index' first"
        )
    store = IndexStore(str(db))
    if require_indexed and not store.get_meta("last_indexed"):
        store.close()
        raise SystemExit(
            f"error: no index at {cfg.db_path}; run 'codegraph index' first"
        )
    return store


def _out(cfg, rows, as_text):
    if getattr(cfg, "as_json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(as_text(rows))


def _cmd_init(args, cfg):
    path = write_default_config(Path(cfg.root))
    print(f"wrote {path}")


def _cmd_index(args, cfg):
    from .builder import build_index

    report = build_index(cfg, force=args.force, quiet=args.quiet or args.json)
    if args.json:
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))


def _cmd_status(args, cfg):
    store = _open_store(cfg)
    try:
        stats = query_stats(store)
    finally:
        store.close()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    else:
        langs = ", ".join(f"{k}:{v}" for k, v in sorted(stats["languages"].items()))
        print(f"files {stats['files']} | symbols {stats['symbols']} | "
              f"calls {stats['calls']} | imports {stats['imports']}")
        print(f"calls resolved {stats['calls_resolved']} / "
              f"unresolved {stats['calls_unresolved']} | "
              f"imports resolved {stats['imports_resolved']} / "
              f"unresolved {stats['imports_unresolved']}")
        print(f"languages: {langs}")
        print(f"root: {stats['root']}")
        print(f"last indexed: {stats['last_indexed']}")


def _fmt_rows(rows):
    return [dict(r) for r in rows]


def _cmd_callers(args, cfg):
    store = _open_store(cfg)
    try:
        rows = query_callers(store, args.symbol, limit=args.limit)
    finally:
        store.close()
    _out(cfg, rows, lambda r: "\n".join(
        f"  {x['qualname']}  ({x['kind']})  {x['call_site']}" for x in r) or
        f"no callers for {args.symbol}")


def _cmd_callees(args, cfg):
    store = _open_store(cfg)
    try:
        rows = query_callees(store, args.symbol, limit=args.limit)
    finally:
        store.close()
    _out(cfg, rows, lambda r: "\n".join(
        f"  {x['callee']} -> {x['target'] or '(unresolved)'}  {x['path']}:{x['line']}"
        for x in r) or f"no callees for {args.symbol}")


def _cmd_deps(args, cfg):
    store = _open_store(cfg)
    try:
        rows = query_deps(store, args.module, limit=args.limit)
    finally:
        store.close()
    _out(cfg, rows, lambda r: "\n".join(
        f"  {x['module']} -> {x['target_path'] or '(external)'}  line {x['line']}"
        for x in r) or f"no dependencies for {args.module}")


def _cmd_dependents(args, cfg):
    store = _open_store(cfg)
    try:
        rows = query_dependents(store, args.module, limit=args.limit)
    finally:
        store.close()
    _out(cfg, rows, lambda r: "\n".join(
        f"  {x['path']}  (line {x['line']})" for x in r) or
        f"no dependents for {args.module}")


def _cmd_impact(args, cfg):
    store = _open_store(cfg)
    try:
        rows = query_impact(store, args.symbol, depth=args.depth, limit=args.limit)
    finally:
        store.close()
    _out(cfg, rows, lambda r: "\n".join(
        f"  d{x['depth']} {x['qualname']}  ({x['kind']})  {x['path']}" for x in r) or
        f"no transitive callers for {args.symbol}")


def _cmd_search(args, cfg):
    store = _open_store(cfg)
    try:
        rows = query_search(store, args.query, limit=args.limit)
    finally:
        store.close()
    _out(cfg, rows, lambda r: "\n".join(
        f"  {x['qualname']}  ({x['kind']})  {x['path']}:{x['start_line']}"
        + (f"  {x['doc']}" if x.get("doc") else "") for x in r) or
        f"no matches for {args.query!r}")


def _cmd_export(args, cfg):
    store = _open_store(cfg)
    try:
        if args.format == "dot":
            content = export_dot(store)
        elif args.format == "json":
            content = json.dumps(export_json(store), ensure_ascii=False, indent=2)
        else:  # unreachable: argparse restricts choices
            raise SystemExit(f"error: unknown export format {args.format}")
    finally:
        store.close()
    if args.output:
        Path(args.output).write_text(content + "\n", encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(content)


def _cmd_serve(args, cfg):
    from .server.mcp import run_stdio

    run_stdio(sys.stdin.buffer, sys.stdout.buffer, sys.stderr, cfg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codegraph",
        description="Code knowledge graph: index a codebase and answer "
                    "call/dependency questions.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    # common options, also injected into every subcommand so they may appear
    # before or after the subcommand name
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None,
                        help="project root (default: current directory)")
    common.add_argument("--config", default=None,
                        help="path to a codegraph.json config file")
    common.add_argument("--db", default=None,
                        help="override the index database path")
    common.add_argument("--json", action="store_true", dest="json",
                        help="machine-readable output where supported")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="write a starter codegraph.json")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("index", parents=[common], help="build or refresh the index")
    p.add_argument("--force", action="store_true", help="re-parse every file")
    p.add_argument("--quiet", action="store_true", help="suppress progress output")
    p.set_defaults(func=_cmd_index)

    p = sub.add_parser("status", parents=[common], help="index statistics")
    p.set_defaults(func=_cmd_status)

    for name, module, text in (
            ("callers", "symbol", "show who calls a symbol"),
            ("callees", "symbol", "show what a symbol calls")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument(module)
        p.add_argument("--limit", type=int, default=100)
        p.set_defaults(func=_cmd_callers if name == "callers" else _cmd_callees)

    for name, text in (("deps", "show a module's dependencies"),
                       ("dependents", "show who imports a module")):
        p = sub.add_parser(name, parents=[common], help=text)
        p.add_argument("module")
        p.add_argument("--limit", type=int, default=200)
        p.set_defaults(func=_cmd_deps if name == "deps" else _cmd_dependents)

    p = sub.add_parser("impact", parents=[common], help="transitive callers of a symbol")
    p.add_argument("symbol")
    p.add_argument("--depth", type=int, default=3)
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=_cmd_impact)

    p = sub.add_parser("search", parents=[common], help="full-text search over the index")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("export", parents=[common], help="export the graph (dot|json)")
    p.add_argument("format", choices=("dot", "json"))
    p.add_argument("-o", "--output", default=None, help="output file (default: stdout)")
    p.set_defaults(func=_cmd_export)

    p = sub.add_parser("serve", parents=[common],
                       help="run the stdio tool server (MCP/JSON-RPC)")
    p.set_defaults(func=_cmd_serve)
    return parser


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        cfg = load_config(root=args.root, config_path=args.config)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load configuration: {exc}", file=sys.stderr)
        return 1
    if args.db:
        cfg.db_path = str(Path(args.db).resolve())
    cfg.as_json = args.json
    try:
        args.func(args, cfg)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
