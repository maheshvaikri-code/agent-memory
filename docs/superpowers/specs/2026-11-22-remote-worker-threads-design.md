# Remote server worker threads (sub-project 3b follow-up): design

Status: implemented (branch feature/remote-worker-threads); approved in conversation (approach 1, "let an in-flight call finish after a revoke"), written 2026-11-22. Changes the concurrency model described in section 5 ("Concurrency") of `2026-11-20-remote-mcp-design.md`; everything else in that spec stands.

## 1. Purpose and success

FastMCP 1.25.0 runs a plain `def` tool inline on the event loop. Over HTTP one slow call therefore delays every caller, including the authentication of new requests (measured: with one call held 3 s, an unauthenticated `GET /` waited 2.75 s). The slow cases are real: a store-lock wait of up to 10 s while another process checkpoints, and a recall over thousands of records.

It succeeds when:

- While one tool call is slow, an unauthenticated request is still answered promptly (target: well under 0.5 s with a call held for 3 s) and a call by a different token completes.
- A token revoked while its call is running gets that call's result. The token's next request is refused at the guard as before. The service is closed once its last call has finished, and never while a call is using it.
- A normal stop (uvicorn's graceful stop) delivers the result of every running call, waiting as long as the call takes, and then closes every service, each leaving its checkpoint. This is measured with a real server and pinned by a test (section 6).
- The authentication, role, identity, audit and scrubbing behaviour of the remote spec is unchanged, and so is the stdio server.
- Median `memory_recall` over the in-process HTTP app stays within 25% or 2 ms (whichever is larger) of the same measurement on `main` in the same session. (About 8 ms before this change.)

This is a responsiveness change, not a throughput change. Python's GIL means CPU-bound recalls do not run faster in parallel. What changes is that waiting (on the store lock, on disk) and long recalls no longer freeze the loop.

## 2. Design

### 2.1 Where tool bodies run

- `build_server` registers the eleven tools as `async def`. Each awaits one dispatcher with the same arguments it passes to `run()` today.
- **Stdio (a plain `service`)**: the dispatcher calls `run()` inline, on the loop, exactly as now. Same blocking behaviour, same errors, same results.
- **HTTP (a `provider`)**: the dispatcher runs `run()` in a worker thread with `anyio.to_thread.run_sync`, using one `anyio.CapacityLimiter`. The limiter is created on first use, inside the running loop (anyio 4 cannot build one outside a loop). The default cap is 8 and is the keyword `max_workers=8` of `build_app` (no CLI flag).
- The whole of `run()` runs in the worker: the provider call (which can open a service, a slow store open), the role check, the service method, the scrubbing, the audit observer. The token identity in `current_principal` reaches the thread because anyio copies the calling context.
- Calls over the cap queue inside the limiter. The guard (authentication, limits, 405) stays on the loop and never queues behind them.
- **Cancellation.** A client that disconnects does not abandon the call: it runs to completion (anyio's default, `abandon_on_cancel=False`). A write is never left half done, and a client's retry of a write is already idempotent.
- **Audit duration** is measured from when the call is accepted, so it includes the wait in the queue. A saturated server then shows up as long `ms` values.

### 2.2 Leases

`Principals` gains `lease(principal)`, a context manager that yields what `service_for` yields today (the service, or the read-only view) and holds the service open for its duration.

- Each cached service has a lease count and a `retired` flag, under the existing `Principals` lock.
- `lease()` runs the sweep, finds or opens the service, and takes a lease. The lease is released when the `with` block ends, on success, error or cancellation.
- The `Principals` lock guards only the bookkeeping. A service is opened (which can wait up to 10 s for the store's file lock) and the services a sweep finds idle are closed outside it, so one token's first call never holds up another token's lease or a finishing call's release. If two first calls for one token race, one service is kept and the other is closed; if `close()` runs while a service is being opened, the new service is closed and the call gets the `internal` shutting-down error.
- **The sweep** pops the service of a token that left the file. With no lease on it, it is closed now (as today). With leases, it is marked retired and closed by the release of its last lease. A retired service gets no new lease: its token is gone, so the guard refuses the token before a new call reaches it.
- **`close()`** refuses new leases, closes every idle service, then waits for the leases on the others to drain for at most `drain_seconds` (default 15). After that it closes the rest anyway and logs a warning naming how many services were still leased (not how many calls). `DRAIN_SECONDS` applies only to a direct `Principals.close()` made while leases are held: the `run_http` `finally` path and tests. At a normal stop of the server no lease is held when the lifespan shutdown runs (see section 6), so `close()` closes every service at once. `MemoryService.close()` itself takes the service lock, so a close also waits for a call already inside a method.
- `service_for(principal)` stays, unchanged in behaviour, for tests and callers that do not hold a lease. `run()` uses `lease()`.
- A token's service keeps its own lock, so two calls by the same token still run one after the other. That preserves the per-token session semantics. Calls by different tokens run at the same time, and meet only at the store's journal and OS file lock, which already coordinate several services in one process.

### 2.3 What does not change

The guard, token file, roles, the read-only view, the error codes and messages, path scrubbing, the audit line format, the tool list and descriptions, and every stdio path.

## 3. Failure modes and answers

| Situation | Result |
|---|---|
| Revoke while that token's call runs | The call completes. The service closes after its last lease. The next request gets the fixed 401. |
| Sweep races a call that is about to enter its method | The lease was taken first, so the service is retired, not closed. |
| Normal stop with calls running | uvicorn waits for the calls as long as they take and delivers their results; the lifespan shutdown then closes every service at once (no lease is held by then), each leaving a checkpoint. No call fails with `internal` and no warning is logged. A hung call delays the stop until the process is killed. |
| A call raises or the client disconnects | The lease is released in a `finally`. The count returns to zero. |
| More calls than workers | They queue. Authentication and other guard answers are unaffected. A queued call has no timeout of its own (see limits). |
| Two calls by one token | Serialised by the service lock, in arrival order as far as the lock allows. |
| A store-lock wait of 10 s | Only that call (and any same-token calls behind it) wait. |

## 4. Tests

Concurrency tests use a gate (a `threading.Event`) inside a patched service method, so timing never depends on machine speed. Tests that measure a delay have generous bounds and are asserted on ordering, not on a stopwatch alone.

1. A held call does not delay an unauthenticated request (401 within 0.5 s) or a different token's call.
2. Two calls by one token run one after the other. Calls by two tokens overlap.
3. Revoke during a call: the call returns ok, the service is closed after the call, the token's next request is 401, `open_count` falls.
4. Shutdown during a call: the call returns ok, then every service closes and the store has a checkpoint. A direct `Principals.close()` made while a call holds a lease and outlives `drain_seconds` returns after about that long, logs the warning, and does not hang.
5. With `max_workers=2` and three held calls, the third does not start until one ends, and the guard still answers meanwhile.
6. The lease count returns to zero after a call that raises a `ServiceError`, an unexpected exception, and a cancelled request.
7. The audit `ms` of a queued call includes its wait; the audit line still carries no argument, result or token.
8. The token's name reaches the note written from a worker thread (identity survives the thread hop); a read-only token still gets `forbidden` before the service is reached.
9. Stdio: the existing stdio and concurrency tests pass unchanged, and a stdio call still runs on the loop thread.
10. A stress run: 8 tokens, 200 mixed calls, a revoke and a re-create in the middle, no `service is closed` error and no deadlock.
11. End to end with a real `uvicorn` process: with one call held by a patched store lock, an unauthenticated request and a second token's recall are answered, and a graceful stop folds the journal.
12. The latency measurement of section 1, reported with the figures from `main` taken in the same session.

## 5. Docs

The README "Known limits" line "Calls are served one at a time ..." and the remote spec's Concurrency paragraph are replaced by the new model: calls run in a bounded worker pool (default 8), a slow call no longer delays authentication or other tokens, and the limits in section 6. The remote spec keeps its history and gets a pointer to this document.

## 6. Limits (stated, not fixed)

- Calls over the cap queue without a timeout. A flood of slow calls can make queued callers wait until their own client times out.
- The cap is a constructor argument, not a command-line flag.
- Calls by one token are serialised by its service lock, so a slow call delays that token's other calls (not other tokens').
- A disconnected client's call still runs to completion.
- A direct `Principals.close()` while a call holds a lease and outlives `drain_seconds` closes that service under the call, which can then fail with `internal`. A normal server stop does not do this.
- Shutdown, as measured with a real uvicorn server: a normal stop waits for a running call as long as it takes (uvicorn's graceful wait is unbounded by default), delivers the call's result to the client, and only then runs the lifespan shutdown. At that moment no lease is held, because mcp's session manager has already finished the calls, so `Principals.close` closes every service at once and each leaves a checkpoint. `DRAIN_SECONDS` therefore applies only to a direct `Principals.close()` while leases are held (the `run_http` `finally` path and tests). No call fails with `internal` and no warning is logged at a normal stop. A hung call delays the stop until the process is killed; even a second Ctrl+C (`force_exit`) waits for it, because the anyio worker threads cannot be abandoned and `MemoryService.close` takes the lock the call holds. `timeout_graceful_shutdown=15` was tried and is worse: the client gets HTTP 500 and an ASGI traceback, and the stop still takes as long as the call. It must not be added to `run_http`. A real-server test pins this (`test_a_stop_with_a_call_in_flight_delivers_its_result_and_leaves_a_checkpoint`).
- No per-token quota, no per-call timeout, no health endpoint. The other known limits of the remote spec stand.

## 7. Out of scope

Stdio; a process per token; per-call timeouts; per-token concurrency quotas; changing the storage layer or the service API; any change to recall quality.

## 8. Measured

Median `memory_recall` over 200 calls through the in-process HTTP app (`tests/remote_latency.py`), main and
this branch alternated in one session, three runs each:

| Run | main | branch |
|---|---|---|
| 1 | 6.41 ms | 6.61 ms |
| 2 | 6.11 ms | 6.34 ms |
| 3 | 5.93 ms | 6.32 ms |

The medians of medians are 6.11 ms on main and 6.34 ms on this branch (+0.23 ms, about +4%), inside the bound of section 1 (25% or 2 ms, whichever is larger).
With one call held, an unauthenticated request was answered in 16 to 47 ms over three runs of the real-server test (before this change: 2.75 s with the
call held for 3 s).
