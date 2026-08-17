# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- dsh bridge: `package.json` declares the `dsh.bundle` contract and
  `cordis.patch.yml` registers the plugin row; `index.js` exposes the eight
  codegraph tools (`callers` / `callees` / `deps` / `dependents` / `search` /
  `impact` / `overview` / `reindex`) to dsh. Install with
  `dsh plugin --profile demo add github:JohnXu22786/codegraph`.
- `tests/test_config.py`: dedicated coverage for config loading, environment
  overrides and validation.

### Fixed

- `include` / `exclude` given as a single string in `codegraph.json` now raise
  a readable `ValueError` instead of being iterated character by character by
  the file walker (which silently indexed nothing or pruned everything).
- README documents the actual `exclude` default count (17, not 18).

## [1.0.0] - 2026-08-16

### Added

- Initial release: code knowledge graph plugin for agent harnesses. Parses a
  codebase (Python / JavaScript / TypeScript / Go / Java / Rust) into a local
  SQLite index and answers call / dependency / impact / full-text questions.
- CLI, Python API, and a stdio tool server (MCP-style JSON-RPC 2.0) with eight
  tools, a 30-second TTL query cache and incremental indexing.
- Self-describing `plugin.json` manifest.