---
name: release-openaidr
description: Use when cutting a new openaidr PyPI release. Walks through version bump, release-notes drafting, pre-flight checks, tag, and push — enforces the publish-pypi.yml notes-file gate so the GitHub Releases page can't silently drift from PyPI.
---

# release-openaidr

End-to-end release procedure for the `openaidr` distribution.
`.github/workflows/publish-pypi.yml` enforces the machine-checkable gates
(tag on main, tag matches both version strings, notes file present, four
CI gates green). This skill is the human-side counterpart: it drafts the
notes with judgment, runs the pre-flight checks, and pushes the tag.

**Announce at start:** "I'm using the release-openaidr skill to cut a new
openaidr release."

## When to invoke

User says any of: "cut a release", "ship 0.1.0", "release openaidr",
"tag a new version", or starts editing `pyproject.toml`'s version field
in this repo.

## Inputs

1. **Target version** (e.g. `0.1.0`). Inferred from `pyproject.toml` if
   the user already bumped it; otherwise ask.
2. **Theme** — the H1 line of the notes file. Ask the user; a commit log
   can't infer it.

## Procedure

### Step 1. Pre-flight checks

Run from the repo root. Stop and ask if any fails — do not "fix" a dirty
tree or out-of-sync main automatically, those signal in-flight work.

```bash
git rev-parse --abbrev-ref HEAD                    # must be main
git status --porcelain                             # must be empty
git fetch --tags origin
git rev-parse --verify "v<version>" 2>/dev/null    # MUST FAIL — tag must not exist
test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
```

PyPI versions are immutable, including yanked ones. If `<version>` is
already on PyPI, the only path forward is a new version number:

```bash
curl -s https://pypi.org/pypi/openaidr/json | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["releases"]))'
```

(A 404 here means the project has never been published — expected before
the first release. See "First release" below for what that additionally
requires.)

### Step 2. Gather the commit log

```bash
prev_tag=$(git describe --tags --abbrev=0)         # empty on the first release
git log --oneline "${prev_tag}..HEAD"
```

Raw material only — do not dump it into the notes file verbatim.

### Step 2b. Span-identity and model-shape drift check (don't skip)

OpenAIDR's consumer-visible contract is the session model and, above all,
span identity: it names one tool call and must survive re-reads of a
growing session, because a consumer draws a call when parsed and attaches
outcome and enrichments later by naming the same span. A change to how it
is derived breaks every consumer, and it **fails silently** — enrichments
land on the wrong row. Nothing in CI can catch that for a downstream repo.

Diff the contract-defining files since the previous release:

```bash
# session/turn/tool-call model + span identity derivation
git diff "${prev_tag}..HEAD" -- src/openaidr/model.py
# per-kind normalisation rules: agent kind, (server, tool) split, status mapping
git diff "${prev_tag}..HEAD" -- src/openaidr/kinds.py src/openaidr/toolnames.py src/openaidr/readers/
# the spec that documents the contract
git diff "${prev_tag}..HEAD" -- docs/specs/session-collection.md
```

**Decision gate — stop and ask the user** if either holds:

- Span-identity derivation changed (different inputs, different ordering,
  a new component) → it goes under `## Compatibility` in its own
  paragraph, named as breaking, with what a consumer must do about stored
  spans. Never bury it in a highlights bullet.
- The model's field set or normalisation output changed (a new status, a
  renamed field, a different `(server, tool)` split for an existing kind)
  but `docs/specs/session-collection.md` did not → update the spec, or
  say why it still holds.

Adding coverage for a *new* agent kind is not a contract change — that's
a highlights bullet, not a compatibility note. The gate is about existing
kinds' output changing shape.

If nothing drifted, say so in one line in the release PR so the next
release knows the check ran and wasn't an oversight.

### Step 2c. Dependency and licence sweep

```bash
git diff "${prev_tag}..HEAD" -- pyproject.toml uv.lock | head -60
```

- `adr-sensor` is pinned exactly (`==`). A bump is consumer-visible —
  it changes which agent formats parse and how — so it earns its own
  bullet naming the old and new version.
- New runtime dependencies must be Apache-2.0-compatible, and must not
  drag in anything proprietary. OpenAIDR's standing rule: it installs and
  runs correctly for someone who has never heard of any downstream
  product. Check the notes file for that too — no closed-product names.

### Step 3. Draft `docs/releases/v<version>.md`

Follow the shape documented in `docs/releases/README.md`:

```markdown
# <version> — <theme>

## Highlights

- **<Theme A>.** 1-3 sentences pitched at someone writing a consumer
  against the session model. Reference ADRs by number when a design doc
  backs the change.
- **Bug fixes.** Combine small fixes into one bullet, comma-separated.

## Install

`uv tool install openaidr==<version>` or `pip install openaidr==<version>`.

## Compatibility

<Behavior changes existing consumers will notice — span identity first if
it moved. Say "pre-alpha, no back-compat hedging" while that's still true.>
```

**Judgment guidelines (apply ruthlessly):**

- **Group by theme, not by commit.** Five commits adding one agent kind's
  reader are one bullet.
- **Lead with user-visible impact**, not implementation. "Codex sessions
  now report MCP server names" — not "refactored `_split_tool_name`."
- **Cut chore/format/docs-only and release-machinery commits.** They
  aren't release-visible.
- **Acknowledge breaking changes explicitly** under `## Compatibility` —
  a changed span identity, a renamed model field, or a different status
  mapping goes there.

Show the draft to the user and iterate until they agree it captures what
shipped.

### Step 4. Bump both version strings together

`pyproject.toml`'s `version` and `src/openaidr/__init__.py`'s
`__version__` are separate sources; the workflow fails the build if they
disagree with the tag. Move them together:

```bash
sed -i.bak 's/^version = ".*"/version = "<version>"/' pyproject.toml && rm pyproject.toml.bak
sed -i.bak 's/^__version__ = ".*"/__version__ = "<version>"/' src/openaidr/__init__.py && rm src/openaidr/__init__.py.bak
uv lock                                            # refresh uv.lock
uv run openaidr --version                          # sanity: prints openaidr <version>
```

### Step 5. Release-prep commit + PR

If the user's original request did not explicitly ask to cut, ship, or
publish a release, stop before the push and ask. Editing the version
field is enough to invoke this skill; it is not permission to publish.

```bash
git checkout -b release/<version>
git add pyproject.toml uv.lock src/openaidr/__init__.py docs/releases/v<version>.md
git status --porcelain          # inspect anything still unstaged; stage intentionally or stop
git commit -m "release: <version> — <theme>"
git push -u origin release/<version>
```

Opening the PR needs its own explicit ask. Then:

```bash
gh pr create --title "release: <version>" --body "Release prep for <version>.

- Bumps \`pyproject.toml\` and \`__version__\` to <version>
- Adds release notes at \`docs/releases/v<version>.md\`
- After merge: tag \`v<version>\` on main and push to trigger PyPI publish + GitHub Release"
```

Surface the PR URL. **Stop here.** The user reviews and merges.

### Step 6. After merge: tag + push

After the user confirms the PR merged, on a clean main:

```bash
git checkout main
git pull --ff-only
git tag v<version>
git push origin v<version>
```

The tag-triggered run registers asynchronously, so `--limit 1` can grab a
stale earlier run. Poll for the run tied to the tagged commit:

```bash
tag_sha=$(git rev-parse "v<version>^{commit}")
run_id=""
until [ -n "$run_id" ]; do
  run_id=$(gh run list --repo open-agent-security/openaidr \
    --workflow publish-pypi.yml --commit "$tag_sha" \
    --json databaseId --jq '.[0].databaseId // empty')
  [ -z "$run_id" ] && sleep 5
done
gh run watch --repo open-agent-security/openaidr "$run_id"
```

### Step 7. Verify

```bash
curl -s https://pypi.org/pypi/openaidr/json | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["releases"]))'
gh release view v<version> --repo open-agent-security/openaidr --json name,body
uvx --from openaidr==<version> openaidr --version    # installs from PyPI, must print <version>
```

All three must succeed. If any fails, surface the workflow run URL rather
than guessing.

## First release

Before the *first* tag only, a human must have done two things that no
workflow can do for itself. Check both and stop if either is missing:

1. A **pending** Trusted Publisher on
   <https://pypi.org/manage/account/publishing/> — project `openaidr`,
   owner `open-agent-security`, repository `openaidr`, workflow
   `publish-pypi.yml`, environment `pypi`. Pending, because the project
   does not exist on PyPI until the first successful upload.
2. A repo **environment named `pypi`** (Settings → Environments). The
   name must match the publisher config exactly or the OIDC exchange is
   rejected — which surfaces as a confusing `invalid-publisher` error at
   the very last step, after a green build.

```bash
gh api repos/open-agent-security/openaidr/environments --jq '.environments[].name'
```

Nothing else about the first release differs: same notes file, same gates.

## Failure modes this prevents

1. **On PyPI but not on GitHub Releases.** The workflow's notes-file gate
   runs before publish: no notes file at the tag → no PyPI upload.
2. **Notes file is a raw commit dump.** Step 3's judgment guidelines.
3. **Tagging a non-main commit.** The workflow's "Verify tag is on main".
4. **Tag ≠ `pyproject.toml` version, or `__version__` drift.** Two
   separate workflow steps; Step 4 keeps them moving together.
5. **Forgetting to refresh `uv.lock`.** `uv sync --frozen` fails in the
   build job; Step 4 bumps the lock to avoid the round-trip.
6. **Silent span-identity or model-shape drift.** Step 2b — the one
   breakage no CI gate in this repo can see, because it only shows up in
   a consumer that stored spans from an earlier version.

## What this skill does NOT do

- **Doesn't publish to PyPI directly.** The workflow does, via Trusted
  Publishing (OIDC). There is no PyPI token anywhere in this repo, and
  none should be added.
- **Doesn't create the GitHub Release directly.** The workflow's
  `release-github` job does, from the notes file.
- **Doesn't write a CHANGELOG.md.** The convention is per-release files
  under `docs/releases/`.
- **Doesn't yank.** PyPI versions are immutable; a broken release is
  fixed by shipping the next version, not by re-uploading.
