# Security Policy

This policy covers **bugs in OpenAIDR's own code** — the readers, the session
model, the renderer, and the CLI.

## Reporting a vulnerability

Please report privately rather than opening a public issue.

File a [private security advisory via the GitHub Security
tab](https://github.com/open-agent-security/openaidr/security/advisories/new).
We acknowledge reports within 5 business days.

## What counts as a vulnerability here

OpenAIDR reads files that contain a person's prompts, the arguments they passed
to tools, the paths they work in, and — in an MCP connection log — occasionally
a credential a server echoed back in an error. Its whole job is to read that
material without spreading it. So the security surface is narrower and stranger
than a typical CLI's:

- **A `LOCAL_ONLY` field reaching a surface that should withhold it.** Turn
  text, initial prompts, context items, tool arguments, tool results, machine
  and user names, and an MCP server's own failure text must not appear in
  `--format json`. If you can get any of them into that document, that is a
  vulnerability, not a formatting bug. The boundary is defined in
  [`src/openaidr/model.py`](src/openaidr/model.py) (`LOCAL_ONLY`) and
  [the spec](docs/specs/session-collection.md).
- **Any network egress.** OpenAIDR has no upload path and opens no sockets. A
  code path that makes a network call is a vulnerability by construction,
  regardless of where it points.
- **Any write to a path OpenAIDR reads.** Transcripts and MCP logs are opened
  read-only. A path that writes, truncates, or moves one is a vulnerability.
- **Escaping the transcript root.** A crafted filename, symlink, or path in a
  transcript that causes a read outside the configured root.
- **A crash or hang on a hostile transcript.** OpenAIDR parses attacker-
  influenceable content: an MCP server controls its own log lines, and a tool
  result can contain anything. Unbounded memory growth, a hang, or an unhandled
  exception that escapes the per-file failure isolation is in scope.
- **A wrong-but-confident answer.** Silently attributing a tool call to the
  wrong MCP server, or to the wrong session, is a security bug in a tool whose
  output feeds detection. The project's standing rule is that a silent wrong
  answer is worse than a stated gap.

Out of scope: a missing agent kind, an unmapped source reported as unmapped, a
withheld outcome, or any other *stated* gap. Those are coverage, and coverage
is a public issue.

## Vulnerabilities in the parsing dependency

Session parsing comes from `adr-sensor` ([Uber's ADR
project](https://github.com/uber/ADR), Apache-2.0). A vulnerability in upstream's
parsing belongs to upstream — please disclose it there. If it also needs a
mitigation on our side of the adapter, open a private advisory here too and say
so, and we will coordinate.

## Vulnerabilities in an agent component

OpenAIDR does not mint vulnerability IDs and takes no position on what a session
means. If what you have found is a vulnerable MCP server, plugin, or skill
rather than a bug in OpenAIDR, disclose to that component's maintainer under
their own policy.
