# Remote MCP server (sub-project 3b): design

Status: approved in conversation (sections 1-3), written 2026-11-20. Builds on the agent surface (3a, `2026-11-17-agent-surface-design.md`), concurrency-safe storage (`2026-11-17-concurrency-safe-storage-design.md`) and lesson capture (3c, `2026-11-18-session-lesson-capture-design.md`).

## 1. Purpose and success

Hosted agents (Base44 first; Devin possibly) cannot reach a local stdio server. They can reach a URL. This sub-project serves the same eleven memory tools over streamable HTTP, behind bearer-token authentication, so such an agent can recall, check, remember and hand over lessons.

It succeeds when:

- An agent with a valid token calls the same eleven tools, with the same descriptions and the same results, as over stdio.
- A caller can do nothing a stdio agent cannot: it cannot accept or reject a lesson, cannot set `priority: must`, cannot choose the repo or store, and cannot see the server's filesystem layout.
- A missing, wrong, expired or revoked token gets nothing, and the response does not say which.
- Every note a remote agent writes is attributed to the token's name, whatever the client claims to be.
- Revoking a token takes effect on the next request without a restart.

It is deliberately small: one transport and an auth layer over the existing `MemoryService`.

## 2. Decisions taken with the user

| # | Decision | Chosen over |
|---|---|---|
| 1 | The server runs next to a checkout of the repo; anchors are verified exactly as in stdio. | No checkout (anchors unverified); both modes. |
| 2 | Named bearer tokens, each with a role (`read-only` or `read-write`); the token's name is the agent name. | One shared token; named tokens all read-write. |
| 3 | The server speaks plain HTTP on loopback by default; a non-loopback bind needs an explicit flag saying a TLS proxy or tunnel fronts it. | Built-in TLS; refuse any non-loopback bind without certificates. |
| 4 | One `MemoryService` per token (Approach A), opened lazily over the same store. | One shared service with a caller argument per call (B); one process per token (C). |

## 3. Architecture

```
agent_memory/
  remote/
    tokens.py    token file: create, list, revoke, verify (hash only)
    server.py    HTTP app (FastMCP streamable HTTP), per-token services, limits, audit log
  cli_remote.py  amem token create|list|revoke ; amem serve --http
  mcp_server.py  build_server gains a service provider and trust_client_name (stdio unchanged)
```

- `MemoryService` stays the security boundary and is not changed, except for the status redaction in 6.5 and the new error code `forbidden`.
- `mcp_server.build_server(service)` keeps working for stdio. Underneath it takes a provider: a callable that returns the service (and role) for the current request. The stdio provider returns the one fixed service and trusts the client-reported name, as today. The HTTP provider reads the verified token of the request, returns that token's service, and does not trust the client-reported name.
- Python 3.10+ and the `mcp` extra are required, as for stdio. `uvicorn` and `starlette` come with `mcp`. The core package stays 3.9+.

## 4. Tokens

- **Format.** `amt_` followed by 32 random bytes, base64url without padding (the 43 characters of the secret). Shown once, at creation.
- **Token file.** JSON at `<store dir>/tokens.json` (override: `--tokens PATH` or `AGENT_MEMORY_TOKENS`): `{"version": 1, "tokens": [{"name", "role", "hash", "created_at", "expires_at"}]}`. `hash` is the SHA-256 hex of the token. The file never holds a token. It is written atomically and with mode 0600 where the OS supports modes (on Windows the mode is best effort and the docs say to place the file in a private directory).
- **Names.** 1-64 characters of letters, digits and `_.@-` (the `sanitize_agent` alphabet). A name must be unique in the file. The name becomes the agent name on every note the token writes.
- **Roles.** `read-only` may call `memory_recall`, `memory_check`, `memory_status`. `read-write` may call all eleven tools.
- **Verification.** Read the `Authorization: Bearer` header, hash it, compare against every entry with `hmac.compare_digest`, then check expiry. The result is a name and a role, or nothing.
- **Reload.** The file is re-read when its modification time or size changes. Revocation and new tokens apply on the next request. If the file cannot be read or parsed, the server fails closed: every request gets 401 and the operator sees an error in the log. The last good copy is not kept.
- **Commands.**
  - `amem token create NAME --role read-only|read-write [--expires-in DAYS]` prints the token once and a paste-ready header and URL snippet.
  - `amem token list` shows name, role, created and expiry. It never shows a hash or a token.
  - `amem token revoke NAME` removes the entry.
  Editing the file by hand and running these commands at the same moment is the operator's risk; the commands do an atomic replace and do not take a lock.

## 5. Server runtime

`amem serve --http [--host 127.0.0.1] [--port 8765] [--tokens PATH] [--public-url URL] [--behind-tls] [--store ...] [--repo ...]`

- **Startup refuses to run** when: Python or `mcp` is missing; the token file has no unexpired token (there is no anonymous mode and no flag for one); the repo root or store directory is unusable; or `--host` is not a loopback address and `--behind-tls` is absent (the message explains that a bearer token over plain HTTP can be read on the network).
- **Endpoint.** Streamable HTTP at `/mcp`, in stateless JSON mode (`stateless_http=True`, `json_response=True`): no MCP session state lives in the server, so a restart loses nothing and a client may retry.
- **Host and Origin.** DNS-rebinding protection is on. Allowed hosts are `127.0.0.1:*`, `localhost:*`, `[::1]:*` and the host of `--public-url`. No CORS headers are sent.
- **Per-token services.** A `MemoryService` is opened lazily the first time a token is used, cached by (name, role, creation time), and closed when its token is gone (a sweep at each token-file reload) and at shutdown, which leaves a checkpoint. The services share one store through the journal and OS lock described in the storage spec, exactly like separate processes do. A token's service keeps its own session, so each agent has the session semantics of a stdio agent.
- **Role enforcement.** The role is read from the verified token of the request, before the service is reached. A read-only token calling any tool outside its three gets the tool error `forbidden`. The read-only service object is also a facade that exposes only `recall`, `check` and `status`, so no write method is reachable from it.
- **Identity.** The service for a token is built with that token's name as its agent. The client-reported name (`clientInfo`) is ignored on HTTP.
- **Fixed, not configurable.** `allow_must` is always off. No review tool exists. The repo root and store path come only from startup arguments.
- **Limits.** Request bodies over 1 MB get 413 (a content-length check plus a counting wrapper for chunked bodies). Failed authentications are counted in a sliding 60-second window; past 20 failures a request with an invalid token gets 429 without further work, and a valid token always passes, so an attacker cannot lock real agents out. The window is global, not per address, because behind a proxy every request shares an address. Its purpose is to bound log noise and slow guessing; the real protection is that a token has 256 bits of entropy.
- **Audit log.** One line to stderr per call: time, token name, tool, outcome code (`ok` or the error code), duration in milliseconds. It never contains arguments, results, the token or any part of it. Authentication failures log a count and the reason class (`missing`, `invalid`), never the presented value.
- **Concurrency.** Requests run on worker threads; each service has its own lock; the store is shared safely through the journal.

## 6. Security model

1. **Assets.** The memory store (notes agents will act on) and the server's filesystem. Tokens are credentials.
2. **Threats and answers.**
   - *Network eavesdropping.* Plain HTTP is only ever bound to loopback unless the operator states a TLS front is in place; the docs give Caddy and Cloudflare Tunnel examples.
   - *Token theft from the server.* Only hashes are stored; tokens are never logged.
   - *Token guessing.* 256-bit secrets, constant-time compare, failure limiter.
   - *Impersonating another agent.* The name comes from the token, never from the request.
   - *A read-only token writing.* Refused by the role check and unreachable through the facade.
   - *Browser-based and DNS-rebinding attacks on a loopback server.* Host and Origin checks; no CORS.
   - *Oversized or malformed requests.* 1 MB cap; the existing service limits apply unchanged.
   - *Prompt injection through recalled notes.* Unchanged from 3a and 3c: the untrusted-block labelling, the `unreviewed` flag and the pending queue apply to remote agents too. A remote `read-write` agent is as trusted as a stdio agent and no more.
3. **Lesson acceptance stays human.** `memory_capture_lessons` and the `lessons` argument work remotely and queue lessons as pending; `amem review accept` needs an interactive terminal on the machine with the store.
4. **No server paths to the caller.** `memory_status` returns the absolute store path and repo root today. On HTTP the service reports only the store's file name and the repo's directory name. Errors already carry repo-relative paths only; a test pins that no absolute server path appears in any remote response.
5. **Fail closed.** An unreadable token file, an unknown role or an unknown tool name results in refusal.

## 7. Errors

| Condition | Response |
|---|---|
| Missing, wrong, expired or revoked token | 401, `WWW-Authenticate: Bearer`, one fixed body |
| Throttled invalid token | 429 |
| Body over 1 MB | 413 |
| Host or Origin not allowed | 421 |
| Write tool with a read-only token | tool error `forbidden` |
| Any other service error | the same `{"error": {"code", "message"}}` as stdio |
| Internal failure | the generic message only; detail in the operator's log |

`forbidden` is added to the service's `CODES`.

## 8. Testing

Each behaviour has a test written to fail first.

- **Token file:** only hashes are stored; names are validated; duplicates refused; mode 0600 where supported; expiry; revocation and new tokens apply without restart; a corrupt file fails closed.
- **HTTP app, in process:** the five error cases above; Host and Origin spoofing; the 1 MB cap including chunked bodies; the failure limiter lets a valid token through.
- **Roles and identity:** a read-only token cannot write (and the facade has no write method); notes carry the token's name even when `clientInfo` claims another; two tokens keep separate sessions; `allow_must` stays off.
- **Surface:** the tool list is the same eleven tools with the same descriptions as stdio; no tool can review, accept, reject or promote.
- **Leaks:** the audit log holds no argument or result text and no token; a token never appears in an exception or response; no absolute server path appears in any response, including `memory_status` and errors.
- **End to end:** a real `uvicorn` subprocess and a real MCP client. A read-write token remembers a note and a read-only token recalls it; a lesson captured remotely is pending and not recalled; a stdio server and the HTTP server share one store.
- **Unchanged:** the protocol block and the installer goldens do not change; the eval scorecard (recall, leaks, abstention, held-out) does not move; recall latency over HTTP is measured and reported, not gated.

## 9. Documentation

README "Remote server": starting it, creating and revoking tokens, the role table, a Caddy example and a Cloudflare Tunnel example, Base44 setup (a custom MCP by URL with an `Authorization: Bearer` header), Devin (unverified, in the same way as the 3a tool table), the audit log format, and the known limits below.

## 10. Out of scope

OAuth flows; built-in TLS; more than one store or repo per server; more than two roles; rate limits on valid calls; review or accept over HTTP; a remote target for `amem install`; token rotation helpers beyond create and revoke; per-token store partitions.

## 11. Rulings and what each costs if wrong

1. **One service per token.** Costs a few extra open handles on one store, each syncing the journal per call. If agents number in the hundreds, this needs a shared service (Approach B) later.
2. **Role enforced at the adapter, not through the service's `read_only` mode.** That mode loads the store once, so a long-lived server on it would go stale. Cost: a second enforcement point to keep in step with the tool list; the test that every tool is classified covers it.
3. **Client-reported name ignored on HTTP.** Cost: logs show the token name, not the client product; an operator who wants the product name puts it in the token name.
4. **Token file fails closed on any read error.** Cost: a half-written edit locks every agent out until fixed; the log says why.
5. **Global failure limiter.** Cost: a flood of bad tokens makes other bad tokens get 429, which is the intent; valid tokens are unaffected.
6. **Plain HTTP with an explicit flag instead of built-in TLS.** Cost: the operator must run a proxy or tunnel.

## 12. To confirm while implementing

- The access token of the current request is readable from tool code in FastMCP 1.25 (`get_access_token`) when `stateless_http=True`; if not, the provider reads it from the request scope.
- `AuthSettings` needs an issuer and resource URL even for a bare token verifier; the values come from `--public-url` and no OAuth endpoint is served. If FastMCP would publish OAuth metadata routes that point nowhere, they are disabled.
- Starlette middleware ordering for the body cap and Host check relative to FastMCP's own checks.
- Windows behaviour of the token file mode and of two services in one process locking the same store (the storage spec tested two processes).
