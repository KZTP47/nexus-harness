# MCP

Planner and coder discovery may call `mcp_call` only for a configured server whose `allowed_tools` list is non-empty, and only for names in that list. The matching `tools/list` descriptor must set `annotations.readOnlyHint` to the JSON boolean `true` and must not set `annotations.destructiveHint` to `true`. `idempotentHint` never grants discovery authority by itself. Missing, conflicting, numeric, or string annotations fail closed. Empty `allowed_tools` keeps the server out of the agent discovery loop. The ordinary `harness mcp` CLI retains its separate explicit-user command path.

The MCP client supports both protocol eras over stdio and Streamable HTTP POST. The legacy era covers `2024-10-07` through `2025-11-25`; it uses `initialize`, `notifications/initialized`, and optional HTTP session IDs. The modern `2026-07-28` era has no initialize exchange or protocol session. Every modern request carries its protocol version, client identity, and client capabilities in `_meta`. Modern HTTP requests also carry `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name`. HTTP redirects are refused so one configured endpoint cannot retarget the client to another remote origin or a local service. Servers must be listed in trusted local configuration.

Set `protocol_mode` per server:

- `legacy` is the default. It avoids a probe and keeps spawn-per-command stdio use fast.
- `auto` probes `server/discover`, then uses modern mode or falls back on clear legacy evidence. HTTP timeouts, authorization failures, and server failures do not trigger legacy fallback.
- `modern` requires `2026-07-28` and fails when the server does not advertise it.

For stdio `auto` and `modern`, discovery runs on a disposable sibling process. The selected session process starts fresh and never receives the discovery probe. This protects legacy servers that exit when any method arrives before `initialize`.

## Stdio server

```json
{
  "mcp": {
    "servers": [
      {
        "name": "project-tools",
        "transport": "stdio",
        "command": "python",
        "args": ["tools/project_mcp.py"],
        "protocol_mode": "auto",
        "allowed_tools": ["search", "explain"]
      }
    ],
    "max_response_bytes": 1000000,
    "timeout_seconds": 60
  }
}
```

The command starts with `shell=False`. All modes support `tools/list` and `tools/call`.

Configured servers are available from the CLI:

```bash
harness mcp list project-tools
harness mcp call project-tools search --arguments '{"query":"parser"}'
```

`--arguments` must be a JSON object. An HTTP server may return an empty success body for a notification; requests that require a result still require a valid JSON-RPC response.

## HTTP server

```json
{
  "name": "database-tools",
  "transport": "http",
  "url": "https://tools.example.test/mcp",
  "protocol_mode": "modern",
  "allowed_tools": ["schema", "query_read_only"]
}
```

Plain HTTP is accepted only for loopback. Remote servers require HTTPS. The client refuses redirects.

Modern list results may include `ttlMs` and `cacheScope` (`private` or `public`). The client validates these hints and records the strictest values across paginated `tools/list` results. Invalid or absent hints become `0` and `private`; the current client does not reuse a cached catalog automatically.

## Limits

- Each request has a timeout.
- Serialized stdio requests and all responses have a byte cap.
- One monotonic deadline covers each request, including SSE streams that keep sending bytes.
- SSE frames are decoded as they arrive. The client returns the matching JSON-RPC response without waiting for the server to close the stream.
- HTTP redirects are refused.
- The client stops its reader and closes the response on deadline or early completion.
- While waiting for a client response, the dispatcher answers server `ping` requests on stdio or HTTP/SSE. Unsupported server requests receive JSON-RPC error `-32601`. Server notifications are retained in a bounded queue and can be drained by the caller.
- `tools/list` follows `nextCursor` pages under one monotonic deadline. Repeated or malformed cursors fail closed.
- Spawned stdio servers run in their own process group. On Windows, the client also assigns a kill-on-close Job Object when the host permits it. Timeout cleanup does not wait on a pipe retained by a descendant process.
- Stdio writes share the request's monotonic deadline. If a server stops reading, the client terminates and reaps its process tree and waits for the bounded writer to stop.
- Stdio output is read in fixed-size binary chunks and decoded incrementally. An unframed response cannot allocate past the response cap before rejection and process cleanup.
- Agent discovery tool calls require both an allowlist entry and an exact read-only, non-destructive annotation.
- A closed or malformed server fails the current call.
- Stdio stderr is drained concurrently so a noisy server cannot fill its pipe. Diagnostics are retained under a 64 KiB-or-response-limit cap and appended to protocol errors.
- Server processes stop when the client closes or times out.

Protocol behavior follows the [official MCP 2026-07-28 release notes](https://blog.modelcontextprotocol.io/posts/2026-07-28/) and the [TypeScript SDK protocol-era contract](https://ts.sdk.modelcontextprotocol.io/v2/protocol-versions). OAuth is not implemented.
