/**
 * dsh-codegraph —— codegraph 的 dsh（DeepSeek Harness）接入层。
 *
 * 本模块是纯 Node bridge：不在 Node 侧复刻任何解析/查询逻辑，而是把仓库里的
 * codegraph Python 核心（`python -m codegraph`，package 位于 src/codegraph）
 * 以子进程方式拉起，把 8 个工具能力暴露为 dsh 工具：
 *
 *   codegraph_callers / codegraph_callees / codegraph_deps /
 *   codegraph_dependents / codegraph_search / codegraph_impact /
 *   codegraph_overview / codegraph_reindex
 *
 * 每个工具按次执行 `python -m codegraph`，带 `--json` 取回结构化的本次查询
 * 结果，与 `codegraph serve` 的 MCP 对应工具语义一致。CLI 退出码非零
 * （如尚未建索引、符号不存在）时返回可读错误（value.ok=false + error），
 * 不会让宿主进程崩溃。只有 reindex 会写索引，其余均为只读。
 *
 * 配置（cordis.patch.yml 的 config 键，均可省略）：
 *   python —— Python 解释器命令或完整路径（默认 python / python3，按平台选）
 *   root   —— 默认代码库根目录（默认 process.cwd()；工具可按次用 root 覆盖）
 */

import { spawn } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'codegraph'
export const inject = ['tools']

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
const SRC_DIR = join(PLUGIN_DIR, 'src')

/** 解析 Python 解释器：配置优先，其次按平台惯例取默认。 */
function pythonBin(config) {
  if (config && typeof config.python === 'string' && config.python.trim()) {
    return config.python.trim()
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

/** 决定的本次查询使用的代码库根目录：调用参数 > 插件配置 > 进程当前目录。 */
function resolveRoot(config, args) {
  if (typeof args?.root === 'string' && args.root.trim()) return args.root.trim()
  if (config && typeof config.root === 'string' && config.root.trim()) return config.root.trim()
  return process.cwd()
}

/** 运行 `python -m codegraph` 并把 stdout 整体作为 JSON 解析。 */
function runCodegraph(config, argv) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonBin(config), ['-m', 'codegraph', ...argv], {
      cwd: PLUGIN_DIR,
      env: {
        ...process.env,
        // 未 pip install 时，让解释器能找到 src/codegraph 包。
        PYTHONPATH: joinPathList(SRC_DIR, process.env.PYTHONPATH),
      },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    let stdout = ''
    let stderr = ''
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => { stdout += chunk })
    child.stderr.on('data', (chunk) => { stderr += chunk })
    child.on('error', (err) => {
      reject(new Error(`无法启动 Python 解释器 ${pythonBin(config)}: ${err.message}`))
    })
    child.on('close', (code) => {
      const tail = (stderr || stdout).trim()
      if (code !== 0) {
        reject(new Error(tail || `codegraph 退出码 ${code}`))
        return
      }
      if (!stdout.trim()) {
        resolve(null)
        return
      }
      try {
        resolve(JSON.parse(stdout))
      } catch {
        reject(new Error(`codegraph 输出不是合法 JSON：${stdout.trim().slice(0, 200)}`))
      }
    })
    child.stdin.end()
  })
}

/** 合成 PYTHONPATH：把 src 目录放在已有值之前（平台分隔符）。 */
function joinPathList(first, rest) {
  const sep = process.platform === 'win32' ? ';' : ':'
  return rest ? `${first}${sep}${rest}` : first
}

/**
 * 组装一个只读查询工具。
 * @param {object} spec
 * @param {string} spec.name          dsh 工具名
 * @param {string} spec.subcommand    CLI 子命令
 * @param {string} spec.argName       CLI 位置参数（在 --root 之前传入）
 * @param {Array<{key: string, flag?: string, backedBy?: string}>} spec.args 额外参数表
 */
function makeQueryTool(config, spec) {
  const { name: toolName, subcommand, argName, args: extraArgs } = spec
  const parameters = {
    root: { type: 'string', description: '代码库根目录（默认取插件配置或当前目录）' },
  }
  if (argName) parameters[argName] = { type: 'string', required: true, description: spec.argHelp ?? '符号或模块名' }
  for (const a of extraArgs) parameters[a.key] = { type: 'integer' }

  return {
    name: toolName,
    description: spec.description,
    parameters,
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [
        { type: 'text', text: value.ok ? JSON.stringify(value.data, null, 2) : `出错：${value.error}` },
        ...(value.ok && value.data ? [{ type: 'json', json: value.data }] : []),
      ],
    },
    async execute(args) {
      const argv = [subcommand]
      if (argName) {
        const v = args?.[argName]
        if (typeof v !== 'string' || !v.trim()) return { ok: false, error: `缺少必填参数 ${argName}` }
        argv.push(v.trim())
      }
      for (const a of extraArgs) {
        if (a.flag && typeof args?.[a.key] === 'number' && a.key !== 'limit') argv.push(a.flag, String(args[a.key]))
      }
      const limit = args?.limit
      if (typeof limit === 'number') argv.push('--limit', String(limit))
      argv.push('--root', resolveRoot(config, args))
      argv.push('--json')
      try {
        const data = await runCodegraph(config, argv)
        return { ok: true, data }
      } catch (err) {
        return { ok: false, error: err.message }
      }
    },
  }
}

function makeOverviewTool(config) {
  return {
    name: 'codegraph_overview',
    description: '返回代码索引统计：文件/符号/调用/导入数、解析率、语言分布、根目录与最近索引时间。',
    parameters: { root: { type: 'string', description: '代码库根目录' } },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [
        { type: 'text', text: value.ok ? JSON.stringify(value.data, null, 2) : `出错：${value.error}` },
        ...(value.ok && value.data ? [{ type: 'json', json: value.data }] : []),
      ],
    },
    async execute(args) {
      const argv = ['status']
      argv.push('--root', resolveRoot(config, args))
      argv.push('--json')
      try {
        const data = await runCodegraph(config, argv)
        return { ok: true, data }
      } catch (err) {
        return { ok: false, error: err.message }
      }
    },
  }
}

function makeReindexTool(config) {
  return {
    name: 'codegraph_reindex',
    description: '刷新代码索引（唯一可写工具）：增量模式只重解析内容哈希变化的文件；force=true 全量重解析。索引建立后才能使用其他只读工具。',
    parameters: {
      force: { type: 'boolean', description: 'true 强制全量重解析（默认 false 增量）' },
      root: { type: 'string', description: '代码库根目录' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [
        { type: 'text', text: value.ok ? JSON.stringify(value.data, null, 2) : `出错：${value.error}` },
        ...(value.ok && value.data ? [{ type: 'json', json: value.data }] : []),
      ],
    },
    async execute(args) {
      const argv = ['index']
      argv.push('--root', resolveRoot(config, args))
      argv.push('--json')
      if (args?.force === true) argv.push('--force')
      try {
        const data = await runCodegraph(config, argv)
        return { ok: true, data }
      } catch (err) {
        return { ok: false, error: err.message }
      }
    },
  }
}

export async function apply(ctx, config = {}) {
  const tools = []
  tools.push(makeQueryTool(config, {
    name: 'codegraph_callers',
    subcommand: 'callers',
    argName: 'symbol',
    argHelp: '限定符号名，如 pkg.cart.Cart.add',
    args: [{ key: 'limit' }],
    description: '列出直接调用给定符号（函数/方法）的所有符号，每个含符号名、类型、文件:行。配合 impact 查看传递调用集合。',
  }))
  tools.push(makeQueryTool(config, {
    name: 'codegraph_callees',
    subcommand: 'callees',
    argName: 'symbol',
    argHelp: '限定符号名',
    args: [{ key: 'limit' }],
    description: '列出给定符号调用的所有内容，逐条标注是否解析到内部符号（resolved/unresolved）。',
  }))
  tools.push(makeQueryTool(config, {
    name: 'codegraph_deps',
    subcommand: 'deps',
    argName: 'module',
    argHelp: '文件路径（web/util.ts）或模块 id（pkg.cart）',
    args: [{ key: 'limit' }],
    description: '列出指定文件/包导入的模块（其依赖），区分已解析的内部依赖与外部依赖。',
  }))
  tools.push(makeQueryTool(config, {
    name: 'codegraph_dependents',
    subcommand: 'dependents',
    argName: 'module',
    argHelp: '文件路径或模块 id',
    args: [{ key: 'limit' }],
    description: '反向依赖：列出所有导入指定模块的文件/包。',
  }))
  tools.push(makeQueryTool(config, {
    name: 'codegraph_search',
    subcommand: 'search',
    argName: 'query',
    argHelp: '全文检索词',
    args: [{ key: 'limit' }],
    description: '对符号名、docstring 与签名做本地全文检索（SQLite FTS5），返回命中的符号。',
  }))
  tools.push(makeQueryTool(config, {
    name: 'codegraph_impact',
    subcommand: 'impact',
    argName: 'symbol',
    argHelp: '限定符号名',
    args: [{ key: 'limit' }, { key: 'depth', flag: '--depth' }],
    description: '给定符号的传递调用者（广度遍历，最多 depth 层）——改动它会波及的所有代码。',
  }))
  tools.push(makeOverviewTool(config))
  tools.push(makeReindexTool(config))
  for (const tool of tools) ctx.tools.register(tool)
  console.error(`[${name}] 已注册 ${tools.length} 个工具（root=${resolveRoot(config, {})}）`)
}