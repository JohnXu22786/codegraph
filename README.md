[简体中文](README.zh.md)

# CodeGraph

A code knowledge graph plugin: parses a codebase into a queryable index, so agents can answer structural questions like "who calls this function" or "what does this module depend on". Good for quickly understanding large codebases, or as a risk-screening tool before making changes.

Self-contained implementation: Python standard library + SQLite, no mandatory third-party dependencies. Ships a CLI, a Python API, and a stdio tool server (MCP-style JSON-RPC 2.0) that any plugin-based harness can load as a subprocess.

## Features

- **Call graph**: `callers` / `callees` — who calls a given function/method, and what it calls
- **Dependency graph**: `deps` / `dependents` — what a module or file imports and who imports it (internal resolution vs. external dependencies)
- **Full-text search**: `search` — local full-text search over symbol names, docstrings and signatures (SQLite FTS5, no external service)
- **Impact analysis**: `impact` — transitive closure listing "who would be affected if this symbol changed"
- **Incremental indexing**: content-hash comparison re-parses only changed files; deleted files are cleaned up automatically
- **Visualization export**: `export dot|json` — Graphviz DOT / JSON, ready to drop into visualization tools
- **Query cache**: the tool server caches read-only queries with a 30-second TTL
- **Two parse engines**: tree-sitter (precise, optional install) and a regex scanner (zero-dependency fallback), chosen automatically

## How it works

```
file discovery (include/exclude/size limit)
   → syntax extraction (symbols, call sites, import statements)
   → SQLite storage (files / symbols / calls / imports + FTS5)
   → cross-file resolution (call targets → symbols, imports → files)
   → queries (CLI / tool server / Python API)
```

Cross-file resolution is heuristic and tries, in priority order: exact match in the same file → files reachable by import → same package/module family → globally unique name. Call sites that fail to resolve keep their raw text and are marked `unresolved` (external code, standard library, third-party packages).

## Installation

```bash
# Option A: install as a Python package (provides the codegraph command)
pip install -e .

# Optional: install tree-sitter grammar packages for markedly better parse precision
pip install -e ".[treesitter]"

# Option B: use directly from the repo without installing
set PYTHONPATH=src        # Windows
export PYTHONPATH=src     # Linux/macOS
python -m codegraph --help
```

Requires Python ≥ 3.10. Supported languages: Python, JavaScript, TypeScript, Go, Java, Rust.

## Quick start

```bash
cd examples/demo

# 1. Build the index (incremental; refresh at any time)
codegraph index
# indexed 4 files (4 changed, 0 skipped, 0 removed) ... 6 symbols, 7 calls, 3 imports

# 2. Ask questions
codegraph callers services.orders.create_order     # who calls create_order → app.run
codegraph callees app.run                          # what app.run calls
codegraph deps services.billing                    # what the billing module depends on
codegraph dependents services.billing              # who depends on billing
codegraph search "coupon"                          # full-text search
codegraph impact services.billing.price            # impact (transitive callers)
codegraph status                                   # index statistics
codegraph export dot -o graph.dot                  # export a visualization
```

## Command line

| Command | Description | Example |
|---|---|---|
| `init` | Write a starter `codegraph.json` config at the project root | `codegraph init` |
| `index [--force]` | Build/refresh the index; `--force` re-parses everything | `codegraph index --force` |
| `status` | Statistics: file/symbol/call/import counts, resolution rates, per-language breakdown | `codegraph status` |
| `callers SYMBOL` | Direct callers | `codegraph callers pkg.cart.Cart.add` |
| `callees SYMBOL` | Callees | `codegraph callees app.main` |
| `deps MODULE` | Module dependencies | `codegraph deps web/index.ts` |
| `dependents MODULE` | Reverse dependencies | `codegraph dependents pkg.pricing` |
| `impact SYMBOL [--depth N]` | Transitive callers | `codegraph impact billing.price --depth 3` |
| `search TEXT` | Full-text search | `codegraph search "shopping cart"` |
| `export dot\|json [-o FILE]` | Export the graph | `codegraph export json -o g.json` |
| `serve` | Start the stdio tool server | `codegraph serve` |

Common options (before or after the subcommand): `--root` project root, `--config` config file, `--db` override database path, `--json` machine-readable output.

## Tool interface (harness integration)

`codegraph serve` starts a stdio tool server: reads newline-delimited JSON-RPC 2.0 messages from stdin, writes responses to stdout, and sends all logs to stderr. It implements the handshake subset MCP needs (`initialize`, `tools/list`, `tools/call`, `ping`) and works with any MCP client or harness that speaks JSON-RPC directly.

**8 tools** (full JSON Schemas are in `plugin.json`):

| Tool | Arguments | Returns |
|---|---|---|
| `callers` | symbol, limit | List of direct callers (symbol, file:line) |
| `callees` | symbol, limit | List of callees (with resolved/unresolved markers) |
| `deps` | module, limit | Dependency list (with resolved file paths) |
| `dependents` | module, limit | Reverse dependency list |
| `search` | query, limit | Symbols hit by full-text search |
| `impact` | symbol, depth, limit | Transitive callers (with depth levels) |
| `overview` | — | Index statistics |
| `reindex` | force | Refresh the index (the only writable tool) |

All tools except `reindex` are read-only and share a 30-second TTL query cache. When no index exists, read-only tools return an error with `isError: true`.

Minimal conversation example:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"any-harness"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"callers","arguments":{"symbol":"pkg.pricing.price"}}}
```

## Installing in DSH

dsh (DeepSeek Harness, a plugin-based agent harness) loads this plugin through its built-in MCP client, with a single line of configuration:

```bash
dsh plugin --profile demo add github:JohnXu22786/codegraph
```

Remove with:

```bash
dsh plugin --profile demo remove dsh-codegraph
```

See **[docs/dsh-integration.md](docs/dsh-integration.md)** for the full integration guide.

The repo also ships a self-contained Node bridge (`index.js` + `dsh.bundle` + `cordis.patch.yml`): once installed, the eight codegraph tools are registered directly as `codegraph_callers` / `codegraph_callees` / `codegraph_deps` / `codegraph_dependents` / `codegraph_search` / `codegraph_impact` / `codegraph_overview` / `codegraph_reindex`. Each call runs the Python CLI with `--json` against the configured root (default: the harness working directory; override per call with the `root` argument). No index yet? Call `codegraph_reindex` first — read-only tools return a readable error until then.

## Configuration

`codegraph.json` (at the project root, generated by `codegraph init`; key fields shown below — `exclude` has 17 defaults, abbreviated here):

```json
{
  "root": ".",
  "include": [],
  "exclude": [".git", "node_modules", "dist", "build", "venv", "target", ".cg"],
  "max_file_kb": 512,
  "incremental": true,
  "engine": "auto",
  "language_map": {}
}
```

- `include`: when non-empty, only matching path prefixes/globs are indexed
- `exclude`: directory names or glob patterns pruned during the walk
- `max_file_kb`: files larger than this are skipped (usually big/generated files)
- `engine`: `auto` (tree-sitter when grammar packages exist, otherwise regex) / `quick` / `deep`
- `language_map`: custom extension mapping, e.g. `{".md": "markdown"}` reserved
- The database lives at `<root>/.cg/cg.sqlite` by default (that directory is excluded by default, so the index never includes itself)

Environment variable overrides: `CODEGRAPH_ROOT`, `CODEGRAPH_DB`, `CODEGRAPH_MAX_FILE_KB`, `CODEGRAPH_ENGINE`.

## Incremental indexing

On by default. Every `index` run computes a SHA-256 of each file's content and compares it with what the database recorded: unchanged files are skipped (second-level refreshes), changed files have all their lines atomically replaced (delete + insert within a single transaction), and deleted files are removed from the store. `--force` or `reindex force=true` forces a full re-parse.

## Exporting visualizations

```bash
codegraph export dot -o graph.dot     # Graphviz format: symbols as nodes, calls as solid edges, unresolved calls as dashed edges, module imports as dotted file-to-file edges
codegraph export json -o graph.json   # structured data: files/symbols/calls/imports/meta
```

## Known limitations

- The regex engine (`engine: quick`) is not string/comment aware: `foo(` inside a string can be recorded as a call site; signatures are single-line only
- The tree-sitter engine (`engine: deep`) does not yet extract symbols for a few node shapes like arrow-function constants or object methods
- Cross-file call resolution is heuristic: same-name methods stay `unresolved` when not globally unique; dynamic calls (reflection, `getattr`, dynamic `import`) cannot be resolved
- Macro invocations (e.g. `println!`) are not recorded as call sites
- These only affect precision in edge cases, never the core query path; index quality improves by installing tree-sitter grammar packages

## Directory structure

```
├── pyproject.toml            # packaging and dependency declarations
├── plugin.json               # plugin manifest: entry, tool schemas, config schema
├── README.md
├── docs/
│   └── dsh-integration.md    # harness integration guide
├── examples/demo/            # a demo project you can try immediately
├── src/codegraph/
│   ├── cli.py                # command-line entry
│   ├── config.py             # config loading (file + environment variables)
│   ├── models.py             # data models (symbols/calls/imports)
│   ├── store.py              # SQLite storage layer + FTS5
│   ├── builder.py            # index building (incremental)
│   ├── resolver.py           # cross-file resolution
│   ├── queries.py            # query API
│   ├── exporter.py           # DOT/JSON export
│   ├── cache.py              # TTL query cache
│   ├── scanner/              # file discovery + two parse engines
│   │   ├── walk.py           #   discovery and ignore rules
│   │   ├── quick.py          #   regex engine (zero-dependency)
│   │   └── deep.py           #   tree-sitter engine (optional)
│   └── server/               # stdio tool server
│       ├── handlers.py       #   tool definitions/execution/rendering
│       └── mcp.py            #   JSON-RPC protocol layer
└── tests/                    # 124 unit/integration tests (+ node:test bridge cases)
```

## Tests

```bash
python -m unittest discover -s tests -t .
```

## License

MIT — see [LICENSE](LICENSE).
