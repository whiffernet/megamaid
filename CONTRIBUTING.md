# Contributing

## Releasing

**The repo declares the version; the tag is cut from it.** `release-tag.yml` reads
`.claude-plugin/VERSION.txt` at merge and cuts `v<that>`. It does not invent a version, so a
tag can never advance without the tree advancing with it.

Bump inside your PR, before merge:

```bash
python3 scripts/bump_version.py --patch   # or --minor / --major / --set 1.2.3
```

That writes all three places the version is recorded — `.claude-plugin/VERSION.txt`,
`.claude-plugin/plugin.json`, and the README's pinned install line — validating every
replacement before writing any, so a failure cannot leave them disagreeing. It bumps from whichever is higher — the file or the newest tag — so a file that
has fallen behind cannot regenerate a version that was already released.

The tests in `tests/test_version_release_gate.py` enforce this and name the fix in their
failure messages. They run in CI's `test` job, which checks out at `fetch-depth: 0` so tags
are visible.

### Why it works this way

Between v0.9.0 and v0.9.4 the version never moved. Nothing failed, and four things quietly
broke ([#26](https://github.com/whiffernet/megamaid/issues/26)):

- Claude Code caches plugins in a version-keyed directory, so `plugin update` saw nothing new
  to fetch;
- `launch.py` stamped the state venv with the version, and the venv holds a **non-editable**
  copy — so users kept running v0.9.0 code no matter what they installed;
- `importlib.metadata.version("megamaid")` returned `0.9.0`, so `upgrade` stamped every
  project it touched with a version four releases wrong;
- `DEFAULT_USER_AGENT` identified every scraped request as `megamaid/0.9`.

The venv stamp now also carries a digest of the installed source, so a changed release
rebuilds whether or not anyone remembered to bump. That is a backstop, not a substitute:
the plugin cache and the project stamps still key on the version alone.

### What counts as shipped

A version bump is required when anything under `src/`, `scripts/`, `pyproject.toml`, or
`.claude-plugin/` changes. Docs, tests, and CI config do not require one.

## Changing the runtime

`src/megamaid/` is copied into every scaffolded project, and `src/megamaid_setup/known_hashes.json`
records every version of it that has ever shipped. Change a runtime file and the manifest goes
stale — regenerate it in the same PR:

```bash
python3 scripts/build_known_hashes.py
```

`tests/test_manifest_freshness.py` fails when you forget. A stale manifest makes `upgrade`
classify every file as divergent and refuse them all, while reporting that it protected your
work.

## Gates

```bash
python3 -m pytest tests/ -q --ignore=tests/integration
ruff check . && ruff format --check .
mypy src/ templates/ scripts/ --ignore-missing-imports
claude plugin validate . --strict
```

`.github/workflows/` is human-authored: the bot identity has no `workflows:write` scope by
design, so workflow changes come from a separate, human-pushed PR.
