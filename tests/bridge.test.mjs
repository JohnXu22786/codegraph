import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { chmodSync, cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
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

async function closePlugin() {
  if (typeof plugin.close === 'function') await plugin.close()
}

test('module exports the canonical plugin entry', () => {
  assert.equal(plugin.name, 'codegraph')
  assert.deepEqual(plugin.inject, ['tools'])
  assert.equal(typeof plugin.apply, 'function')
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
  const tools = await applyOnce({ root: join(PROJ, '..', 'no-such-dir') })
  const overview = tools.find((t) => t.name === 'codegraph_overview')
  const res = await overview.execute({})
  assert.equal(res.ok, false)
  assert.match(res.error, /no index|index/)
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
