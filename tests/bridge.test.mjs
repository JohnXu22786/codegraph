import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import {
  chmodSync,
  cpSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs'
import { join, dirname } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import * as plugin from '../index.js'

const THIS_DIR = dirname(fileURLToPath(import.meta.url))
const PLUGIN_DIR = join(THIS_DIR, '..')
const PROJ = join(PLUGIN_DIR, 'tests', 'fixtures', 'proj')

function applyOnce(config = {}) {
  const registered = []
  const ctx = { tools: { register: (t) => registered.push(t) } }
  return plugin.apply(ctx, config).then(() => registered)
}

function shellQuote(value) {
  return `'${value.replaceAll("'", "'\\''")}'`
}

function makePythonWrapper(dir, logPath, body = 'exec python3 "$@"') {
  const wrapper = join(dir, 'python-wrapper.sh')
  writeFileSync(
    wrapper,
    `#!/bin/sh\nprintf '%s\\n' "$*" >> ${shellQuote(logPath)}\n${body}\n`,
    'utf8',
  )
  chmodSync(wrapper, 0o755)
  return wrapper
}

function makePidLoggingPythonWrapper(dir, logPath) {
  const wrapper = join(dir, 'python-pid-wrapper.sh')
  writeFileSync(
    wrapper,
    `#!/bin/sh
printf '%s %s\\n' "$$" "$*" >> ${shellQuote(logPath)}
exec python3 "$@"
`,
    'utf8',
  )
  chmodSync(wrapper, 0o755)
  return wrapper
}

function makeFallbackRecoveryWrapper(dir, logPath, failPath, releasePath, donePath) {
  const wrapper = join(dir, 'python-fallback-wrapper.sh')
  const countPath = join(dir, 'persistent-count')
  writeFileSync(
    wrapper,
    `#!/bin/sh
log=${shellQuote(logPath)}
count_file=${shellQuote(countPath)}
fail_file=${shellQuote(failPath)}
release_file=${shellQuote(releasePath)}
done_file=${shellQuote(donePath)}

record() {
  printf '%s\\n' "$1" >> "$log"
}

if [ "\${3:-}" = "serve" ]; then
  count=0
  if [ -f "$count_file" ]; then count=$(tr -d '\\n' < "$count_file"); fi
  count=$((count + 1))
  printf '%s\\n' "$count" > "$count_file"
  record "persistent-start-$count"
  if [ "$count" -eq 1 ]; then
    while [ ! -f "$fail_file" ]; do sleep 0.01; done
    record "persistent-fail-1"
    exit 1
  fi
  while [ ! -f "$done_file" ]; do sleep 0.01; done
  record "persistent-ready-$count"
  exec python3 "$@"
fi

command="\${3:-unknown}"
record "fallback-start-$command"
while [ ! -f "$release_file" ]; do sleep 0.01; done
record "fallback-run-$command"
python3 "$@"
status=$?
touch "$done_file"
record "fallback-done-$command"
exit "$status"
`,
    'utf8',
  )
  chmodSync(wrapper, 0o755)
  return wrapper
}

function eventsFrom(logPath) {
  if (!existsSync(logPath)) return []
  return readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean)
}

async function waitForEvent(logPath, event) {
  const deadline = Date.now() + 2000
  while (Date.now() < deadline) {
    if (eventsFrom(logPath).includes(event)) return
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
  assert.fail(`timed out waiting for ${event}; events: ${eventsFrom(logPath).join(', ')}`)
}

async function waitForPidExit(pid) {
  const deadline = Date.now() + 2000
  while (Date.now() < deadline) {
    try {
      process.kill(pid, 0)
    } catch (error) {
      if (error.code === 'ESRCH') return
      throw error
    }
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
  assert.fail(`timed out waiting for process ${pid} to exit`)
}

async function closePlugin() {
  if (typeof plugin.close === 'function') await plugin.close()
}

test('module exports the canonical plugin entry', () => {
  assert.equal(plugin.name, 'codegraph')
  assert.deepEqual(plugin.inject, ['tools'])
  assert.equal(typeof plugin.apply, 'function')
})

test('npm package includes the bundled Python source', () => {
  const pack = JSON.parse(execFileSync('npm', ['pack', '--dry-run', '--json'], {
    cwd: PLUGIN_DIR,
    encoding: 'utf8',
  }))
  const files = pack[0]?.files?.map(({ path }) => path) ?? []

  assert.ok(files.includes('src/codegraph/__main__.py'))
})

test('npm package includes the self-describing plugin manifest', () => {
  const pack = JSON.parse(execFileSync('npm', ['pack', '--dry-run', '--json'], {
    cwd: PLUGIN_DIR,
    encoding: 'utf8',
  }))
  const files = pack[0]?.files?.map(({ path }) => path) ?? []

  assert.ok(files.includes('plugin.json'))
})

test('plugin manifest requires a non-empty db_path', () => {
  const manifest = JSON.parse(readFileSync(join(PLUGIN_DIR, 'plugin.json'), 'utf8'))
  assert.equal(manifest.configSchema.properties.db_path.type, 'string')
  assert.equal(manifest.configSchema.properties.db_path.minLength, 1)
})

test('apply registers exactly the eight documented tools', async () => {
  const tools = await applyOnce()
  assert.deepEqual(
    tools.map((t) => t.name),
    [
      'codegraph_callers',
      'codegraph_callees',
      'codegraph_deps',
      'codegraph_dependents',
      'codegraph_search',
      'codegraph_impact',
      'codegraph_overview',
      'codegraph_reindex',
    ],
  )
})

test('registered tools expose object parameter schemas', async () => {
  const tools = await applyOnce()

  for (const tool of tools) {
    assert.equal(tool.parameters.type, 'object', tool.name)
    assert.ok(tool.parameters.properties, `${tool.name} properties`)
    assert.ok(Array.isArray(tool.parameters.required), `${tool.name} required`)
  }

  const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]))
  assert.deepEqual(byName.codegraph_callers.parameters.required, ['symbol'])
  assert.deepEqual(byName.codegraph_overview.parameters.required, [])
})

test('read-only tools report a readable error before an index exists', async () => {
  try {
    const tools = await applyOnce({ root: join(PROJ, '..', 'no-such-dir') })
    const overview = tools.find((t) => t.name === 'codegraph_overview')
    const res = await overview.execute({})
    assert.equal(res.ok, false)
    assert.match(res.error, /no index|index/)
  } finally {
    await closePlugin()
  }
})

test('codegraph_reindex builds an index, then queries work', async () => {
  // work on a scratch copy so the fixture dir never gains a .cg/ index
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-'))
  const root = join(scratch, 'proj')
  cpSync(PROJ, root, { recursive: true })
  try {
    const tools = await applyOnce({ root })
    const byName = Object.fromEntries(tools.map((t) => [t.name, t]))

    const reindex = await byName['codegraph_reindex'].execute({})
    assert.equal(reindex.ok, true)
    assert.ok(reindex.data.files_scanned >= 1)

    const callers = await byName['codegraph_callers'].execute({ symbol: 'pkg.pricing.price' })
    assert.equal(callers.ok, true)
    assert.ok(callers.data.length >= 1)

    const search = await byName['codegraph_search'].execute({ query: 'cart' })
    assert.equal(search.ok, true)
    assert.ok(search.data.length >= 1)

    const overview = await byName['codegraph_overview'].execute({})
    assert.equal(overview.ok, true)
    assert.equal(typeof overview.data.files, 'number')
  } finally {
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})
test('bridge reuses one persistent Python process for a root', async () => {
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-persistent-'))
  const root = join(scratch, 'proj')
  const logPath = join(scratch, 'python-invocations.log')
  cpSync(PROJ, root, { recursive: true })
  const python = makePythonWrapper(scratch, logPath)
  try {
    const tools = await applyOnce({ root, python })
    const byName = Object.fromEntries(tools.map((t) => [t.name, t]))
    assert.equal((await byName.codegraph_reindex.execute({})).ok, true)
    assert.equal((await byName.codegraph_overview.execute({})).ok, true)
    assert.equal((await byName.codegraph_search.execute({ query: 'cart' })).ok, true)
    const invocations = readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean)
    assert.equal(invocations.length, 1)
  } finally {
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})

test('bridge shares query cache across symlink aliases for one root', async (t) => {
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-symlink-cache-'))
  try {
    const root = join(scratch, 'proj')
    const aliasA = join(scratch, 'alias-a')
    const aliasB = join(scratch, 'alias-b')
    const logPath = join(scratch, 'python-invocations.log')
    const python = makePythonWrapper(scratch, logPath)
    cpSync(PROJ, root, { recursive: true })
    const source = join(root, 'cache_module.py')
    writeFileSync(source, 'def alias_cache_marker():\n    return 1\n', 'utf8')

    const kind = process.platform === 'win32' ? 'junction' : 'dir'
    try {
      symlinkSync(root, aliasA, kind)
      symlinkSync(root, aliasB, kind)
    } catch (error) {
      const unavailableCodes = new Set([
        'EACCES', 'EPERM', 'ENOSYS', 'ENOTSUP', 'EOPNOTSUPP',
      ])
      if (!unavailableCodes.has(error.code)) throw error
      t.skip(`directory symlinks unavailable: ${error.message}`)
      return
    }

    const tools = await applyOnce({ python })
    const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]))
    assert.equal((await byName.codegraph_reindex.execute({ root: aliasA })).ok, true)

    const initial = await byName.codegraph_search.execute({
      root: aliasA,
      query: 'alias_cache_marker',
    })
    assert.equal(initial.ok, true)
    assert.ok(initial.data.some((row) => row.name === 'alias_cache_marker'))

    writeFileSync(source, 'def refreshed_alias_marker():\n    return 2\n', 'utf8')
    const reindex = await byName.codegraph_reindex.execute({ root: aliasB })
    assert.equal(reindex.ok, true)
    assert.ok(reindex.data.files_changed >= 1)

    const refreshed = await byName.codegraph_search.execute({
      root: aliasA,
      query: 'alias_cache_marker',
    })
    assert.equal(refreshed.ok, true)
    assert.deepEqual(refreshed.data, [])

    const newMarker = await byName.codegraph_search.execute({
      root: aliasA,
      query: 'refreshed_alias_marker',
    })
    assert.equal(newMarker.ok, true)
    assert.ok(newMarker.data.some((row) => row.name === 'refreshed_alias_marker'))

    assert.equal(eventsFrom(logPath).length, 1)
  } finally {
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})

test('bridge evicts idle persistent sessions across distinct roots', async () => {
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-session-eviction-'))
  const logPath = join(scratch, 'python-invocations.log')
  const python = makePidLoggingPythonWrapper(scratch, logPath)
  const roots = Array.from({ length: 9 }, (_, index) => join(scratch, `proj-${index}`))
  for (const root of roots) cpSync(PROJ, root, { recursive: true })

  try {
    const tools = await applyOnce({ python })
    const overview = tools.find((tool) => tool.name === 'codegraph_overview')

    for (const root of roots) {
      const result = await overview.execute({ root })
      assert.equal(result.ok, false)
    }
    const invocations = eventsFrom(logPath)
    assert.equal(invocations.length, roots.length)
    const firstPid = Number(invocations[0].split(' ', 1)[0])
    assert.ok(Number.isInteger(firstPid) && firstPid > 0)
    await waitForPidExit(firstPid)

    // The first root is the least recently used one and must have been closed.
    const result = await overview.execute({ root: roots[0] })
    assert.equal(result.ok, false)
    assert.equal(eventsFrom(logPath).length, roots.length + 1)
  } finally {
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})

test('aborting a bridge request terminates the Python process', async () => {
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-cancel-'))
  const root = join(scratch, 'proj')
  const logPath = join(scratch, 'python-invocations.log')
  cpSync(PROJ, root, { recursive: true })
  const python = makePythonWrapper(scratch, logPath, 'exec sleep 1')
  try {
    const tools = await applyOnce({ root, python })
    const overview = tools.find((tool) => tool.name === 'codegraph_overview')
    const controller = new AbortController()
    const abortTimer = setTimeout(() => controller.abort(), 25)
    const result = await Promise.race([
      overview.execute({}, { signal: controller.signal }),
      new Promise((resolve) => setTimeout(() => resolve({ timedOut: true }), 250)),
    ])
    clearTimeout(abortTimer)
    assert.equal(result.timedOut, undefined)
    assert.equal(result.ok, false)
    assert.match(result.error, /abort|cancel/i)

    const timed = await overview.execute({ timeoutMs: 25 })
    assert.equal(timed.ok, false)
    assert.match(timed.error, /timed out/i)
  } finally {
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})

test('a queued bridge timeout is not blocked by an earlier request', async () => {
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-queued-timeout-'))
  const root = join(scratch, 'proj')
  cpSync(PROJ, root, { recursive: true })
  const python = makePythonWrapper(scratch, join(scratch, 'python-invocations.log'), 'exec sleep 1')
  try {
    const tools = await applyOnce({ root, python })
    const overview = tools.find((tool) => tool.name === 'codegraph_overview')
    const first = overview.execute({})
    await new Promise((resolve) => setTimeout(resolve, 20))
    const second = overview.execute({ timeoutMs: 25 })
    const result = await Promise.race([
      second,
      new Promise((resolve) => setTimeout(() => resolve({ timedOut: true }), 250)),
    ])
    assert.equal(result.timedOut, undefined)
    assert.equal(result.ok, false)
    assert.match(result.error, /timed out/i)
    await closePlugin()
    await Promise.all([first, second])
  } finally {
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})

test('fallback keeps queued and new requests serialized for a root', async () => {
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-fallback-recovery-'))
  const root = join(scratch, 'proj')
  const logPath = join(scratch, 'events.log')
  const failPath = join(scratch, 'fail-persistent')
  const releasePath = join(scratch, 'release-fallback')
  const donePath = join(scratch, 'fallback-done')
  cpSync(PROJ, root, { recursive: true })
  const python = makeFallbackRecoveryWrapper(scratch, logPath, failPath, releasePath, donePath)
  const pending = []
  try {
    const tools = await applyOnce({ root, python })
    const byName = Object.fromEntries(tools.map((tool) => [tool.name, tool]))

    const first = byName.codegraph_reindex.execute({})
    pending.push(first)
    await waitForEvent(logPath, 'persistent-start-1')
    const queued = byName.codegraph_overview.execute({})
    pending.push(queued)
    writeFileSync(failPath, '')
    await waitForEvent(logPath, 'fallback-start-index')

    const newRequest = byName.codegraph_overview.execute({})
    pending.push(newRequest)
    await new Promise((resolve) => setTimeout(resolve, 50))
    assert.deepEqual(eventsFrom(logPath), [
      'persistent-start-1',
      'persistent-fail-1',
      'fallback-start-index',
    ])

    writeFileSync(releasePath, '')
    const [firstResult, queuedResult, newResult] = await Promise.all([first, queued, newRequest])
    assert.equal(firstResult.ok, true)
    assert.equal(queuedResult.ok, true)
    assert.equal(newResult.ok, true)
    assert.deepEqual(eventsFrom(logPath), [
      'persistent-start-1',
      'persistent-fail-1',
      'fallback-start-index',
      'fallback-run-index',
      'fallback-done-index',
      'persistent-start-2',
      'persistent-ready-2',
    ])
  } finally {
    writeFileSync(failPath, '')
    writeFileSync(releasePath, '')
    await Promise.allSettled(pending)
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})

test('a persistent bridge server does not die from cumulative stderr output', async () => {
  const scratch = mkdtempSync(join(tmpdir(), 'codegraph-bridge-output-'))
  const root = join(scratch, 'proj')
  const logPath = join(scratch, 'python-invocations.log')
  cpSync(PROJ, root, { recursive: true })
  const python = makePythonWrapper(
    scratch,
    logPath,
    'head -c 2097153 /dev/zero >&2\nexec python3 "$@"',
  )
  try {
    const tools = await applyOnce({ root, python })
    const byName = Object.fromEntries(tools.map((t) => [t.name, t]))
    assert.equal((await byName.codegraph_reindex.execute({})).ok, true)
    assert.equal((await byName.codegraph_overview.execute({})).ok, true)
    const invocations = readFileSync(logPath, 'utf8').trim().split('\n').filter(Boolean)
    assert.equal(invocations.length, 1)
  } finally {
    await closePlugin()
    rmSync(scratch, { recursive: true, force: true })
  }
})
