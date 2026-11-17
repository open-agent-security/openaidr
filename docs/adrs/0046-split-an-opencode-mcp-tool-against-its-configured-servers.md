---
id: 0046
title: Split an OpenCode MCP tool against its configured servers
status: accepted
date: 2026-11-17
supersedes: null
superseded-by: null
---

## Context

OpenCode names an MCP tool `sanitize(server) + "_" + sanitize(tool)`, and
records only that name on a tool part. Server and tool names can both hold
`_`. The tool `git_hub_search` can be the tool `hub_search` on server `git`,
or `search` on `git_hub`. OpenAIDR's rule is per-kind splitting, and a wrong
server is worse than none, because a consumer then joins the call to the wrong
component ([OpenCode](../specs/opencode.md#mcp-servers)).

## Decision

The reader reads the server names from every OpenCode configuration file that
can apply to the session. Exactly one configured server whose written name and
`_` start the tool name splits it, and the call names the configured server.
With none, or with more than one, the call stays whole, with no server and no
class. A name in OpenCode's built-in table is never split.

## Alternatives considered

- **Split at the first `_`.** Rejected: it gives a wrong server for any server
  name that holds `_`, and a wrong tool for many more.
- **Split at the last `_`.** Rejected for the same reason, from the other end.
- **Treat every unknown name as MCP with no server.** Rejected: an unknown
  built-in, from a newer OpenCode or a plugin, would then read as MCP.

## Consequences

The split depends on the configuration as it is when the session is read. A
server removed since leaves its calls whole. A plugin's custom tool whose name
starts with a server's written name and `_` would read as that server's tool.

## When to revisit

When OpenCode records the server on a tool part, or writes MCP tool names with
a delimiter that a server name cannot hold.
