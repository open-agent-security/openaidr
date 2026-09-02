# Release notes

One file per release, named `v<version>.md` — `v0.1.0.md`, `v0.1.0b1.md`. No
`CHANGELOG.md`: the per-release file *is* the changelog entry, and it is also
the body of the GitHub Release.

`.github/workflows/publish-pypi.yml` will not publish a tag whose notes file is
missing. The check runs in the `build` job, before the PyPI upload, because PyPI
versions are immutable: a missing notes file caught after publish would leave
the package shipped and the Releases page silently empty. The first `# ` heading
in the file becomes the GitHub Release title.

The `release-openaidr` skill (`.claude/skills/release-openaidr/`) drafts these
from the commit log and walks the rest of the release.
