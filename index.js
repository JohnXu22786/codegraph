/**
 * dsh-codegraph —— codegraph 的 dsh（DeepSeek Harness）接入层。
 *
 * Python core runs as one long-lived stdio server per project root.  Keeping
 * the process alive reuses the server-side query cache and avoids paying the
 * interpreter startup cost for every tool call.  A failed server startup
 * falls back to the one-shot CLI so an installation problem remains visible
 * as a normal tool error.
 */

import { spawn } from 'node:child_process'
import { realpathSync } from 'node:fs'
import { dirname, join, resolve as resolvePath } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'codegraph'
export const inject = ['tools']

const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url))
const SRC_DIR = join(PLUGIN_DIR, 'src')
const DEFAULT_TIMEOUT_MS = 120000
const MAX_STREAM_BYTES = 2 * 1024 * 1024
const MAX_ROOT_SESSIONS = 8
const SESSIONS = new Map()

/** 解析 Python 解释器：配置优先，其次按平台惯例取默认。 */
function pythonBin(config) {
  if (config && typeof config.python === 'string' && config.python.trim()) {
    return config.python.trim()
  }
  return process.platform === 'win32' ? 'python' : 'python3'
}

/** 决定的本次查询使用的代码库根目录：调用参数 > 插件配置 > 进程当前目录。 */
function resolveRoot(config, args) {
  if (typeof args?.root === 'string' && args.root.trim()) return resolvePath(args.root.trim())
  if (config && typeof config.root === 'string' && config.root.trim()) return resolvePath(config.root.trim())
  return resolvePath(process.cwd())
}

/** 合成 PYTHONPATH：把 src 目录放在已有值之前（平台分隔符）。 */
function joinPathList(first, rest) {
  const sep = process.platform === 'win32' ? ';' : ':'
  return rest ? `${first}${sep}${rest}` : first
}

class PersistentServerUnavailableError extends Error {
  constructor(message) {
    super(message)
    this.name = 'PersistentServerUnavailableError'
    this.fallback = true
  }
}

class StreamLimitError extends Error {
  constructor(stream) {
    super(`codegraph ${stream} output exceeded ${MAX_STREAM_BYTES} bytes`)
    this.name = 'StreamLimitError'
  }
}

function abortError(reason) {
  if (reason instanceof Error) return reason
  const error = new Error(typeof reason === 'string' ? reason : 'codegraph request aborted')
  error.name = 'AbortError'
  return error
}

function timeoutError(timeoutMs) {
  const error = new Error(`codegraph request timed out after ${timeoutMs} ms`)
  error.name = 'TimeoutError'
  return error
}

function errorText(error) {
  return error instanceof Error ? error.message : String(error)
}

function objectSchema(properties, required = []) {
  return { type: 'object', properties, required }
}

/**
 * One persistent `python -m codegraph serve` process for one root.
 * Requests are serialized by RootSession because the Python stdio server is
 * deliberately synchronous and a reindex must not race a query for the same
 * SQLite database.
 */
class PythonServer {
  constructor(config, root) {
    this.config = config
    this.root = root
    this.child = null
    this.pending = new Map()
    this.nextId = 1
    this.stdoutBuffer = ''
    this.stderr = ''
    this.closed = false
  }

  _start() {
    if (this.closed) throw new Error('codegraph bridge is closed')
    if (this.child) return this.child

    let child
    try {
      child = spawn(pythonBin(this.config), ['-m', 'codegraph', 'serve', '--root', this.root], {
        cwd: PLUGIN_DIR,
        env: {
          ...process.env,
          PYTHONPATH: joinPathList(SRC_DIR, process.env.PYTHONPATH),
        },
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (error) {
      throw new PersistentServerUnavailableError(
        `无法启动 codegraph server：${errorText(error)}`,
      )
    }

    this.child = child
    this.stdoutBuffer = ''
    this.stderr = ''
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      if (this.child === child) this._onStdout(chunk)
    })
    child.stderr.on('data', (chunk) => {
      if (this.child === child) this._onStderr(chunk)
    })
    child.stdin.on('error', (error) => {
      if (this.child === child) {
        this._fail(new PersistentServerUnavailableError(
          `codegraph server stdin failed: ${errorText(error)}`,
        ))
      }
    })
    child.on('error', (error) => {
      if (this.child === child) {
        this._fail(new PersistentServerUnavailableError(
          `无法启动 Python 解释器 ${pythonBin(this.config)}: ${errorText(error)}`,
        ))
      }
    })
    child.on('close', (code, signal) => {
      if (this.child !== child) return
      this.child = null
      const details = this.stderr.trim().slice(-2000)
      const suffix = details ? `: ${details}` : ''
      this._rejectPending(new PersistentServerUnavailableError(
        `codegraph server exited (${signal ?? code ?? 'unknown'})${suffix}`,
      ))
    })
    return child
  }

  _onStdout(chunk) {
    this.stdoutBuffer += chunk
    while (true) {
      const newline = this.stdoutBuffer.indexOf('\n')
      if (newline < 0) {
        if (Buffer.byteLength(this.stdoutBuffer, 'utf8') > MAX_STREAM_BYTES) {
          this._fail(new StreamLimitError('stdout'))
        }
        return
      }
      const line = this.stdoutBuffer.slice(0, newline)
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1)
      if (Buffer.byteLength(line, 'utf8') > MAX_STREAM_BYTES) {
        this._fail(new StreamLimitError('stdout'))
        return
      }
      if (!line.trim()) continue
      let message
      try {
        message = JSON.parse(line)
      } catch {
        this._fail(new Error(`codegraph server 输出不是合法 JSON：${line.slice(0, 200)}`))
        return
      }
      const pending = this.pending.get(message.id)
      if (!pending) continue
      this.pending.delete(message.id)
      pending.cleanup()
      if (message.error) {
        pending.reject(new Error(message.error.message || 'codegraph server request failed'))
      } else {
        pending.resolve(message.result)
      }
    }
  }

  _onStderr(chunk) {
    this.stderr = (this.stderr + chunk).slice(-2000)
  }

  _rejectPending(error) {
    for (const pending of this.pending.values()) {
      pending.cleanup()
      pending.reject(error)
    }
    this.pending.clear()
  }

  _terminate() {
    const child = this.child
    if (!child) return
    this.child = null
    try {
      child.kill()
    } catch {
      // The process may already have exited.
    }
  }

  _fail(error) {
    this._rejectPending(error)
    this._terminate()
  }

  async request(tool, args, signal) {
    if (signal?.aborted) throw abortError(signal.reason)
    const child = this._start()
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      let abortListener
      const cleanup = () => {
        if (abortListener) signal?.removeEventListener('abort', abortListener)
      }
      const pending = { resolve, reject, cleanup }
      this.pending.set(id, pending)
      abortListener = () => {
        if (!this.pending.has(id)) return
        this.pending.delete(id)
        cleanup()
        reject(abortError(signal.reason))
        this._terminate()
      }
      if (signal) {
        signal.addEventListener('abort', abortListener, { once: true })
        if (signal.aborted) {
          abortListener()
          return
        }
      }
      try {
        child.stdin.write(JSON.stringify({
          jsonrpc: '2.0',
          id,
          method: 'tools/call',
          params: { name: tool, arguments: args },
        }) + '\n')
      } catch (error) {
        this.pending.delete(id)
        cleanup()
        reject(new PersistentServerUnavailableError(`codegraph server write failed: ${errorText(error)}`))
        this._terminate()
      }
    }).then((response) => {
      if (!response) throw new Error('codegraph server returned an empty response')
      if (response.isError) {
        const text = response.content?.find((item) => item.type === 'text')?.text
        throw new Error(text || 'codegraph tool failed')
      }
      return response.content?.find((item) => item.type === 'json')?.json ?? null
    })
  }

  close() {
    this.closed = true
    this._rejectPending(abortError('codegraph bridge closed'))
    this._terminate()
  }
}

class RootSession {
  constructor(config, root, onStateChange) {
    this.server = new PythonServer(config, root)
    this.tail = Promise.resolve()
    this.pendingCount = 0
    this.onStateChange = onStateChange
  }

  enqueue(task, signal) {
    if (signal?.aborted) return Promise.reject(abortError(signal.reason))

    this.pendingCount += 1

    let resolveResult
    let rejectResult
    let settled = false
    let cancelled = false
    let abortListener
    let released = false
    const result = new Promise((resolve, reject) => {
      resolveResult = resolve
      rejectResult = reject
    })
    const cleanup = () => {
      if (abortListener) signal?.removeEventListener('abort', abortListener)
    }
    const release = () => {
      if (released) return
      released = true
      this.pendingCount -= 1
      this.onStateChange?.()
    }
    const settle = (fn, value) => {
      if (settled) return
      settled = true
      cleanup()
      fn(value)
    }
    abortListener = () => {
      cancelled = true
      release()
      settle(rejectResult, abortError(signal.reason))
    }
    if (signal) {
      signal.addEventListener('abort', abortListener, { once: true })
      if (signal.aborted) {
        abortListener()
        return result
      }
    }

    const run = this.tail.then(async () => {
      if (cancelled || signal?.aborted) throw abortError(signal.reason)
      return task()
    })
    this.tail = run.catch(() => undefined)
    run.then(
      (value) => {
        release()
        settle(resolveResult, value)
      },
      (error) => {
        release()
        settle(rejectResult, error)
      },
    )
    return result
  }

  close() {
    this.server.close()
  }
}

function sessionKey(config, root) {
  let identityRoot = root
  try {
    identityRoot = realpathSync.native(root)
  } catch {
    // Keep the lexical path when the root cannot be resolved yet.
  }
  return `${pythonBin(config)}\0${identityRoot}`
}

function evictSessions() {
  while (SESSIONS.size > MAX_ROOT_SESSIONS) {
    let evictedKey
    let evictedSession
    for (const [key, session] of SESSIONS) {
      if (session.pendingCount === 0) {
        evictedKey = key
        evictedSession = session
        break
      }
    }
    if (!evictedSession) return
    SESSIONS.delete(evictedKey)
    evictedSession.close()
  }
}

function getSession(config, root) {
  const key = sessionKey(config, root)
  let session = SESSIONS.get(key)
  if (!session) {
    session = new RootSession(config, root, evictSessions)
    SESSIONS.set(key, session)
  } else {
    // Map insertion order is the LRU order.
    SESSIONS.delete(key)
    SESSIONS.set(key, session)
  }
  return session
}

function requestController(signal, timeoutMs) {
  const controller = new AbortController()
  let timer
  const relay = () => controller.abort(signal.reason)
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason)
    else signal.addEventListener('abort', relay, { once: true })
  }
  if (timeoutMs > 0) timer = setTimeout(() => controller.abort(timeoutError(timeoutMs)), timeoutMs)
  return {
    signal: controller.signal,
    cleanup() {
      if (timer) clearTimeout(timer)
      signal?.removeEventListener('abort', relay)
    },
  }
}

function requestTimeout(config, args) {
  const value = args?.timeoutMs ?? config?.timeoutMs ?? DEFAULT_TIMEOUT_MS
  if (typeof value !== 'number' || !Number.isFinite(value)) return DEFAULT_TIMEOUT_MS
  return Math.max(1, Math.floor(value))
}

/** Run the old one-shot CLI, used only when the persistent server cannot start. */
function runCodegraph(config, argv, { signal, timeoutMs } = {}) {
  return new Promise((resolve, reject) => {
    let child
    try {
      child = spawn(pythonBin(config), ['-m', 'codegraph', ...argv], {
        cwd: PLUGIN_DIR,
        env: {
          ...process.env,
          PYTHONPATH: joinPathList(SRC_DIR, process.env.PYTHONPATH),
        },
        stdio: ['pipe', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (error) {
      reject(new Error(`无法启动 Python 解释器 ${pythonBin(config)}: ${errorText(error)}`))
      return
    }

    let stdout = ''
    let stderr = ''
    let stdoutBytes = 0
    let stderrBytes = 0
    let settled = false
    let timer
    const finish = (fn, value) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      fn(value)
    }
    const terminate = () => {
      try {
        child.kill()
      } catch {
        // The process may already have exited.
      }
    }
    const fail = (error) => {
      terminate()
      finish(reject, error)
    }
    const onAbort = () => fail(abortError(signal.reason))
    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      stdoutBytes += Buffer.byteLength(chunk, 'utf8')
      if (stdoutBytes > MAX_STREAM_BYTES) fail(new StreamLimitError('stdout'))
      else stdout += chunk
    })
    child.stderr.on('data', (chunk) => {
      stderrBytes += Buffer.byteLength(chunk, 'utf8')
      if (stderrBytes > MAX_STREAM_BYTES) fail(new StreamLimitError('stderr'))
      else stderr += chunk
    })
    child.on('error', (error) => fail(new Error(`无法启动 Python 解释器 ${pythonBin(config)}: ${errorText(error)}`)))
    child.on('close', (code) => {
      if (settled) return
      const tail = (stderr || stdout).trim()
      if (code !== 0) {
        finish(reject, new Error(tail.slice(-2000) || `codegraph 退出码 ${code}`))
        return
      }
      if (!stdout.trim()) {
        finish(resolve, null)
        return
      }
      try {
        finish(resolve, JSON.parse(stdout))
      } catch {
        finish(reject, new Error(`codegraph 输出不是合法 JSON：${stdout.trim().slice(0, 200)}`))
      }
    })
    if (signal) {
      signal.addEventListener('abort', onAbort, { once: true })
      if (signal.aborted) {
        onAbort()
        return
      }
    }
    if (timeoutMs > 0) timer = setTimeout(() => fail(timeoutError(timeoutMs)), timeoutMs)
    child.stdin.end()
  })
}

async function executeCodegraph(config, args, execContext, request) {
  const root = resolveRoot(config, args)
  const timeoutMs = requestTimeout(config, args)
  const controls = requestController(execContext?.signal, timeoutMs)
  const session = getSession(config, root)
  try {
    const queued = session.enqueue(async () => {
      try {
        return await session.server.request(request.name, request.arguments, controls.signal)
      } catch (error) {
        if (!error?.fallback) throw error
        return runCodegraph(config, request.argv, {
          signal: controls.signal,
          timeoutMs,
        })
      }
    }, controls.signal)
    // Reserve the just-enqueued session before removing older idle sessions.
    evictSessions()
    return await queued
  } finally {
    controls.cleanup()
  }
}

function makeQueryTool(config, spec) {
  const { name: toolName, subcommand, argName, args: extraArgs } = spec
  const properties = {
    root: { type: 'string', description: '代码库根目录（默认取插件配置或当前目录）' },
    timeoutMs: { type: 'integer', description: '本次调用超时毫秒数（默认 120000）' },
  }
  const required = []
  if (argName) {
    properties[argName] = { type: 'string', description: spec.argHelp ?? '符号或模块名' }
    required.push(argName)
  }
  for (const a of extraArgs) properties[a.key] = { type: 'integer' }

  return {
    name: toolName,
    description: spec.description,
    parameters: objectSchema(properties, required),
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [
        { type: 'text', text: value.ok ? JSON.stringify(value.data, null, 2) : `出错：${value.error}` },
        ...(value.ok && value.data ? [{ type: 'json', json: value.data }] : []),
      ],
    },
    async execute(args, execContext = {}) {
      const argv = [subcommand]
      const rpcArgs = {}
      if (argName) {
        const v = args?.[argName]
        if (typeof v !== 'string' || !v.trim()) return { ok: false, error: `缺少必填参数 ${argName}` }
        argv.push(v.trim())
        rpcArgs[argName] = v.trim()
      }
      for (const a of extraArgs) {
        if (a.flag && typeof args?.[a.key] === 'number' && a.key !== 'limit') {
          argv.push(a.flag, String(args[a.key]))
          rpcArgs[a.key] = args[a.key]
        }
      }
      const limit = args?.limit
      if (typeof limit === 'number') {
        argv.push('--limit', String(limit))
        rpcArgs.limit = limit
      }
      const root = resolveRoot(config, args)
      argv.push('--root', root, '--json')
      try {
        const data = await executeCodegraph(config, args, execContext, {
          name: subcommand,
          arguments: rpcArgs,
          argv,
        })
        return { ok: true, data }
      } catch (error) {
        return { ok: false, error: errorText(error) }
      }
    },
  }
}

function makeOverviewTool(config) {
  return {
    name: 'codegraph_overview',
    description: '返回代码索引统计：文件/符号/调用/导入数、解析率、语言分布、根目录与最近索引时间。',
    parameters: objectSchema({
      root: { type: 'string', description: '代码库根目录' },
      timeoutMs: { type: 'integer', description: '本次调用超时毫秒数（默认 120000）' },
    }),
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [
        { type: 'text', text: value.ok ? JSON.stringify(value.data, null, 2) : `出错：${value.error}` },
        ...(value.ok && value.data ? [{ type: 'json', json: value.data }] : []),
      ],
    },
    async execute(args, execContext = {}) {
      const root = resolveRoot(config, args)
      try {
        const data = await executeCodegraph(config, args, execContext, {
          name: 'overview',
          arguments: {},
          argv: ['status', '--root', root, '--json'],
        })
        return { ok: true, data }
      } catch (error) {
        return { ok: false, error: errorText(error) }
      }
    },
  }
}

function makeReindexTool(config) {
  return {
    name: 'codegraph_reindex',
    description: '刷新代码索引（唯一可写工具）：增量模式只重解析内容哈希变化的文件；force=true 全量重解析。索引建立后才能使用其他只读工具。',
    parameters: objectSchema({
      force: { type: 'boolean', description: 'true 强制全量重解析（默认 false 增量）' },
      root: { type: 'string', description: '代码库根目录' },
      timeoutMs: { type: 'integer', description: '本次调用超时毫秒数（默认 120000）' },
    }),
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [
        { type: 'text', text: value.ok ? JSON.stringify(value.data, null, 2) : `出错：${value.error}` },
        ...(value.ok && value.data ? [{ type: 'json', json: value.data }] : []),
      ],
    },
    async execute(args, execContext = {}) {
      const root = resolveRoot(config, args)
      const force = args?.force === true
      try {
        const data = await executeCodegraph(config, args, execContext, {
          name: 'reindex',
          arguments: { force },
          argv: ['index', '--root', root, '--json', ...(force ? ['--force'] : [])],
        })
        return { ok: true, data }
      } catch (error) {
        return { ok: false, error: errorText(error) }
      }
    },
  }
}

/** Stop all persistent children. Useful for host shutdown and integration tests. */
export async function close() {
  for (const session of SESSIONS.values()) session.close()
  SESSIONS.clear()
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
