# 接入 dsh（DeepSeek Harness）

dsh 是「一切皆是插件」的 agent harness：插件以 npm bundle 分发（`package.json` 声明 `dsh.bundle.patch` 指向一个 `cordis.patch.yml` 配置层），bundle 按序叠加进 profile 的配置。dsh 内置 MCP 客户端（`@deepseek-ai/dsh-mcp-client`），可以把任意 stdio 工具服务器挂载为工具。

本插件是自包含的 Python 实现，因此以**工具服务器**形态接入：不需要在 dsh 侧写任何 JS 代码，只加一条配置。

## 方式一：dsh 内置 MCP 客户端（推荐，一行配置）

在你的 profile 的 `cordis.patch.yml`（或 `$DSH_HOME/cordis.patch.yml`）中加入：

```yaml
- insert:
    - id: codegraph-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: codegraph
        command: python
        args:
          - -m
          - codegraph
          - serve
          - --root
          - /absolute/path/to/your/project
        env:
          PYTHONPATH: /absolute/path/to/this-plugin/src   # 已 pip install 的机器可省略
        cwd: /absolute/path/to/this-plugin
        toolCallTimeoutMs: 30000
        failOnStartupError: false
```

要点：

- **serverName** 决定工具命名空间：`mcp__codegraph__callers`、`mcp__codegraph__search` 等 8 个工具
- **--root** 指向要分析的项目；服务器会读取该项目根目录的 `codegraph.json`（若无则用默认配置）
- **首次使用前**在项目里先跑一次 `codegraph index` 建立索引（也可以在对话中调用 `mcp__codegraph__reindex`，增量刷新）
- Windows 下若 `python` 不在 PATH，把 `command` 换成解释器的完整路径
- 已知现象（harness 侧）：dsh 的 MCP 工具注册是异步的，新会话的第一轮 prompt 可能看不到 `mcp__*` 工具，第二轮出现即可正常使用

## 方式二：按 manifest 直接加载

`plugin.json` 是插件的自描述 manifest，声明了：

- `entry.tool_server`：`python -m codegraph serve`（stdio 工具服务器进程）
- `entry.cli`：`python -m codegraph`
- `tools`：8 个工具的完整名称、描述与 JSON Schema（与 `tools/list` 返回一致）
- `configSchema`：配置字段说明

任何「读 manifest 启动工具进程」的 harness 都可以这样接入：按 `entry.tool_server` 拉起进程，走标准 MCP 握手（`initialize` → `notifications/initialized` → `tools/list` → `tools/call`），协议版本支持 `2024-11-05` 与 `2025-03-26`。本插件的协议层只依赖标准库，不依赖任何 MCP SDK。

## 工具一览

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `callers` | 谁直接调用该符号 | `symbol`（限定名，如 `pkg.cart.Cart.add`） |
| `callees` | 该符号调用了什么 | `symbol` |
| `deps` | 模块依赖 | `module`（文件路径或模块 id） |
| `dependents` | 反向依赖 | `module` |
| `search` | 全文搜索（名字/docstring/签名） | `query` |
| `impact` | 传递调用者（改动波及面） | `symbol`, `depth` |
| `overview` | 索引统计 | — |
| `reindex` | 刷新索引（增量；`force` 全量） | `force` |

## 事件与生命周期

- 插件无外部事件依赖：索引是本地文件 `<root>/.cg/cg.sqlite`，工具服务器按请求读取
- 编辑密集的工作流可在适当时机调用 `reindex` 保持索引新鲜；增量模式只重解析内容哈希变化的文件，开销很小
- 服务器随 harness 结束 stdin 而退出（EOF → 退出 0），无残留进程
- 只读查询带 30 秒 TTL 缓存；`reindex` 结果不缓存

## 其他 harness

协议是通用的 MCP stdio（JSON-RPC 2.0 换行协议），任何支持 stdio MCP 服务器的 harness 都可以用同样的方式挂载；没有 MCP 客户端的环境也可以直接用 `codegraph` CLI 或 Python API（`import codegraph`）完成同样的查询。
