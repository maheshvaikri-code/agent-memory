# Remote MCP server (sub-project 3b): design

Status: implemented (branch `feature/remote-mcp`); approved in conversation (sections 1-3), written 2026-11-20. Builds on the agent surface (3a, `2026-11-17-agent-surface-design.md`), concurrency-safe storage (`2026-11-17-concurrency-safe-storage-design.md`) and lesson capture (3c, `2026-11-18-session-lesson-capture-design.md`).

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
    limits.py    ASGI guard in front of the MCP app: bearer check, fixed 401, 1 MB cap, failure limiter, audit log
    server.py    HTTP app (FastMCP streamable HTTP behind the guard), per-token services, startup rules
  cli_remote.py  amem token create|list|revoke ; amem serve --http
  mcp_server.py  build_server gains a service provider and trust_client_name (stdio unchanged)
```

- `MemoryService` stays the security boundary and is not changed, except for the status redaction in 6.5 and the new error code `forbidden`.
- `mcp_server.build_server(service)` keeps working for stdio. Underneath it takes a provider: a callable that returns the service (and role) for the current request. The stdio provider returns the one fixed service and trusts the client-reported name, as today. The HTTP provider reads the verified token of the request, returns that token's service, and does not trust the client-reported name: a provider-based server ignores the client-reported name unless asked to trust it. The token is verified by our own ASGI guard (`remote/limits.py`), not by the SDK's token verifier (ruling 7), and reaches the provider through a context variable.
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

- **Startup refuses to run** when: Python or `mcp` is missing; the token file has no unexpired token (there is no anonymous mode and no flag for one); the repo root or store directory is unusable (start-up opens and closes one service on the store, so a corrupt store file or a store directory that cannot be created is reported before the first request); `--public-url` is not `http(s)://host[:port]`; or `--host` is not a loopback address and `--behind-tls` is absent (the message explains that a bearer token over plain HTTP can be read on the network).
- **Endpoint.** Streamable HTTP at `/mcp`, in stateless JSON mode (`stateless_http=True`, `json_response=True`): no MCP session state lives in the server, so a restart loses nothing and a client may retry.
- **Host and Origin.** DNS-rebinding protection is on. Allowed hosts are `127.0.0.1:*`, `localhost:*`, `[::1]:*` and the host of `--public-url` (lower-cased, with and without a port, plus the exact port when the URL names one; an IPv6 literal is bracketed); a Host that is not allowed gets 421, and any Origin header gets 403 (no origin is allowed). No CORS headers are sent.
- **Guard scope.** The guard passes only `lifespan` scopes through unauthenticated. It closes a websocket handshake with code 1008 and refuses any scope type it does not know. A bearer credential over 512 characters is refused as invalid without being hashed.
- **Per-token services.** A `MemoryService` is opened lazily the first time a token is used, cached by (name, role, creation time), and closed when its token is gone (a sweep at the next call) and at shutdown, which leaves a checkpoint. The shutdown close runs from the ASGI lifespan shutdown (and again when `run_http` returns, as a second path). The sweep never closes a service while the token file is unreadable or a read was inconsistent: it needs a clean snapshot, bracketed by the file's generation counter, so an outage does not lose every session. The services share one store through the journal and OS lock described in the storage spec, exactly like separate processes do. A token's service keeps its own session, so each agent has the session semantics of a stdio agent.
- **Role enforcement.** The role is read from the verified token of the request, before the service is reached. A read-only token calling any tool outside its three gets the tool error `forbidden`. The read-only service object is also a facade that exposes only `recall`, `check` and `status`, so no write method is reachable from it.
- **Identity.** The service for a token is built with that token's name as its agent. The client-reported name (`clientInfo`) is ignored on HTTP (`build_server` ignores it on the provider path unless asked).
- **Fixed, not configurable.** `allow_must` is always off. No review tool exists. The repo root and store path come only from startup arguments.
- **Limits.** Request bodies over 1 MB get 413 (a content-length check plus a counting wrapper for chunked bodies). Failed authentications are counted in a sliding 60-second window; past 20 failures a request with an invalid token gets 429 without further work, and a valid token always passes, so an attacker cannot lock real agents out. The window is global, not per address, because behind a proxy every request shares an address. Its purpose is to slow guessing; the real protection is that a token has 256 bits of entropy. It does not bound log noise: one WARNING line is still written per throttled request. After authentication, any method other than `POST` gets 405 with `Allow: POST` and a fixed body, without the body being read (an idle SSE stream would otherwise be held open per client).
- **Audit log.** One line to stderr per call: UTC time (`...Z`), token name, tool, outcome code (`ok` or the error code), duration in milliseconds. It never contains arguments, results, the token or any part of it. An authentication failure logs one line carrying only `reason=<missing|invalid>` (and `throttled=yes` when the limiter answered); there is no count, and never the presented value. After a successful start-up one line on stderr gives the listen address, the token file, the number of usable tokens and the allowed hosts (never a token). The `server:` response header is off.
- **Concurrency.** Tool calls are served one at a time on the event loop (FastMCP 1.25.0 runs a plain `def` tool inline), so a slow call delays every caller, including authentication of new requests (measured: with one call held 3 s, an unauthenticated `GET /` waited 2.75 s). Each service has its own lock, which today guards nothing; the store is shared safely with other processes through the journal. Moving tool bodies to worker threads is a future change that needs its own race tests: a sweep or a close racing an in-flight call would give `internal: service is closed`. **Superseded by `2026-11-22-remote-worker-threads-design.md`:** tool calls now run in a bounded pool of worker threads with leased services; the text above describes the first implementation.

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
| Host not allowed | 421 |
| Origin not allowed | 403 |
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
- **Unchanged:** the protocol block and the installer goldens do not change; the eval scorecard (recall, leaks, abstention, held-out) does not move; recall latency over HTTP is measured and reported, not gated (200 `memory_recall` calls, quiet laptop: in-process app through starlette TestClient, median about 8.1 to 8.4 ms; stdio, about 5.3 to 5.4 ms).

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
7. **Own guard instead of the SDK token verifier.** Cost: about 150 lines of authentication plumbing we maintain and test; benefit: a fixed 401 with no OAuth routes and a single audited path.

### Known limits left

- A revoke during an in-flight request can leave a service open for the revoked key until the next token-file change or shutdown (not a bypass: the guard refuses the token).
- `created_at` has one-second resolution, so revoking and re-creating a same-name same-role token within one second inherits the old service.
- `--public-url` is validated and its host lower-cased, but a trailing dot or a Host header in upper case is not normalised and fails closed with 421.
- Every throttled request still writes one WARNING line, so a flood of bad tokens is a flood of log lines.
- Tool calls no longer block one another: they run in a worker pool, and the limits that remain (queueing over the cap, one token's calls serialised, shutdown drain) are in section 6 of `2026-11-22-remote-worker-threads-design.md`.
- The inner app still receives the Authorization header in its ASGI scope.

## 12. Findings from the spike and the build

- **Own ASGI guard, not the SDK's verifier.** FastMCP's token verifier is shaped for OAuth: it wants an issuer and resource URL and would publish OAuth metadata routes that point nowhere. Our guard serves no OAuth route, answers a fixed 401, and is the one audited path for authentication, the body cap and the failure limiter. Host and Origin checks stay with FastMCP's transport security, inside the guard.
- **Identity through a context variable.** The guard sets the verified principal in a context variable; FastMCP's stateless mode propagates it to the tool code (anyio copies the context in `task_group.start`), so the provider reads the caller of the current request.
- **One run per app.** `FastMCP.session_manager.run()` runs only once per instance, so tests build a new app each time.
- **Two services, one store.** Two services in one process share a store safely through the journal and the OS lock, and keep separate sessions. This was confirmed on Windows.
- **Stop signal.** uvicorn re-raises the captured stop signal (SIGTERM, Ctrl-Break) with the default handler after it has shut down, which can end the process before any code after `server.run()`. Closing the services from the lifespan shutdown is what leaves a checkpoint on a normal stop; an end-to-end test pins it.
- **Dependencies.** `uvicorn` and `starlette` come with `mcp`; no new dependency is added.
