import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { cpSync, mkdtempSync, rmSync } from 'node:fs'
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