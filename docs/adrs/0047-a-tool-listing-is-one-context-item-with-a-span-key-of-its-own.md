---
id: 0047
title: A tool listing is one context item per record, with a span key of its own
status: proposed
date: 2026-11-22
supersedes: null
superseded-by: null
---

## Context

A consumer that judges a tool call needs what the tool says it does. An MCP
tool's description reaches the model, but OpenAIDR did not carry it.

- Claude Code writes the tools loaded at the start in a `prompt_snapshot`
  attachment (`tools`, each with `name`, `description` and `schema`). It writes
  the tools that a tool search loads in a `deferred_tools_record` attachment
  (`entries`, each with `name`, `description` and `input_schema`). The search's
  own result is a `tool_reference` that holds a name and no description.
- Codex writes each tool that a tool search finds in its `tool_search_output`,
  inside the namespace of its server. The reader kept only the namespace's
  description, as `mcp_instructions`.

Two existing rules apply. ADR-0016 keeps one context item for each record, with
its named things as parts. A context item's span must not move. In Claude Code
the span is the item's position among the session's context items. In Codex it
is the record's line, and one `tool_search_output` already gives an
`mcp_instructions` item at that span.

## Decision

1. A record that lists tools with descriptions gives one `ContextItem` with
   source `tool_listing`. Each tool is one `ContextPart`: its name and its
   description. The item's text is the descriptions, with a blank line between
   them.
2. The item's span uses the turn key `context.tools`. In Claude Code, listings
   are counted apart from the other context items. In Codex, the index is the
   record's line, as for the other Codex context items.
3. A session lists each pair of name and description once. A tool with no name
   or no description has no part. A record with no part gives no item.

## Alternatives considered

- **Count listings under the `context` key.** Rejected. In Claude Code every
  context item after the first listing moves to a new span when a consumer
  upgrades, which ADR-0016 rejects. In Codex the listing and the
  `mcp_instructions` item of the same record get the same span.
- **One context item for each tool.** Rejected. ADR-0016 keeps one item for
  each record, and parts already name each tool. A snapshot of dozens of tools
  would also give dozens of items.
- **Add the argument descriptions from the schema to a part's text.** Rejected
  here. The schema is JSON, so the text would be a rendering that this package
  writes. A part's text is the record's own words.
- **Keep every repeat.** Rejected. A second snapshot, or a search for a tool
  already loaded, repeats the same tools. A consumer that counts material would
  count them twice.
- **Remove repeats by name alone.** Rejected. A server can reconnect with new
  words for a tool, and the model reads the new words from then on.
- **Give a tool with no description an empty part.** Rejected. The part has
  nothing of its own to carry. A record with nothing in it is not an item, as
  for the other context sources.

## Consequences

- Additive. No existing span moves, and `source`, `name`, `text` and `parts`
  keep their meaning.
- A consumer that finds context items by the `:context:` span prefix does not
  find listings. Listings use `:context.tools:`.
- A part is named by the same function that names a call to the tool, and
  `listed_tool_name` joins the result: `mcp__<server>__<tool>` for an MCP tool,
  else the call's tool name. A consumer joins a description to a call by
  name. In Codex the names are given when the session is built. A server name
  with punctuation is restored from the completed MCP items and the connection
  log, and those are complete only then.
- `deferred_tools_delta` holds names only, and is not read. A tool's argument
  descriptions are not carried.

## When to revisit

If Claude Code writes the description into the `tool_reference`, or a kind
writes one record for each tool, revisit the source of the parts.
