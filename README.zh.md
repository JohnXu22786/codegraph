# CodeGraph

[English](README.md)

代码知识图谱插件：把代码库解析成可查询的索引，让 agent 能回答「谁调用了这个函数」「这个模块依赖什么」这类结构性问题。适合用来快速理解大型代码库，或作为改动前的风险排查工具。

自包含实现：Python 标准库 + SQLite，无强制第三方依赖。提供 CLI、Python API、以及一个 stdio 工具服务器（MCP 风格的 JSON-RPC 2.0），任何插件化 harness 都能以子进程方式加载它。

## 功能

- **调用关系**：`callers` / `callees` —— 谁调用了某个函数/方法，它又调用了什么
- **依赖关系**：`deps` / `dependents` —— 模块或文件导入了什么、被谁导入（区分内部解析与外部依赖）
- **全文搜索**：`search` —— 对符号名、docstring、签名做本地全文检索（SQLite FTS5，无需外部服务）
- **影响面分析**：`impact` —— 传递闭包，列出「这个符号改了会波及谁」
- **增量索引**：按内容哈希比对，只重解析变化的文件，删除的文件自动清理
- **导出可视化**：`export dot|json` —— Graphviz DOT / JSON，可直接丢进可视化工具
- **查询缓存**：工具服务器对只读查询做 30 秒 TTL 缓存
- **两种解析引擎**：tree-sitter（精确，可选安装）与正则扫描器（零依赖兜底），自动选择

## 工作原理

```
文件发现（include/exclude/大小上限）
   → 语法提取（符号、调用点、导入语句）
   → SQLite 存储（files / symbols / calls / imports + FTS5）
   → 跨文件解析（调用目标 → 符号，导入 → 文件）
   → 查询（CLI / 工具服务器 / Python API）
```

跨文件解析是启发式的，按优先级尝试：同文件精确匹配 → 导入可达文件 → 同包/同模块族 → 全局唯一名。解析失败的调用点保留原始文本并标记为 unresolved（外部代码、标准库、第三方包）。

## 安装

```bash
# 方式 A：安装为 Python 包（提供 codegraph 命令）
pip install -e .

# 可选：安装 tree-sitter 语法包，显著提升解析精度
pip install -e ".[treesitter]"

# 方式 B：零安装直接使用（仓库内）
set PYTHONPATH=src        # Windows
export PYTHONPATH=src     # Linux/macOS
python -m codegraph --help
```

支持 Python ≥ 3.10。支持语言：Python、JavaScript、TypeScript、Go、Java、Rust。

## 快速开始

```bash
cd examples/demo

# 1. 建立索引（增量模式，之后可随时刷新）
codegraph index
# indexed 4 files (4 changed, 0 skipped, 0 removed) ... 6 symbols, 7 calls, 3 imports

# 2. 提问
codegraph callers services.orders.create_order     # 谁调用了 create_order → app.run
codegraph callees app.run                          # app.run 调用了什么
codegraph deps services.billing                    # billing 模块依赖什么
codegraph dependents services.billing              # 谁依赖 billing
codegraph search "coupon"                          # 全文搜索
codegraph impact services.billing.price            # 影响面（传递调用者）
codegraph status                                   # 索引统计
codegraph export dot -o graph.dot                  # 导出可视化
```

## 命令行

| 命令 | 说明 | 示例 |
|---|---|---|
| `init` | 在项目根写入 starter 配置 codegraph.json | `codegraph init` |
| `index [--force]` | 建立/刷新索引；`--force` 全量重解析 | `codegraph index --force` |
| `status` | 统计：文件/符号/调用/导入数、解析率、语言分布 | `codegraph status` |
| `callers SYMBOL` | 直接调用者 | `codegraph callers pkg.cart.Cart.add` |
| `callees SYMBOL` | 被调用者 | `codegraph callees app.main` |
| `deps MODULE` | 模块依赖 | `codegraph deps web/index.ts` |
| `dependents MODULE` | 反向依赖 | `codegraph dependents pkg.pricing` |
| `impact SYMBOL [--depth N]` | 传递调用者 | `codegraph impact billing.price --depth 3` |
| `search TEXT` | 全文搜索 | `codegraph search "shopping cart"` |
| `export dot\|json [-o FILE]` | 导出图 | `codegraph export json -o g.json` |
| `serve` | 启动 stdio 工具服务器 | `codegraph serve` |

通用选项（可放在子命令前或后）：`--root` 项目根目录、`--config` 配置文件、`--db` 覆盖数据库路径、`--json` 机器可读输出。

## 工具接口（harness 集成）

`codegraph serve` 启动一个 stdio 工具服务器：从 stdin 读取换行分隔的 JSON-RPC 2.0 消息，向 stdout 写入响应，日志全部走 stderr。实现了 MCP 所需的握手子集（`initialize`、`tools/list`、`tools/call`、`ping`），对任何 MCP 客户端或直接对接 JSON-RPC 的 harness 均可用。

**8 个工具**（`plugin.json` 中附完整 JSON Schema）：

| 工具 | 参数 | 返回 |
|---|---|---|
| `callers` | symbol, limit | 直接调用者列表（符号、文件:行） |
| `callees` | symbol, limit | 被调用者列表（含 resolved/unresolved 标记） |
| `deps` | module, limit | 依赖列表（含解析到的文件路径） |
| `dependents` | module, limit | 反向依赖列表 |
| `search` | query, limit | 全文检索命中的符号 |
| `impact` | symbol, depth, limit | 传递调用者（含层级深度） |
| `overview` | — | 索引统计 |
| `reindex` | force | 刷新索引（唯一可写工具） |

除 `reindex` 外均为只读，且带 30 秒 TTL 查询缓存。未建索引时，只读工具返回 `isError: true` 的错误信息。

最小对话示例：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"any-harness"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"callers","arguments":{"symbol":"pkg.pricing.price"}}}
```

## 在 DSH 中安装

dsh（DeepSeek Harness，插件化 agent harness）通过其内置 MCP 客户端加载本插件，一行命令即可安装：

```bash
dsh plugin --profile demo add github:JohnXu22786/codegraph
```

卸载：

```bash
dsh plugin --profile demo remove dsh-codegraph
```

详见 **[docs/dsh-integration.md](docs/dsh-integration.md)**。

本仓库还随附自包含的 Node 桥（`index.js` + `dsh.bundle` + `cordis.patch.yml`）：安装后
8 个 codegraph 工具会直接注册为 `codegraph_callers` / `codegraph_callees` /
`codegraph_deps` / `codegraph_dependents` / `codegraph_search` / `codegraph_impact` /
`codegraph_overview` / `codegraph_reindex`。每次调用都以 `--json` 运行 Python CLI，
针对配置的代码库根目录（默认取 dsh 进程当前目录，可用 `root` 参数按次覆盖）查询。
尚未建索引时先调用 `codegraph_reindex`——只读工具在此之前会返回可读的错误提示。

## 配置文件

`codegraph.json`（项目根目录，`codegraph init` 生成；以下为关键字段示例，`exclude` 默认值共 17 项，此处为节选）：

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

- `include`：非空时仅索引匹配的路径前缀/glob
- `exclude`：目录名或 glob 模式，walk 时剪枝
- `max_file_kb`：超过该大小的文件跳过（通常是大文件/生成文件）
- `engine`：`auto`（有语法包用 tree-sitter，否则正则）/ `quick` / `deep`
- `language_map`：自定义扩展名映射，如 `{".md": "markdown"}` 预留
- 数据库默认位于 `<root>/.cg/cg.sqlite`（该目录已被默认排除，不会索引到自身）

环境变量覆盖：`CODEGRAPH_ROOT`、`CODEGRAPH_DB`、`CODEGRAPH_MAX_FILE_KB`、`CODEGRAPH_ENGINE`。

## 增量索引

默认开启。每次 `index` 计算文件内容 SHA-256，与库中记录比对：未变文件直接跳过（秒级刷新），变更文件原子替换其全部行（一个事务内删除旧行、写入新行），已删除的文件从库中清除。`--force` 或 `reindex force=true` 强制全量重解析。

## 导出可视化

```bash
codegraph export dot -o graph.dot     # Graphviz 格式：符号为节点、调用为实线边、未解析调用为虚线边、模块导入为文件间点线边
codegraph export json -o graph.json   # 结构化数据：files/symbols/calls/imports/meta
```

## 已知限制

- 正则引擎（`engine: quick`）不做字符串/注释感知，字符串里的 `foo(` 可能被误记为调用点；签名只取单行
- tree-sitter 引擎（`deep`）对箭头函数常量、对象方法等少数节点形态暂不提取符号
- 跨文件调用解析是启发式：重名方法在全局不唯一时保持 unresolved；动态调用（反射、`getattr`、动态 import）无法解析
- 宏调用（如 `println!`）不记录为调用点
- 上述限制只在极端情况下影响精度，不影响核心查询路径；索引质量可通过安装 tree-sitter 语法包提升

## 目录结构

```
├── pyproject.toml            # 打包与依赖声明
├── plugin.json               # 插件 manifest：入口、工具 schema、配置 schema
├── README.md
├── docs/
│   └── dsh-integration.md    # harness 接入说明
├── examples/demo/            # 可直接体验的示例项目
├── src/codegraph/
│   ├── cli.py                # 命令行入口
│   ├── config.py             # 配置加载（文件 + 环境变量）
│   ├── models.py             # 数据模型（符号/调用/导入）
│   ├── store.py              # SQLite 存储层 + FTS5
│   ├── builder.py            # 索引构建（增量）
│   ├── resolver.py           # 跨文件解析
│   ├── queries.py            # 查询 API
│   ├── exporter.py           # DOT/JSON 导出
│   ├── cache.py              # TTL 查询缓存
│   ├── scanner/              # 文件发现 + 两种解析引擎
│   │   ├── walk.py           #   发现与忽略规则
│   │   ├── quick.py          #   正则引擎（零依赖）
│   │   └── deep.py           #   tree-sitter 引擎（可选）
│   └── server/               # stdio 工具服务器
│       ├── handlers.py       #   工具定义/执行/渲染
│       └── mcp.py            #   JSON-RPC 协议层
└── tests/                    # 124 个单元/集成测试（另有 node:test 桥接用例）
```

## 测试

```bash
python -m unittest discover -s tests -t .
```

## 许可证

MIT — 见 [LICENSE](LICENSE)。
