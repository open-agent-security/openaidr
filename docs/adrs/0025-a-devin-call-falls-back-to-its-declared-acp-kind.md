---
id: 0025
title: A Devin call the table does not name takes the ACP kind Devin declared
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

The table decides first. A call the table does not name, and that is not an
MCP call, takes the class of the kind Devin declared for it
(`toolclass.class_of_acp_kind`): `read` is `file_read`, `edit` `file_edit`,
`delete` and `move` `file_write`, `execute` `shell`, `fetch` `web_fetch`. A kind
that does not say one thing carries no capability: `search` is ACP's word for
searching files and the web alike, so it is `other`, like `think`,
`switch_mode` and `other`. A call with no declared kind still gets `None`. Only
`kind` is read from `tool_call_state`; the table still gains the names seen in
real sessions, so they keep their precise class.

## Alternatives considered

- **Keep extending the table only**: rejected because the gap reopens with every
  Devin release, model and Desktop build, and nothing says when it has.
- **Let the declared kind override the table**: rejected because ACP's kinds are
  coarser than the table's classes (`search` cannot be `file_search` or
  `web_search`), so a named tool would lose precision.
- **Map `search` to `file_search`**: rejected because a web search would then
  read as a local file search, a wrong answer rather than a missing one.

## Consequences

- A Devin tool added after the table was written is classed the day it ships,
  as precisely as its ACP kind allows.
- `canonical_arguments` stays empty for such a call: the kind says what the
  call is, not where its arguments sit.
- The mapping is ACP's, not Devin's, so another kind whose store keeps ACP
  records can use `class_of_acp_kind` the same way.
