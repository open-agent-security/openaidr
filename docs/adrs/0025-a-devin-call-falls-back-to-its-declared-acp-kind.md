---
id: 0025
title: A Devin call the table does not name is `other` when Devin declared an ACP kind
status: proposed
date: 2026-10-10
supersedes: null
superseded-by: null
---

## Context

The `devin` tool table was read from the input schemas of one Devin CLI
build (3000.11.3). Real sessions on the `swe-1-6` models then called
`find_file_by_name` and `code_search`, which the table did not hold, so those
calls had no class and a consumer could not tell them from a tool it had never
heard of. The binary names further tools the table lacks (`web_search`,
`browser_preview`, `shell_command`), Devin offers different tool sets by model,
and Devin Desktop bundles its own copy of the agent at a different version from
the CLI writing the same store. A table keyed on names will keep falling
behind.

Devin speaks the Agent Client Protocol, and for every call it keeps the ACP
`ToolCall` it sent its client in `tool_call_state`, including `kind`, ACP's
closed category for the call (`read`, `edit`, `delete`, `move`, `search`,
`execute`, `think`, `fetch`, `switch_mode`, `other`). Measured on a real store,
270 of 310 calls had one, and every one agreed with the table where the table
had the name: `read` read, `exec` execute, `grep` search, `edit` edit.

## Decision

The table decides first, and only the table gives a capability. A call the
table does not name, and that is not an MCP call, is `other` when Devin
declared any ACP kind for it (`toolclass.class_of_acp_kind`): the call is known
to be the agent's own built-in, and nothing about what it touched is claimed. A
call with no declared kind still gets `None`. Only `kind` is read from
`tool_call_state`; the table keeps gaining the names seen in real sessions,
which is how a tool gets its precise class.

ACP defines kinds so that a client can "choose appropriate icons", and its
definitions are broad: `read` is "files or data", `edit` "files or content",
`execute` "commands or code", `fetch` "external data", `search` "information".
Mapping any of them to a file, shell or web class would tell a consumer a
database read was a file read, or an artifact-store fetch was web access.

## Alternatives considered

- **Keep extending the table only**: rejected because the gap reopens with every
  Devin release, model and Desktop build, and nothing says when it has.
- **Let the declared kind override the table**: rejected because ACP's kinds are
  coarser than the table's classes (`search` cannot be `file_search` or
  `web_search`), so a named tool would lose precision.
- **Map each kind to the class it most often means** (`read` to `file_read`,
  `execute` to `shell`, `fetch` to `web_fetch`, and so on): rejected because
  each definition spans more than one class, so an unnamed database read,
  notebook cell or artifact-store fetch would read as a file read, a shell
  command or web access: a wrong answer rather than a missing one. Review on
  the first version of this PR found exactly that, kind by kind.

## Consequences

- A Devin tool added after the table was written is recognized as the agent's
  own the day it ships; it gets a capability once the table names it.
- `canonical_arguments` stays empty for such a call: the kind says what the
  call is, not where its arguments sit.
- The rule is ACP's, not Devin's, so another kind whose store keeps ACP records
  can use `class_of_acp_kind` the same way.
