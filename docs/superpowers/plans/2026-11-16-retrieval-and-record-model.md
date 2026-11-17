# Retrieval and Record Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `agent-memory` return the right memories for a coding task, and only those, with provenance and staleness on every record.

**Architecture:** A common envelope (author, evidence, scope, status and so on) is added to the existing typed graph nodes through one codec and one writer. A new `retrieval/` package ranks records by fusing a BM25 lexical channel, a file-scope channel, a one-hop graph channel and an optional embedding channel, then applies a relevance floor and packs into a measured token budget. `get_context()` and `check_suggestion()` become thin views over the new `recall()` and `check()`.

**Tech Stack:** Python 3.9+, `ison-graph>=1.4.1`, pytest. No new required dependencies. `tiktoken` and `ison-graph-embeddings` are optional.

**Spec:** `docs/superpowers/specs/2026-11-16-retrieval-and-record-model-design.md`

## Global Constraints

- All 79 existing tests pass without edits. Run them with `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider` from the repo root. `PYTHONPATH=src` matters: pip has a different copy of this package installed, and without it the tests import that copy.
- `ison-graph` floor is `>=1.4.1`.
- No new required dependencies.
- No LLM call and no network call anywhere in `recall()` or `check()`.
- Records are never deleted by this code. Status changes only.
- Node ids must not be empty or contain whitespace, `:` or `|` (ISONGraph raises `InvalidIdentifierError`).
- A property missing on one node of a type loads back as `None`, not as absent. A list property loads back as a JSON string. Every reader goes through `record.py`.
- Public signatures and return shapes of `get_context`, `get_quick_context`, `check_suggestion` and every store method are unchanged.
- Files are read and written as UTF-8 explicitly. The development machine's default encoding is cp1252.
- This directory is the canonical copy. Do not edit `../isongraph/tools/agent-memory`.

## Deviations from the spec, decided while planning

These are reflected back into the spec file.

1. `text` is derived from the kind's fields at read time and is not persisted. Persisting it would let it drift from the fields it summarises.
2. No kind defaults to `priority="must"`. Guardrail and guidance kinds default to `should`, background kinds to `info`. `must` is always explicit. Otherwise every unscoped bug pattern would join the always-on pinned set.
3. The default token counter is `ceil(chars / 4)`. `tiktoken` is opt-in, because loading an encoding can trigger a download, which breaks the no-network rule.
4. Consolidation keeps its own retention thresholds. The registry replaces `TOKEN_ESTIMATES` and the `INDEXABLE_TYPES` table in `embeddings_integration.py` only. `cache_integration.py` is left alone.
5. `check()` also accepts a single matching term when that term is distinctive (appears in at most two guardrail records). Without this, "switch the ledger to MongoDB" would not find the rejected alternative "MongoDB".
6. A public `anchor(id, path, symbol="")` method is added so records created through store methods can be anchored.
7. Graph neighbours inherit half the seed's fused score directly, instead of entering rank fusion as a fourth list.

## Review Focus

Inputs the spec implies but does not spell out, most likely first. Each has a test in the task named.

1. **Windows paths.** `files=["services\\payments\\webhook.py"]` or an absolute path under the repo must match records anchored to `services/payments/webhook.py`. (Task 4)
2. **Empty query.** `recall(task="", files=[])` and `recall(task="the and of")` return an empty result without raising. (Task 8)
3. **Legacy file from disk.** A `.memory` file written before this change, where nodes of one type have different columns, loads with defaults and produces no crash on `recall()`. (Task 2)
4. **Tiny or zero budget.** `max_tokens=0` and `max_tokens=5` return an empty prompt, not a truncated line and not an exception. (Task 8)
5. **Anchor to a path that cannot be a node id or cannot be read.** A path containing a space, a path outside the repo, and a binary file are all accepted; the anchor is stored and its state is `unknown` when it cannot be hashed. (Task 4)

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/agent_memory/record.py` | create | Kind registry, `MemoryRecord`, envelope codec, id and text helpers |
| `src/agent_memory/anchors.py` | create | Path normalisation, content hashing, anchor edges, staleness, `head_commit` |
| `src/agent_memory/writer.py` | create | `MemoryWriter`: add, merge, supersede, retract, feedback |
| `src/agent_memory/retrieval/__init__.py` | create | Re-exports |
| `src/agent_memory/retrieval/tokenize.py` | create | Tokeniser and stemmer |
| `src/agent_memory/retrieval/lexical.py` | create | BM25 |
| `src/agent_memory/retrieval/channels.py` | create | Scope and graph channels |
| `src/agent_memory/retrieval/budget.py` | create | `TokenCounter` |
| `src/agent_memory/retrieval/render.py` | create | Markdown rendering |
| `src/agent_memory/retrieval/retriever.py` | create | `Retriever`, `ScoredMemory`, `RecallResult` |
| `src/agent_memory/stores/base.py` | modify | `writer` attribute and `_add_memory()` |
| `src/agent_memory/stores/*.py` | modify | 26 `add_node` call sites switch to `_add_memory()` |
| `src/agent_memory/memory.py` | modify | New public methods; store wiring deduplicated |
| `src/agent_memory/context.py` | modify | `CodingContext` as a view over `RecallResult` |
| `src/agent_memory/consolidation.py` | modify | Archive instead of delete; real duplicate merge |
| `src/agent_memory/embeddings_integration.py` | modify | Index registry kinds by `text` |
| `tests/eval/` | create | Scenario world, harness, baseline, threshold tests |
| `tests/test_record.py`, `test_anchors.py`, `test_writer.py`, `test_tokenize_lexical.py`, `test_retriever.py`, `test_check.py`, `test_context_compat.py`, `test_consolidation.py`, `test_store_migration.py` | create | Unit tests |

---

### Task 0: Baseline

**Files:**
- Create: `.gitignore`
- Modify: `pyproject.toml` (dependency line)

**Interfaces:**
- Produces: a git repository with a clean first commit and `ison-graph` 1.4.1 installed.

- [ ] **Step 1: Confirm git with the user, then initialise**

This directory is not a git repository. Ask the user before running `git init`. If they decline, skip every commit step in this plan.

```bash
git init
```

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.egg-info/
.pytest_cache/
.benchmarks/
*.pyc
```

- [ ] **Step 3: Raise the dependency floor**

In `pyproject.toml`, replace `"ison-graph>=1.2.0",` with `"ison-graph>=1.4.1",`.

- [ ] **Step 4: Upgrade the installed package**

Run: `python -m pip install --user "ison-graph>=1.4.1"`
Then: `python -c "import ison_graph; print(ison_graph.__version__)"`
Expected: `1.4.1`

- [ ] **Step 5: Run the existing suite**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider`
Expected: `79 passed, 1 skipped`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: baseline, ison-graph>=1.4.1"
```

---

### Task 1: Evaluation world, harness and baseline

The harness judges an implementation by what appears in its rendered prompt, so the same scenarios can score the code as it is today and the code after this plan.

**Files:**
- Create: `tests/eval/__init__.py` (empty)
- Create: `tests/eval/world.py`
- Create: `tests/eval/scenarios.py`
- Create: `tests/eval/harness.py`
- Create: `tests/eval/run.py`
- Create: `tests/eval/BASELINE.md`

**Interfaces:**
- Produces:
  - `build_world(memory, root: pathlib.Path) -> None`
  - `MARKERS: Dict[str, str]` (name → unique substring), `PINNED: set[str]`
  - `SCENARIOS: List[Scenario]`
  - `evaluate(recall_fn, check_fn) -> Scorecard`, where `recall_fn(task: str, files: List[str]) -> str` and `check_fn(approach: str, files: List[str]) -> str`
  - `Scorecard.recall`, `.leaks`, `.abstention`, `.by_condition`, `.failures`

- [ ] **Step 1: Verify the store signatures the world relies on**

Run: `grep -n "    def add_\|    def supersede" src/agent_memory/stores/decisions.py src/agent_memory/stores/conventions.py src/agent_memory/stores/bugs.py src/agent_memory/stores/dependencies.py src/agent_memory/stores/performance.py src/agent_memory/stores/techdebt.py -A8`

Confirm the positional parameters used in Step 2 exist with these names: `add_decision(title, context)`, `add_alternative(decision_title, option, rejected_because)`, `add_convention(category, rule, enforced_by, exceptions, applies_to)`, `add_idiom(pattern, preferred_form, avoid)`, `add_pattern(name, signature, modules_affected)`, `add_fix_pattern(pattern_name, fix_template, caveats)`, `add_dependency(name, version, category)`, `add_quirk(dependency_name, description, severity)`, `add_workaround(quirk_description, solution)`, `add_hot_path(name, frequency, untouchable=)`, `add_constraint(operation, constraint, reason)`, `add_debt(location, description, severity)`, `add_refactoring_attempt(debt_location, approach, outcome, reason_failed)`. If a parameter name differs, pass that argument positionally in Step 2.

- [ ] **Step 2: Write `tests/eval/world.py`**

```python
"""One realistic memory store plus file tree, shared by every scenario."""
from pathlib import Path
from typing import Dict

# name -> substring that appears in exactly one memory's rendered text
MARKERS: Dict[str, str] = {
    "webhook_storm": "webhook_storm",
    "jitter": "jitter",
    "inc": "INC-2031",
    "serializable": "serializable",
    "mongodb": "MongoDB",
    "stampede": "cache_stampede",
    "colon": "colon separated",
    "512mb": "512MB",
    "chunk": "chunk pipeline",
    "fifteen": "15 minutes",
    "network_hop": "network hop",
    "leeway": "leeway",
    "reversible": "reversible",
    "timed_out": "timed out",
    "five_ms": "5ms",
    "apierror": "ApiError",
    "psycopg": "psycopg",
    "pricecontext": "PriceContext",
    "synchronously": "synchronously",
    "queued": "queued",
    "print_statements": "print statements",
    "structlog": "structlog",
    "pool_leak": "pool_leak",
    "overflow": "indexer_batch_overflow",
    "alpha": "alpha primary",
    "beta": "beta replica",
    "sftp": "SFTP",
    "email_dup": "email_retry_duplicate",
    "immutable": "immutable",
    "fixation": "fixation",
    "reindex_window": "reindex window",
    "secrets": "never log secrets",
}
PINNED = {"secrets"}

FILES = {
    "services/payments/webhook.py": "def handle_webhook(event):\n    return retry_delivery(event)\n\ndef retry_delivery(event):\n    pass\n",
    "services/payments/refunds.py": "def process_refund(event):\n    pass\n",
    "services/cache/lru.py": "class LruCache:\n    def get(self, key):\n        pass\n",
    "services/auth/tokens.py": "def issue_token(user):\n    pass\n\ndef check_expiry(token):\n    pass\n",
    "services/notifications/email.py": "def send_email(msg):\n    pass\n",
    "services/search/indexer.py": "def bulk_index(docs):\n    pass\n",
    "db/migrations/runner.py": "def run_all():\n    pass\n",
    "db/pool.py": "def get_connection():\n    pass\n",
    "api/handlers/orders.py": "def get_order(order_id):\n    pass\n",
    "web/landing.css": "body { margin: 0 }\n",
    "web/checkout.tsx": "export const Checkout = () => null\n",
    "web/login.tsx": "export const Login = () => null\n",
    "legacy/billing_export.py": "def export():\n    pass\n",
}


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def build_world(memory, root: Path) -> None:
    """Populate `memory` and the file tree under `root`.

    Uses only store methods that exist before this plan, then applies the
    new record operations when the implementation has them. That lets the
    same world score the old and the new code.
    """
    for rel, content in FILES.items():
        _write(root, rel, content)
    new_api = hasattr(memory, "supersede") and hasattr(memory, "anchor")
    m = memory

    # --- payments
    storm = m.bugs.add_pattern(
        "webhook_storm", "unbounded retry on external webhook floods the provider",
        modules_affected=["services/payments/webhook"])
    m.bugs.add_fix_pattern("webhook_storm", "cap retries at five attempts with jitter", "")
    m.decisions.add_decision("Cap payment webhook retries at 5", "INC-2031 unbounded retry storm")
    m.decisions.add_decision("Use Postgres for the billing ledger", "needs serializable transactions")
    m.decisions.add_alternative("Use Postgres for the billing ledger", "MongoDB",
                                "no multi-document transactions at the time")

    # --- cache
    m.bugs.add_pattern("cache_stampede",
                       "concurrent misses recompute the same key without a lock",
                       modules_affected=["services/cache"])
    m.conventions.add_convention("naming", "cache keys use colon separated namespaces",
                                 "manual", None, "services/cache")
    m.dependencies.add_dependency("redis", "5.0.1", "storage")
    m.dependencies.add_quirk("redis", "redis pipeline silently drops commands over 512MB payload", "high")
    m.dependencies.add_workaround("redis pipeline silently drops commands over 512MB payload",
                                  "chunk pipeline batches to 1000 commands")

    # --- auth
    m.decisions.add_decision("JWT access tokens expire after 15 minutes",
                             "limits the blast radius of a leaked token")
    m.decisions.add_alternative("JWT access tokens expire after 15 minutes",
                                "server side sessions in Redis",
                                "adds a network hop to every request")
    m.bugs.add_pattern("token_clock_skew",
                       "expiry check fails when service clocks differ; allow 30s leeway",
                       modules_affected=["services/auth/tokens"])
    m.bugs.add_pattern("csrf_token_reuse",
                       "csrf token reused after login allows session fixation",
                       modules_affected=["web/login"])

    # --- database
    m.conventions.add_convention("migrations",
                                 "every migration must be reversible with a down step",
                                 "manual", None, "db/migrations")
    m.techdebt.add_debt("db/migrations/runner.py",
                        "runner executes migrations without an advisory lock", "high")
    m.techdebt.add_refactoring_attempt("db/migrations/runner.py",
                                       "wrap all migrations in a single transaction",
                                       "failed", "DDL on large tables timed out")
    m.conventions.add_idiom("db_access", "use the get_connection context manager",
                            "opening raw psycopg connections")

    # --- api and performance
    m.performance.add_hot_path("order_lookup", "very_high", untouchable=True)
    m.performance.add_constraint("order_lookup", "<5ms p99", "checkout latency budget")
    m.conventions.add_convention("errors",
                                 "handlers raise ApiError with a stable code, never bare Exception",
                                 "manual", None, "api/handlers")

    # --- web
    m.conventions.add_convention("frontend",
                                 "checkout components read prices from the PriceContext provider",
                                 "manual", None, "web/checkout")
    m.conventions.add_convention("assets",
                                 "http cache headers on landing assets are set immutable",
                                 "manual", None, "web/landing")

    # --- search
    m.conventions.add_convention("search",
                                 "search index migration requires a full reindex window",
                                 "manual", None, "services/search")
    overflow = m.bugs.add_pattern("indexer_batch_overflow",
                                  "search indexer batch above 500 documents overflows the bulk request",
                                  modules_affected=["services/search/indexer"])
    alpha = m.bugs.add_pattern("indexer_retry_a",
                               "indexer shard alpha primary: bulk request retries need backoff",
                               modules_affected=["services/search/indexer"])

    # --- notifications
    m.bugs.add_pattern("email_retry_duplicate",
                       "retry of email send without idempotency key delivers duplicates",
                       modules_affected=["services/notifications/email"])

    # --- records that should not surface, or should surface flagged
    old_refund = m.decisions.add_decision("Refund webhooks are processed synchronously",
                                          "simplest thing at launch")
    old_log = m.conventions.add_convention("logging",
                                           "log with print statements prefixed by module",
                                           "manual", None, "*")
    leak = m.bugs.add_pattern("pool_leak",
                              "connections leak when get_connection raises inside the pool",
                              modules_affected=["db/pool"])
    sftp = m.decisions.add_decision("Billing export writes CSV nightly to the SFTP drop",
                                    "finance pulls it each morning")

    if new_api:
        m.anchor(storm, "services/payments/webhook.py", "retry_delivery")
        m.anchor(overflow, "services/search/indexer.py")
        m.anchor(alpha, "services/search/indexer.py")
        m.anchor(sftp, "legacy/billing_export.py")
        # the indexer changes after `overflow` and `alpha` were recorded
        _write(root, "services/search/indexer.py",
               "def bulk_index(docs, batch=200):\n    pass\n")
        beta = m.bugs.add_pattern("indexer_retry_b",
                                  "indexer shard beta replica: bulk request retries need backoff",
                                  modules_affected=["services/search/indexer"])
        m.anchor(beta, "services/search/indexer.py")
        (root / "legacy" / "billing_export.py").unlink()
        new_refund = m.supersede(old_refund, "decision", {
            "title": "Refund webhooks are queued and processed by a worker",
            "context": "synchronous handling timed the provider out",
            "status": "accepted",
        })
        m.anchor(new_refund.id, "services/payments/refunds.py")
        m.supersede(old_log, "convention", {
            "category": "logging", "rule": "use structlog with a bound request id",
            "enforced_by": "manual", "exceptions": "", "applies_to": "*",
        })
        m.retract(leak, "could not reproduce; was a test artefact")
        m.remember("convention", {
            "category": "security", "rule": "never log secrets or tokens",
            "enforced_by": "manual", "exceptions": "", "applies_to": "*",
        }, priority="must")
    else:
        m.bugs.add_pattern("indexer_retry_b",
                           "indexer shard beta replica: bulk request retries need backoff",
                           modules_affected=["services/search/indexer"])
        m.decisions.add_decision("Refund webhooks are queued and processed by a worker",
                                 "synchronous handling timed the provider out")
        m.conventions.add_convention("logging", "use structlog with a bound request id",
                                     "manual", None, "*")
        m.conventions.add_convention("security", "never log secrets or tokens",
                                     "manual", None, "*")
```

- [ ] **Step 3: Write `tests/eval/scenarios.py`**

```python
"""48 scenarios, six per condition. Expectations are marker names from world.MARKERS."""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Scenario:
    condition: str
    task: str
    files: List[str] = field(default_factory=list)
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    empty: bool = False                           # nothing but the pinned set
    above: Optional[Tuple[str, str]] = None       # (first, second) in prompt order
    flagged: List[str] = field(default_factory=list)
    unflagged: List[str] = field(default_factory=list)
    check: bool = False                           # use check_fn, task is the approach


S = Scenario
W = "services/payments/webhook.py"
T = "services/auth/tokens.py"
L = "services/cache/lru.py"
I = "services/search/indexer.py"

SCENARIOS: List[Scenario] = [
    # relevant, task text only
    S("task_only", "add retry logic to the payment webhook handler", include=["webhook_storm"]),
    S("task_only", "why do we expire JWT access tokens so quickly", include=["fifteen"]),
    S("task_only", "speed up the order lookup query", include=["five_ms"]),
    S("task_only", "write a database migration for the orders table", include=["reversible"]),
    S("task_only", "redis pipeline is dropping commands on a large payload", include=["512mb"]),
    S("task_only", "concurrent cache misses recompute the same key", include=["stampede"]),
    # relevant, files only
    S("files_only", "", [W], include=["webhook_storm"]),
    S("files_only", "", [T], include=["leeway"]),
    S("files_only", "", [L], include=["stampede"]),
    S("files_only", "", ["db/migrations/0042_add_orders.py"], include=["reversible"]),
    S("files_only", "", ["api/handlers/orders.py"], include=["apierror"]),
    S("files_only", "", ["web/checkout.tsx"], include=["pricecontext"]),
    # absent: nothing stored about the topic
    S("absent", "add a dark mode toggle to the settings screen", empty=True),
    S("absent", "generate PDF invoices with a watermark", empty=True),
    S("absent", "translate onboarding copy into German", empty=True),
    S("absent", "upgrade the kubernetes ingress controller", empty=True),
    S("absent", "add a GraphQL subscription for inventory levels", empty=True),
    S("absent", "resize uploaded avatars to thumbnails", empty=True),
    # irrelevant: unrelated task and unrelated files
    S("irrelevant", "tidy the changelog formatting", ["docs/CHANGELOG.md"], empty=True),
    S("irrelevant", "bump the copyright year in the footer", ["site/footer.html"], empty=True),
    S("irrelevant", "rename the CI job for lint", [".github/workflows/lint.yml"], empty=True),
    S("irrelevant", "fix a typo in the contributor guide", ["docs/CONTRIBUTING.md"], empty=True),
    S("irrelevant", "adjust editor indentation settings", [".editorconfig"], empty=True),
    S("irrelevant", "add a license banner to the scripts", ["scripts/release.sh"], empty=True),
    # adjacent: same vocabulary, different module (pairs are in the same tier)
    S("adjacent", "add retry to the payment webhook", [W], above=("webhook_storm", "email_dup")),
    S("adjacent", "name the cache keys for the lru", [L], above=("colon", "immutable")),
    S("adjacent", "token expiry check fails across services", [T], above=("leeway", "fixation")),
    S("adjacent", "write a reversible migration", ["db/migrations/0043_x.py"],
      above=("reversible", "reindex_window")),
    S("adjacent", "retry the email send safely", ["services/notifications/email.py"],
      above=("email_dup", "webhook_storm")),
    S("adjacent", "set http cache headers for landing assets", ["web/landing.css"],
      above=("immutable", "colon")),
    # superseded and retracted
    S("superseded", "how are refund webhooks processed", include=["queued"], exclude=["synchronously"]),
    S("superseded", "", ["services/payments/refunds.py"], include=["queued"], exclude=["synchronously"]),
    S("superseded", "add logging to the order handler", include=["structlog"], exclude=["print_statements"]),
    S("superseded", "what is our logging convention", include=["structlog"], exclude=["print_statements"]),
    S("superseded", "connection pool leak when get_connection raises", exclude=["pool_leak"]),
    S("superseded", "refund worker queue processing", include=["queued"], exclude=["synchronously"]),
    # stale
    S("stale", "search indexer batch overflow", [I], include=["overflow"], flagged=["overflow"]),
    S("stale", "indexer bulk request retries backoff", [I], above=("beta", "alpha")),
    S("stale", "billing export CSV to SFTP", include=["sftp"], flagged=["sftp"]),
    S("stale", "add retry logic to the payment webhook", [W], include=["webhook_storm"],
      unflagged=["webhook_storm"]),
    S("stale", "", [I], include=["overflow"], flagged=["overflow"]),
    S("stale", "nightly billing export", include=["sftp"], flagged=["sftp"]),
    # guardrail check
    S("guardrail", "Retry indefinitely with exponential backoff", [W], include=["webhook_storm"], check=True),
    S("guardrail", "store sessions server side in Redis instead of JWT", [T],
      include=["network_hop"], check=True),
    S("guardrail", "switch the billing ledger to MongoDB", include=["mongodb"], check=True),
    S("guardrail", "wrap all the migrations in one transaction", ["db/migrations/runner.py"],
      include=["timed_out"], check=True),
    S("guardrail", "recompute the cache value on every miss without locking", [L],
      include=["stampede"], check=True),
    S("guardrail", "skip the leeway and compare expiry timestamps exactly", [T],
      include=["leeway"], check=True),
]
```

- [ ] **Step 4: Write `tests/eval/harness.py`**

```python
"""Scores an implementation from the text it renders."""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from .scenarios import SCENARIOS, Scenario
from .world import MARKERS, PINNED

RecallFn = Callable[[str, List[str]], str]


@dataclass
class Scorecard:
    include_hits: int = 0
    include_total: int = 0
    leaks: int = 0
    abstain_hits: int = 0
    abstain_total: int = 0
    by_condition: Dict[str, List[bool]] = field(default_factory=lambda: defaultdict(list))
    failures: List[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.include_hits / self.include_total if self.include_total else 1.0

    @property
    def abstention(self) -> float:
        return self.abstain_hits / self.abstain_total if self.abstain_total else 1.0

    def to_markdown(self) -> str:
        rows = ["| Condition | Passed |", "|---|---|"]
        for cond, results in self.by_condition.items():
            rows.append(f"| {cond} | {sum(results)}/{len(results)} |")
        rows += ["",
                 f"- must-include recall: {self.recall:.2f} ({self.include_hits}/{self.include_total})",
                 f"- superseded or retracted leaks: {self.leaks}",
                 f"- abstention: {self.abstention:.2f} ({self.abstain_hits}/{self.abstain_total})"]
        return "\n".join(rows)


def _block(text: str, marker: str) -> str:
    """The rendered item containing `marker`: its line plus indented lines after it."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if marker in line:
            out = [line]
            for nxt in lines[i + 1:]:
                if nxt.startswith("  "):
                    out.append(nxt)
                else:
                    break
            return "\n".join(out)
    return ""


def _judge(s: Scenario, text: str, card: Scorecard) -> bool:
    ok = True
    for name in s.include:
        card.include_total += 1
        if MARKERS[name] in text:
            card.include_hits += 1
        else:
            ok = False
    for name in s.exclude:
        if MARKERS[name] in text:
            card.leaks += 1
            ok = False
    if s.empty:
        card.abstain_total += 1
        present = [n for n, mk in MARKERS.items() if mk in text and n not in PINNED]
        if present:
            ok = False
        else:
            card.abstain_hits += 1
    if s.above:
        first, second = (text.find(MARKERS[n]) for n in s.above)
        if first < 0 or (second >= 0 and second < first):
            ok = False
    for name in s.flagged:
        if "⚠" not in _block(text, MARKERS[name]):
            ok = False
    for name in s.unflagged:
        if "⚠" in _block(text, MARKERS[name]):
            ok = False
    return ok


def evaluate(recall_fn: RecallFn, check_fn: RecallFn) -> Scorecard:
    card = Scorecard()
    for s in SCENARIOS:
        text = (check_fn if s.check else recall_fn)(s.task, s.files)
        ok = _judge(s, text, card)
        card.by_condition[s.condition].append(ok)
        if not ok:
            card.failures.append(f"[{s.condition}] {s.task!r} {s.files}")
    return card
```

- [ ] **Step 5: Write `tests/eval/run.py`**

```python
"""Print the scorecard: PYTHONPATH=src python -m tests.eval.run"""
import json
import sys
import tempfile
from pathlib import Path

from agent_memory import AgentMemory

from .harness import evaluate
from .world import build_world


def make_fns(memory):
    if hasattr(memory, "recall"):
        recall_fn = lambda task, files: memory.recall(task=task, files=files, max_tokens=2000).to_prompt()
        check_fn = lambda approach, files: "\n".join(
            f"- [{w.record.id}] {w.text}" for w in memory.check(approach=approach, files=files))
    else:
        recall_fn = lambda task, files: memory.get_context(task=task, files=files, max_tokens=2000).to_prompt()
        check_fn = lambda approach, files: json.dumps(memory.check_suggestion(approach))
    return recall_fn, check_fn


def build(root: Path):
    try:
        memory = AgentMemory("shopline", repo_root=str(root))
    except TypeError:
        memory = AgentMemory("shopline")
    build_world(memory, root)
    return memory


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        memory = build(Path(tmp))
        card = evaluate(*make_fns(memory))
    out = card.to_markdown()
    if card.failures:
        out += "\n\nFailures:\n" + "\n".join(f"- {f}" for f in card.failures)
    sys.stdout.buffer.write((out + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run the baseline against the current code**

Run: `PYTHONPATH=src python -m tests.eval.run`
Expected: a table prints. The numbers will be poor; that is the point. If `build_world` raises, fix the argument order per Step 1 and rerun. Do not change expectations to make the old code look better.

- [ ] **Step 7: Save the output**

Write `tests/eval/BASELINE.md` with a first line `# Baseline: implementation before the retrieval rework (2026-11-16)` followed by the exact output of Step 6.

- [ ] **Step 8: Confirm the existing suite still passes and commit**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider`
Expected: `79 passed, 1 skipped`

```bash
git add tests/eval
git commit -m "test: evaluation world, harness and baseline scorecard"
```

---

### Task 2: Record model (`record.py`)

**Files:**
- Create: `src/agent_memory/record.py`
- Test: `tests/test_record.py`

**Interfaces:**
- Produces:
  - `KINDS: Dict[str, KindSpec]`; `KindSpec(tier, template, half_life_days, scope_field)`
  - `GUARDRAIL`, `GUIDANCE`, `BACKGROUND`, `TIER_ORDER`
  - `build_text(kind: str, props: Mapping) -> str`
  - `normalize_text(text: str) -> str`
  - `make_id(kind: str, text: str) -> str`
  - `utc_now() -> str`
  - `envelope_props(*, agent, session_id, source, evidence, commit, scope, priority, confidence) -> Dict[str, Any]`
  - `MemoryRecord` with attributes `kind, id, fields, text, created_at, updated_at, agent, session_id, source, evidence, commit, scope, priority, confidence, status, invalid_at, helpful, harmful`, property `ref`, property `tier`, and `MemoryRecord.from_node(node, warnings: Optional[List[str]] = None)`
  - `ENVELOPE_FIELDS`, `PRIORITIES`, `STATUSES`

- [ ] **Step 1: Write the failing tests**

`tests/test_record.py`:

```python
import json

import pytest
from ison_graph import ISONGraph

from agent_memory.record import (
    BACKGROUND, GUARDRAIL, KINDS, MemoryRecord, build_text, envelope_props,
    make_id, normalize_text,
)


def test_build_text_uses_kind_template():
    assert build_text("bug_pattern", {"name": "webhook_storm", "signature": "unbounded retry"}) \
        == "webhook_storm: unbounded retry"
    assert build_text("alternative", {"option": "MongoDB", "rejected_because": "no transactions"}) \
        == "Rejected: MongoDB — no transactions"


def test_build_text_drops_missing_parts():
    assert build_text("decision", {"title": "Use Postgres", "context": None}) == "Use Postgres"
    assert build_text("convention", {"category": "naming", "rule": "snake_case"}) == "[naming] snake_case"


def test_make_id_is_stable_and_prefix_safe():
    a = make_id("convention", "[naming] snake_case functions everywhere")
    b = make_id("convention", "[naming] snake_case functions everywhere")
    c = make_id("convention", "[naming] snake_case functions everywhere else")
    assert a == b and a != c
    assert not any(ch in a for ch in " :|")
    assert a.startswith("convention_")


def test_from_node_fills_legacy_defaults():
    g = ISONGraph(name="t")
    g.add_node("convention", "conv_x", category="naming", rule="snake_case",
               enforced_by="ruff", exceptions="", applies_to="services/cache")
    rec = MemoryRecord.from_node(g.get_node("convention", "conv_x"))
    assert rec.source == "legacy" and rec.status == "active"
    assert rec.confidence == 0.6
    assert rec.scope == ["*services/cache*"]
    assert rec.priority == "should"
    assert rec.text == "[naming] snake_case"


def test_envelope_round_trips_through_a_file(tmp_path):
    g = ISONGraph(name="t")
    env = envelope_props(agent="codex", session_id="s1", source="explicit", evidence="PR 12",
                         commit="abc", scope=["src/**", "lib/*.py"], priority="must", confidence=0.9)
    g.add_node("decision", "d1", title="Use Postgres", context="transactions", **env)
    # a second node of the same type without the envelope: its columns load as None
    g.add_node("decision", "d2", title="Old one", context="")
    path = tmp_path / "m.memory"
    g.save(str(path))
    h = ISONGraph.load(str(path))
    new = MemoryRecord.from_node(h.get_node("decision", "d1"))
    old = MemoryRecord.from_node(h.get_node("decision", "d2"))
    assert (new.agent, new.scope, new.priority, new.confidence) == ("codex", ["src/**", "lib/*.py"], "must", 0.9)
    assert (old.agent, old.scope, old.priority, old.source) == ("unknown", ["*"], "should", "legacy")


def test_decision_status_aliases():
    g = ISONGraph(name="t")
    g.add_node("decision", "a", title="A", status="accepted")
    g.add_node("decision", "b", title="B", status="superseded")
    g.add_node("decision", "c", title="C", status="deprecated")
    get = lambda i: MemoryRecord.from_node(g.get_node("decision", i)).status
    assert (get("a"), get("b"), get("c")) == ("active", "superseded", "archived")


def test_malformed_envelope_falls_back_and_warns():
    g = ISONGraph(name="t")
    g.add_node("decision", "d", title="T", scope="[not json", confidence="high", helpful="x")
    warnings = []
    rec = MemoryRecord.from_node(g.get_node("decision", "d"), warnings)
    assert rec.scope == ["*"] and rec.confidence == 0.6 and rec.helpful == 0
    assert warnings and all("d" in w for w in warnings)


def test_tiers_and_registry_shape():
    assert KINDS["bug_pattern"].tier == GUARDRAIL
    assert KINDS["open_question"].tier == BACKGROUND
    assert "session" not in KINDS and "task" not in KINDS
    assert normalize_text("  Use   Postgres ") == "use postgres"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_record.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent_memory.record'`

- [ ] **Step 3: Implement `src/agent_memory/record.py`**

```python
"""
Record model: which node types are memories, the envelope every memory
carries, and the one place that converts between node properties and
MemoryRecord.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

NodeRef = Tuple[str, Any]

GUARDRAIL, GUIDANCE, BACKGROUND = "guardrail", "guidance", "background"
TIER_ORDER = (GUARDRAIL, GUIDANCE, BACKGROUND)

PRIORITIES = ("must", "should", "info")
STATUSES = ("active", "superseded", "retracted", "archived")
SOURCE_CONFIDENCE = {"explicit": 0.7, "extracted": 0.5, "bootstrap": 0.5,
                     "imported": 0.6, "legacy": 0.6}
ENVELOPE_FIELDS = ("created_at", "updated_at", "agent", "session_id", "source",
                   "evidence", "commit", "scope", "priority", "confidence",
                   "status", "invalid_at", "helpful", "harmful")

# DecisionStore already writes a `status` with its own vocabulary.
_STATUS_ALIASES = {"accepted": "active", "proposed": "active", "deprecated": "archived"}
_LEGACY_TIME_FIELDS = ("added", "date", "started", "when", "discovered", "identified")


@dataclass(frozen=True)
class KindSpec:
    tier: str
    template: str
    half_life_days: Optional[int] = None
    scope_field: Optional[str] = None


KINDS: Dict[str, KindSpec] = {
    # guardrails: prior negative experience and hard limits
    "alternative": KindSpec(GUARDRAIL, "Rejected: {option} — {rejected_because}"),
    "bug_pattern": KindSpec(GUARDRAIL, "{name}: {signature}", scope_field="modules_affected"),
    "refactoring_attempt": KindSpec(GUARDRAIL, "Refactor attempt ({outcome}): {approach} — {reason_failed}"),
    "quirk": KindSpec(GUARDRAIL, "{description}"),
    "hot_path": KindSpec(GUARDRAIL, "Hot path {name} ({frequency})"),
    "performance_constraint": KindSpec(GUARDRAIL, "{operation}: {constraint} — {reason}"),
    # guidance: how things are done here
    "convention": KindSpec(GUIDANCE, "[{category}] {rule}", 365, "applies_to"),
    "idiom": KindSpec(GUIDANCE, "{pattern}: prefer {preferred_form}; avoid {avoid}", 365),
    "project_pattern": KindSpec(GUIDANCE, "{name}: {template} — {usage_context}", 365),
    "decision": KindSpec(GUIDANCE, "{title} — {context}", 365),
    "fix_pattern": KindSpec(GUIDANCE, "Fix: {fix_template} — {caveats}", 365),
    "workaround": KindSpec(GUIDANCE, "Workaround: {solution}{description}", 365),
    "test_convention": KindSpec(GUIDANCE, "[test:{category}] {pattern}", 365),
    "required_test_case": KindSpec(GUIDANCE, "Tests for {for_pattern} must cover: {cases_required}", 365),
    "template": KindSpec(GUIDANCE, "Template {name} ({category})", 365),
    "snippet": KindSpec(GUIDANCE, "{name}: {code} — {usage_context}", 365),
    # background: useful when relevant
    "module": KindSpec(BACKGROUND, "{path}: {purpose}", 180, "path"),
    "design_pattern": KindSpec(BACKGROUND, "{name} — {why_chosen}", 180, "where_used"),
    "tech_debt": KindSpec(BACKGROUND, "{location}: {description}", 180, "location"),
    "bug": KindSpec(BACKGROUND, "{title} — {root_cause}", 180, "module"),
    "expertise_area": KindSpec(BACKGROUND, "{domain} ({depth})", 180, "modules"),
    "session_decision": KindSpec(BACKGROUND, "{decision} — {rationale}", 30),
    "open_question": KindSpec(BACKGROUND, "{question}", 30),
    "blocker": KindSpec(BACKGROUND, "{description}", 30),
    "partial_work": KindSpec(BACKGROUND, "{work_type} at {location}: {state}", 30, "location"),
}


class _Blank(dict):
    def __missing__(self, key):
        return ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_text(kind: str, props: Mapping[str, Any]) -> str:
    """The one-statement form of a memory, built from its kind's fields."""
    values = _Blank({k: ("" if v is None else str(v)) for k, v in props.items()})
    text = KINDS[kind].template.format_map(values)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" :—-;")


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def make_id(kind: str, text: str) -> str:
    norm = normalize_text(text)
    slug = re.sub(r"[^a-z0-9]+", "_", norm).strip("_")[:24].strip("_") or "x"
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:6]
    return f"{kind}_{slug}_{digest}"


def envelope_props(*, agent: str = "unknown", session_id: str = "", source: str = "explicit",
                   evidence: str = "", commit: str = "", scope: Optional[List[str]] = None,
                   priority: Optional[str] = None, confidence: Optional[float] = None) -> Dict[str, Any]:
    """Envelope as node properties. Unset scope and priority are stored empty
    so the kind's derived defaults apply on read."""
    now = utc_now()
    return {
        "created_at": now, "updated_at": now, "agent": agent, "session_id": session_id,
        "source": source, "evidence": evidence, "commit": commit,
        "scope": json.dumps(list(scope)) if scope else "",
        "priority": priority or "",
        "confidence": float(confidence if confidence is not None else SOURCE_CONFIDENCE.get(source, 0.6)),
        "status": "active", "invalid_at": "", "helpful": 0, "harmful": 0,
    }


def _derived_scope(spec: KindSpec, props: Mapping[str, Any]) -> List[str]:
    raw = props.get(spec.scope_field) if spec.scope_field else None
    if raw in (None, "", "*"):
        return ["*"]
    parts = [p.strip().replace("\\", "/") for p in str(raw).split(",")]
    globs = [f"*{p}*" for p in parts if p and p != "*"]
    return globs or ["*"]


@dataclass
class MemoryRecord:
    kind: str
    id: Any
    fields: Dict[str, Any]
    text: str
    created_at: str = ""
    updated_at: str = ""
    agent: str = "unknown"
    session_id: str = ""
    source: str = "legacy"
    evidence: str = ""
    commit: str = ""
    scope: List[str] = field(default_factory=lambda: ["*"])
    priority: str = "should"
    confidence: float = 0.6
    status: str = "active"
    invalid_at: str = ""
    helpful: int = 0
    harmful: int = 0

    @property
    def ref(self) -> NodeRef:
        return (self.kind, self.id)

    @property
    def tier(self) -> str:
        return KINDS[self.kind].tier

    @classmethod
    def from_node(cls, node, warnings: Optional[List[str]] = None) -> "MemoryRecord":
        props = node.properties
        spec = KINDS[node.type]

        def warn(name: str) -> None:
            if warnings is not None:
                warnings.append(f"{node.type}:{node.id}: bad value for {name!r}, using default")

        def text_of(name: str, default: str = "") -> str:
            v = props.get(name)
            return default if v is None or v == "" else str(v)

        source = text_of("source", "legacy")

        scope = _derived_scope(spec, props)
        raw_scope = props.get("scope")
        if isinstance(raw_scope, list):
            scope = [str(x) for x in raw_scope] or scope
        elif raw_scope not in (None, ""):
            try:
                parsed = json.loads(raw_scope)
                if not isinstance(parsed, list):
                    raise ValueError
                scope = [str(x) for x in parsed] or scope
            except (ValueError, TypeError):
                warn("scope")

        confidence = SOURCE_CONFIDENCE.get(source, 0.6)
        raw_conf = props.get("confidence")
        if raw_conf not in (None, ""):
            try:
                confidence = min(1.0, max(0.0, float(raw_conf)))
            except (ValueError, TypeError):
                warn("confidence")

        def count(name: str) -> int:
            v = props.get(name)
            if v in (None, ""):
                return 0
            try:
                return max(0, int(v))
            except (ValueError, TypeError):
                warn(name)
                return 0

        priority = text_of("priority")
        if priority not in PRIORITIES:
            priority = "info" if spec.tier == BACKGROUND else "should"

        status = _STATUS_ALIASES.get(text_of("status", "active"), text_of("status", "active"))
        if status not in STATUSES:
            warn("status")
            status = "active"

        created = text_of("created_at")
        if not created:
            for name in _LEGACY_TIME_FIELDS:
                if props.get(name):
                    created = str(props[name])
                    break

        fields = {k: v for k, v in props.items() if k not in ENVELOPE_FIELDS or k == "status"}
        return cls(
            kind=node.type, id=node.id, fields=fields, text=build_text(node.type, props),
            created_at=created, updated_at=text_of("updated_at", created),
            agent=text_of("agent", "unknown"), session_id=text_of("session_id"),
            source=source, evidence=text_of("evidence"), commit=text_of("commit"),
            scope=scope, priority=priority, confidence=confidence, status=status,
            invalid_at=text_of("invalid_at"), helpful=count("helpful"), harmful=count("harmful"),
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_record.py -q -p no:cacheprovider`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/record.py tests/test_record.py
git commit -m "feat: record model with kind registry and envelope codec"
```

---

### Task 3: Tokeniser and BM25

**Files:**
- Create: `src/agent_memory/retrieval/__init__.py` (empty for now)
- Create: `src/agent_memory/retrieval/tokenize.py`
- Create: `src/agent_memory/retrieval/lexical.py`
- Test: `tests/test_tokenize_lexical.py`

**Interfaces:**
- Produces:
  - `tokenize(text: str) -> List[str]`
  - `tokens_for(text: str) -> Tuple[str, ...]` (cached)
  - `BM25(docs: Mapping[Hashable, Sequence[str]])`, `.search(query_tokens) -> List[Hit]`, `.df: Counter`
  - `Hit(key, score: float, matched: frozenset)`

- [ ] **Step 1: Write the failing tests**

`tests/test_tokenize_lexical.py`:

```python
from agent_memory.retrieval.lexical import BM25
from agent_memory.retrieval.tokenize import tokenize, tokens_for


def test_splits_code_identifiers_and_paths():
    assert tokenize("handleWebhook retry_delivery services/payments/webhook.py") == \
        ["handle", "webhook", "retry", "delivery", "service", "payment", "webhook", "py"]


def test_stems_inflections_to_a_common_form():
    assert tokenize("retries retrying retried retry") == ["retry"] * 4
    assert tokenize("caching cache caches") == ["cach"] * 3
    assert tokenize("migrations migration") == ["migration"] * 2


def test_drops_stopwords_and_single_characters():
    assert tokenize("add the a fix to we must x") == []


def test_tokens_for_is_cached_and_immutable():
    assert tokens_for("Retry storm") is tokens_for("Retry storm")
    assert isinstance(tokens_for("Retry storm"), tuple)


def test_bm25_ranks_the_doc_with_more_query_terms_first():
    docs = {
        "a": tokenize("unbounded retry on external webhook"),
        "b": tokenize("retry of email send delivers duplicates"),
        "c": tokenize("cache keys use namespaces"),
    }
    hits = BM25(docs).search(tokenize("webhook retry"))
    assert [h.key for h in hits] == ["a", "b"]
    assert hits[0].matched == frozenset({"webhook", "retry"})


def test_bm25_empty_inputs():
    assert BM25({}).search(["x"]) == []
    assert BM25({"a": ["x"]}).search([]) == []


def test_equal_docs_get_equal_scores():
    docs = {"a": ["indexer", "alpha", "bulk"], "b": ["indexer", "beta", "bulk"]}
    hits = BM25(docs).search(["indexer", "bulk"])
    assert hits[0].score == hits[1].score
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_tokenize_lexical.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `retrieval/tokenize.py`**

Create an empty `src/agent_memory/retrieval/__init__.py`, then:

```python
"""Tokeniser for memory text and task descriptions. Code-aware, no dependencies."""
import re
from functools import lru_cache
from typing import List, Tuple

STOPWORDS = frozenset("""
a an and are as at be but by do does for from how i if in into is it its
no not of on or our so that the their then there this to was we what when
where which why will with you
add all any can change every fix make must need new should update use using
""".split())

_CAMEL = re.compile(r"([a-z0-9])([A-Z])")
_WORD = re.compile(r"[A-Za-z0-9]+")
_VOWELS = "aeiou"


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith("ied"):
        w = w[:-3] + "y"
    elif len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    if len(w) > 5 and w.endswith("ing"):
        w = w[:-3]
        if len(w) > 2 and w[-1] == w[-2] and w[-1] not in _VOWELS + "ls":
            w = w[:-1]
    elif len(w) > 4 and w.endswith("ed") and not w.endswith("eed"):
        w = w[:-2]
        if len(w) > 2 and w[-1] == w[-2] and w[-1] not in _VOWELS + "ls":
            w = w[:-1]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def tokenize(text: str) -> List[str]:
    out = []
    for word in _WORD.findall(_CAMEL.sub(r"\1 \2", text or "")):
        word = word.lower()
        if len(word) < 2 or word in STOPWORDS:
            continue
        out.append(_stem(word))
    return out


@lru_cache(maxsize=65536)
def tokens_for(text: str) -> Tuple[str, ...]:
    """Cached tokens. Unchanged records cost a dictionary lookup per recall."""
    return tuple(tokenize(text))
```

Note on the expected values in Step 1: with the trailing-`e` rule, `service` → `servic`, `handle` → `handl`, and `cache` → `cach`. Run the test, read the actual output of the first test, and if the stems differ from the literal list only by that trailing-`e` rule, correct the expected list in the test to the actual stems. The properties that matter are the second test (inflections collapse to one form) and the third (stopwords vanish); those must pass as written.

- [ ] **Step 4: Implement `retrieval/lexical.py`**

```python
"""BM25 over short texts, in memory."""
import math
from collections import Counter
from dataclasses import dataclass
from typing import FrozenSet, Hashable, List, Mapping, Sequence


@dataclass(frozen=True)
class Hit:
    key: Hashable
    score: float
    matched: FrozenSet[str]


class BM25:
    def __init__(self, docs: Mapping[Hashable, Sequence[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = {key: Counter(tokens) for key, tokens in docs.items()}
        self.length = {key: len(tokens) for key, tokens in docs.items()}
        self.n = len(self.tf)
        self.avg = (sum(self.length.values()) / self.n) if self.n else 0.0
        self.df: Counter = Counter()
        for counts in self.tf.values():
            self.df.update(counts.keys())

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(self, query_tokens: Sequence[str]) -> List[Hit]:
        query = list(dict.fromkeys(query_tokens))
        hits = []
        for key, tf in self.tf.items():
            matched = [t for t in query if t in tf]
            if not matched:
                continue
            norm = self.k1 * (1 - self.b + self.b * self.length[key] / self.avg) if self.avg else self.k1
            score = sum(self.idf(t) * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in matched)
            hits.append(Hit(key, score, frozenset(matched)))
        hits.sort(key=lambda h: (-h.score, str(h.key)))
        return hits
```

- [ ] **Step 5: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_tokenize_lexical.py -q -p no:cacheprovider`
Expected: 7 passed (after the stem-literal correction described in Step 3, if needed)

- [ ] **Step 6: Commit**

```bash
git add src/agent_memory/retrieval tests/test_tokenize_lexical.py
git commit -m "feat: code-aware tokeniser and BM25"
```

---

### Task 4: Anchors and staleness (`anchors.py`)

**Files:**
- Create: `src/agent_memory/anchors.py`
- Test: `tests/test_anchors.py`

**Interfaces:**
- Consumes: `NodeRef` from `record.py`
- Produces:
  - `norm_path(path, repo_root: Optional[str]) -> str` (repo-relative POSIX)
  - `file_node_id(path: str) -> str`
  - `content_hash(abs_path) -> Optional[str]`
  - `head_commit(repo_root: Optional[str]) -> str`
  - `add_anchor(graph, ref, path, symbol="", repo_root=None, commit="") -> None` (re-stamps if the anchor exists)
  - `anchor_paths(graph, ref) -> List[str]`
  - `restamp(graph, ref, repo_root) -> int`
  - `HashCache(repo_root)`
  - `staleness(graph, ref, cache: HashCache) -> str`, one of `fresh, drifted, broken, orphaned, unknown`
  - `STALENESS_FACTOR: Dict[str, float]`

- [ ] **Step 1: Write the failing tests**

`tests/test_anchors.py`:

```python
import os

from ison_graph import ISONGraph

from agent_memory.anchors import (
    HashCache, add_anchor, anchor_paths, content_hash, file_node_id, head_commit,
    norm_path, restamp, staleness,
)

REF = ("bug_pattern", "bp1")


def graph():
    g = ISONGraph(name="t")
    g.add_node("bug_pattern", "bp1", name="x", signature="y")
    return g


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def test_norm_path_handles_backslashes_and_absolute_paths(tmp_path):
    root = str(tmp_path)
    assert norm_path("services\\payments\\webhook.py", root) == "services/payments/webhook.py"
    assert norm_path("./a/b.py", root) == "a/b.py"
    absolute = os.path.join(root, "services", "payments", "webhook.py")
    assert norm_path(absolute, root) == "services/payments/webhook.py"
    assert norm_path("a\\b.py", None) == "a/b.py"


def test_file_node_id_is_a_valid_identifier():
    node_id = file_node_id("docs/my notes: v1|draft.md")
    assert not any(ch in node_id for ch in " :|")


def test_hash_ignores_line_endings_and_trailing_whitespace(tmp_path):
    a = write(tmp_path, "a.py", "x = 1\ny = 2\n")
    b = write(tmp_path, "b.py", "x = 1  \r\ny = 2\r\n")
    assert content_hash(a) == content_hash(b)
    assert content_hash(tmp_path / "missing.py") is None


def test_fresh_then_drifted_then_orphaned(tmp_path):
    g = graph()
    f = write(tmp_path, "src/a.py", "def handle():\n    pass\n")
    add_anchor(g, REF, "src/a.py", repo_root=str(tmp_path))
    assert staleness(g, REF, HashCache(str(tmp_path))) == "fresh"
    write(tmp_path, "src/a.py", "def handle():\n    return 1\n")
    assert staleness(g, REF, HashCache(str(tmp_path))) == "drifted"
    f.unlink()
    assert staleness(g, REF, HashCache(str(tmp_path))) == "orphaned"


def test_broken_when_the_anchored_symbol_disappears(tmp_path):
    g = graph()
    write(tmp_path, "src/a.py", "def handle():\n    pass\n")
    add_anchor(g, REF, "src/a.py", symbol="handle", repo_root=str(tmp_path))
    write(tmp_path, "src/a.py", "def process():\n    pass\n")
    assert staleness(g, REF, HashCache(str(tmp_path))) == "broken"


def test_worst_state_wins_and_restamp_clears_drift(tmp_path):
    g = graph()
    write(tmp_path, "a.py", "1\n")
    write(tmp_path, "b.py", "1\n")
    add_anchor(g, REF, "a.py", repo_root=str(tmp_path))
    add_anchor(g, REF, "b.py", repo_root=str(tmp_path))
    write(tmp_path, "b.py", "2\n")
    assert staleness(g, REF, HashCache(str(tmp_path))) == "drifted"
    assert restamp(g, REF, str(tmp_path)) == 2
    assert staleness(g, REF, HashCache(str(tmp_path))) == "fresh"
    assert sorted(anchor_paths(g, REF)) == ["a.py", "b.py"]


def test_unknown_without_root_or_anchors(tmp_path):
    g = graph()
    assert staleness(g, REF, HashCache(str(tmp_path))) == "unknown"
    add_anchor(g, REF, "a.py", repo_root=None)
    assert staleness(g, REF, HashCache(None)) == "unknown"


def test_awkward_paths_are_accepted(tmp_path):
    g = graph()
    write(tmp_path, "docs/my notes.md", "hello\n")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    add_anchor(g, REF, "docs/my notes.md", repo_root=str(tmp_path))
    add_anchor(g, REF, "blob.bin", repo_root=str(tmp_path))
    add_anchor(g, REF, "../outside/elsewhere.py", repo_root=str(tmp_path))
    assert "docs/my notes.md" in anchor_paths(g, REF)
    assert staleness(g, REF, HashCache(str(tmp_path))) in ("fresh", "orphaned")


def test_anchor_survives_save_and_load(tmp_path):
    g = graph()
    write(tmp_path, "src/a b.py", "1\n")
    add_anchor(g, REF, "src/a b.py", repo_root=str(tmp_path))
    g.save(str(tmp_path / "m.memory"))
    h = ISONGraph.load(str(tmp_path / "m.memory"))
    assert anchor_paths(h, REF) == ["src/a b.py"]
    assert staleness(h, REF, HashCache(str(tmp_path))) == "fresh"


def test_head_commit_reads_git_without_a_subprocess(tmp_path):
    assert head_commit(str(tmp_path)) == ""
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "main").write_text("abc123\n", encoding="utf-8")
    assert head_commit(str(tmp_path)) == "abc123"
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_anchors.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/agent_memory/anchors.py`**

```python
"""
Anchors tie a memory to files. Each ANCHORED_TO edge stores the content
hash the memory was written against, so staleness can be computed against
the working tree at read time. Staleness is never stored.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from .record import NodeRef

EDGE = "ANCHORED_TO"
STALENESS_FACTOR = {"fresh": 1.0, "unknown": 1.0, "drifted": 0.7, "broken": 0.4, "orphaned": 0.2}
_SEVERITY = {"fresh": 0, "drifted": 1, "broken": 2, "orphaned": 3}


def norm_path(path, repo_root: Optional[str] = None) -> str:
    """Repo-relative POSIX path. Paths outside the repo are returned as given, with forward slashes."""
    raw = str(path)
    posix = raw.replace("\\", "/")
    if repo_root and os.path.isabs(raw):
        root = str(Path(repo_root).resolve()).replace("\\", "/").rstrip("/")
        full = str(Path(raw).resolve()).replace("\\", "/")
        if full.lower().startswith(root.lower() + "/"):
            return full[len(root) + 1:]
        return full
    while posix.startswith("./"):
        posix = posix[2:]
    return posix


def file_node_id(path: str) -> str:
    """Node ids may not contain whitespace, ':' or '|'."""
    return quote(path, safe="/._-")


def _normalised_bytes(data: bytes) -> bytes:
    lines = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    return b"\n".join(line.rstrip() for line in lines).rstrip(b"\n")


def content_hash(abs_path) -> Optional[str]:
    try:
        data = Path(abs_path).read_bytes()
    except OSError:
        return None
    return hashlib.sha1(_normalised_bytes(data)).hexdigest()


def head_commit(repo_root: Optional[str]) -> str:
    """HEAD commit read straight from .git, or '' when unavailable."""
    if not repo_root:
        return ""
    try:
        git = Path(repo_root) / ".git"
        if git.is_file():
            pointer = git.read_text(encoding="utf-8").split(":", 1)[1].strip()
            git = (Path(repo_root) / pointer).resolve()
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head
        ref = head[4:].strip()
        ref_file = git / ref
        if ref_file.exists():
            return ref_file.read_text(encoding="utf-8").strip()
        packed = git / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    return ""


def _abs(path: str, repo_root: str) -> Path:
    return Path(path) if os.path.isabs(path) else Path(repo_root) / path


def add_anchor(graph, ref: NodeRef, path, symbol: str = "",
               repo_root: Optional[str] = None, commit: str = "") -> None:
    """Anchor `ref` to `path`, stamping the current hash. Re-stamps an existing anchor."""
    rel = norm_path(path, repo_root)
    target = ("file", file_node_id(rel))
    if not graph.has_node(*target):
        graph.add_node("file", target[1], path=rel)
    digest = content_hash(_abs(rel, repo_root)) if repo_root else None
    if graph.has_edge(EDGE, ref, target):
        if not symbol:
            symbol = str(graph.get_edge(EDGE, ref, target).properties.get("symbol") or "")
        graph.remove_edge(EDGE, ref, target)
    graph.add_edge(EDGE, ref, target, symbol=symbol, hash=digest or "", commit=commit)


def _anchors(graph, ref: NodeRef) -> List[Tuple[str, str, str]]:
    out = []
    for edge in graph.edges(EDGE, source=ref):
        if not graph.has_node(*edge.target):
            continue
        path = str(graph.get_node(*edge.target).properties.get("path") or "")
        out.append((path, str(edge.properties.get("symbol") or ""), str(edge.properties.get("hash") or "")))
    return out


def anchor_paths(graph, ref: NodeRef) -> List[str]:
    return [path for path, _, _ in _anchors(graph, ref)]


def restamp(graph, ref: NodeRef, repo_root: Optional[str]) -> int:
    """Re-record every anchor of `ref` at the current file hash. Returns the count."""
    anchors = _anchors(graph, ref)
    for path, symbol, _ in anchors:
        add_anchor(graph, ref, path, symbol, repo_root, head_commit(repo_root))
    return len(anchors)


class HashCache:
    """Per-call cache of file hash and text, so a file is read once per recall."""

    def __init__(self, repo_root: Optional[str]):
        self.repo_root = repo_root
        self._seen: Dict[str, Tuple[Optional[str], str]] = {}

    def get(self, path: str) -> Tuple[Optional[str], str]:
        if path not in self._seen:
            digest, text = None, ""
            try:
                data = _abs(path, self.repo_root).read_bytes()
                digest = hashlib.sha1(_normalised_bytes(data)).hexdigest()
                text = data.decode("utf-8", errors="replace")
            except OSError:
                pass
            self._seen[path] = (digest, text)
        return self._seen[path]


def staleness(graph, ref: NodeRef, cache: HashCache) -> str:
    """Worst state across the anchors of `ref`."""
    if not cache.repo_root:
        return "unknown"
    worst = None
    for path, symbol, stored in _anchors(graph, ref):
        if not stored:
            continue
        current, text = cache.get(path)
        if current is None:
            state = "orphaned"
        elif current == stored:
            state = "fresh"
        elif symbol and not re.search(r"\b" + re.escape(symbol) + r"\b", text):
            state = "broken"
        else:
            state = "drifted"
        if worst is None or _SEVERITY[state] > _SEVERITY[worst]:
            worst = state
    return worst or "unknown"
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_anchors.py -q -p no:cacheprovider`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/anchors.py tests/test_anchors.py
git commit -m "feat: file anchors with computed staleness"
```

---

### Task 5: Writer and the public write API

**Files:**
- Create: `src/agent_memory/writer.py`
- Modify: `src/agent_memory/stores/base.py` (constructor, new helper)
- Modify: `src/agent_memory/memory.py` (constructor, `load`, `start_session`, new methods)
- Test: `tests/test_writer.py`

**Interfaces:**
- Consumes: `record.py`, `anchors.py`, `retrieval.tokenize.tokens_for`
- Produces:
  - `WriteResult(ref, created: bool, merged: bool, similar: List[NodeRef])`, property `.id`
  - `MemoryWriter(graph, repo_root=None, agent="unknown")` with `.session_id`, `.add(kind, fields, *, node_id=None, anchors=(), scope=None, evidence="", priority=None, confidence=None, source="explicit") -> WriteResult`, `.supersede(old_ref, kind, fields, **kw) -> WriteResult`, `.retract(ref, reason="")`, `.feedback(ref, helpful: bool)`, `.find(memory_id) -> NodeRef`
  - `BaseStore.writer`, `BaseStore._add_memory(kind, node_id, **fields) -> str`
  - `AgentMemory(name="project", repo_root=None, agent="unknown")`; `AgentMemory.load(path, repo_root=None, agent="unknown")`
  - `AgentMemory.set_actor(agent, session_id="")`, `.remember(kind, fields, *, anchors=(), scope=None, evidence="", priority=None, confidence=None, source="explicit") -> WriteResult`, `.supersede(old_id, kind, fields, **kw) -> WriteResult`, `.retract(memory_id, reason="")`, `.feedback(memory_id, helpful)`, `.anchor(memory_id, path, symbol="")`, `.verify(memory_id) -> int`, `.load_warnings: List[str]`
  - `anchors` items are either a path string or a `(path, symbol)` tuple.

- [ ] **Step 1: Write the failing tests**

`tests/test_writer.py`:

```python
import pytest

from agent_memory import AgentMemory
from agent_memory.record import MemoryRecord


def rec(memory, result):
    return MemoryRecord.from_node(memory.graph.get_node(*result.ref))


def test_remember_stamps_the_envelope():
    m = AgentMemory("t", agent="codex")
    m.set_actor("codex", "s_1")
    r = m.remember("bug_pattern", {"name": "storm", "signature": "unbounded retry"},
                   evidence="INC-1", scope=["services/payments/**"])
    record = rec(m, r)
    assert r.created and not r.merged
    assert (record.agent, record.session_id, record.source) == ("codex", "s_1", "explicit")
    assert record.evidence == "INC-1" and record.scope == ["services/payments/**"]
    assert record.confidence == 0.7 and record.status == "active" and record.created_at


def test_same_text_merges_and_reinforces():
    m = AgentMemory("t")
    a = m.remember("bug_pattern", {"name": "storm", "signature": "unbounded retry"}, evidence="INC-1")
    b = m.remember("bug_pattern", {"name": "Storm", "signature": "unbounded   retry"}, evidence="INC-2")
    assert b.ref == a.ref and b.merged and not b.created
    record = rec(m, a)
    assert record.confidence == pytest.approx(0.8)
    assert "INC-1" in record.evidence and "INC-2" in record.evidence
    assert len(list(m.graph.nodes("bug_pattern"))) == 1


def test_near_duplicates_are_written_and_reported():
    m = AgentMemory("t")
    a = m.remember("convention", {"category": "naming", "rule": "cache keys use colon separated namespaces"})
    b = m.remember("convention", {"category": "naming", "rule": "cache keys use colon separated namespace prefixes"})
    assert b.created and a.ref in b.similar


def test_unknown_kind_and_bad_priority_are_rejected():
    m = AgentMemory("t")
    with pytest.raises(ValueError, match="Valid kinds"):
        m.remember("gossip", {"text": "x"})
    with pytest.raises(ValueError, match="priority"):
        m.remember("quirk", {"description": "x"}, priority="urgent")
    with pytest.raises(ValueError, match="no text"):
        m.remember("quirk", {"description": ""})


def test_supersede_links_and_invalidates_the_old_record():
    m = AgentMemory("t")
    old = m.remember("decision", {"title": "Process refunds synchronously", "context": "launch"})
    new = m.supersede(old.id, "decision", {"title": "Queue refunds", "context": "timeouts"})
    assert rec(m, old).status == "superseded" and rec(m, old).invalid_at
    assert rec(m, new).status == "active"
    assert m.graph.has_edge("SUPERSEDES", new.ref, old.ref)


def test_retract_and_feedback():
    m = AgentMemory("t")
    r = m.remember("quirk", {"description": "pipeline drops commands"})
    m.feedback(r.id, True)
    m.feedback(r.id, False)
    m.feedback(r.id, False)
    assert (rec(m, r).helpful, rec(m, r).harmful) == (1, 2)
    m.retract(r.id, "could not reproduce")
    assert rec(m, r).status == "retracted" and "could not reproduce" in rec(m, r).evidence


def test_unknown_id_raises_key_error():
    m = AgentMemory("t")
    with pytest.raises(KeyError):
        m.retract("nope")


def test_anchor_and_verify(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    r = m.remember("bug_pattern", {"name": "p", "signature": "s"}, anchors=["a.py"])
    assert m.graph.has_edge("ANCHORED_TO", r.ref, ("file", "a.py"))
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert m.verify(r.id) == 1


def test_location_field_becomes_an_anchor_when_it_is_a_file(tmp_path):
    (tmp_path / "runner.py").write_text("pass\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    r = m.remember("tech_debt", {"location": "runner.py", "description": "no lock"})
    assert m.graph.has_edge("ANCHORED_TO", r.ref, ("file", "runner.py"))


def test_reactivating_text_of_a_superseded_record_gets_a_new_id():
    m = AgentMemory("t")
    old = m.remember("decision", {"title": "A", "context": "x"})
    m.supersede(old.id, "decision", {"title": "B", "context": "y"})
    again = m.remember("decision", {"title": "A", "context": "x"})
    assert again.created and again.ref != old.ref


def test_save_and_load_keeps_actor_settings_and_reports_warnings(tmp_path):
    m = AgentMemory("t")
    m.remember("quirk", {"description": "pipeline drops commands"})
    m.graph.add_node("decision", "bad", title="T", confidence="high")
    path = str(tmp_path / "m.memory")
    m.save(path)
    loaded = AgentMemory.load(path, repo_root=str(tmp_path), agent="cursor")
    assert loaded.writer.agent == "cursor" and loaded.repo_root == str(tmp_path)
    assert loaded.conventions.writer is loaded.writer
    assert any("bad" in w for w in loaded.load_warnings)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_writer.py -q -p no:cacheprovider`
Expected: FAIL (`TypeError: AgentMemory.__init__() got an unexpected keyword argument 'agent'`)

- [ ] **Step 3: Implement `src/agent_memory/writer.py`**

```python
"""The single write path for memory records."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import anchors as anchors_mod
from .record import (
    ENVELOPE_FIELDS, KINDS, PRIORITIES, MemoryRecord, NodeRef, build_text,
    envelope_props, make_id, normalize_text, utc_now,
)
from .retrieval.tokenize import tokens_for

SIMILAR_JACCARD = 0.6
REINFORCE_STEP = 0.1
CONFIDENCE_CAP = 0.95


@dataclass
class WriteResult:
    ref: NodeRef
    created: bool
    merged: bool = False
    similar: List[NodeRef] = field(default_factory=list)

    @property
    def id(self):
        return self.ref[1]


class MemoryWriter:
    def __init__(self, graph, repo_root: Optional[str] = None, agent: str = "unknown"):
        self.graph = graph
        self.repo_root = repo_root
        self.agent = agent
        self.session_id = ""
        self._by_text: Dict[Tuple[str, str], NodeRef] = {}
        self._indexed: set = set()

    # -- lookup ------------------------------------------------------------

    def find(self, memory_id) -> NodeRef:
        for kind in KINDS:
            if self.graph.has_node(kind, memory_id):
                return (kind, memory_id)
        raise KeyError(f"No memory with id {memory_id!r}")

    def _active_with_text(self, kind: str, norm: str) -> Optional[NodeRef]:
        if kind not in self._indexed:
            for node in self.graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status == "active":
                    self._by_text.setdefault((kind, normalize_text(rec.text)), rec.ref)
            self._indexed.add(kind)
        ref = self._by_text.get((kind, norm))
        if ref is None or not self.graph.has_node(*ref):
            return None
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if rec.status != "active" or normalize_text(rec.text) != norm:
            self._by_text.pop((kind, norm), None)
            return None
        return ref

    # -- writes ------------------------------------------------------------

    def add(self, kind: str, fields: Dict[str, Any], *, node_id=None,
            anchors: Iterable = (), scope: Optional[List[str]] = None, evidence: str = "",
            priority: Optional[str] = None, confidence: Optional[float] = None,
            source: str = "explicit") -> WriteResult:
        if kind not in KINDS:
            raise ValueError(f"Unknown memory kind {kind!r}. Valid kinds: {', '.join(sorted(KINDS))}")
        if priority is not None and priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}, got {priority!r}")
        props = {k: ("" if v is None else v) for k, v in fields.items()}
        text = build_text(kind, props)
        norm = normalize_text(text)
        if not norm and node_id is None:
            raise ValueError(f"A {kind} memory has no text; check the field names for this kind")

        existing = self._active_with_text(kind, norm) if norm else None
        if existing is not None:
            self._reinforce(existing, evidence)
            self._anchor_all(existing, kind, props, anchors)
            return WriteResult(existing, created=False, merged=True)

        similar = self._similar(kind, text) if node_id is None else []

        new_id = node_id if node_id is not None else make_id(kind, text)
        if self.graph.has_node(kind, new_id):
            base = f"{new_id}_{make_id(kind, text).rsplit('_', 1)[1]}" if node_id is not None else new_id
            new_id, n = base, 2
            while self.graph.has_node(kind, new_id):
                new_id, n = f"{base}_{n}", n + 1

        envelope = envelope_props(agent=self.agent, session_id=self.session_id, source=source,
                                  evidence=evidence, commit=anchors_mod.head_commit(self.repo_root),
                                  scope=scope, priority=priority, confidence=confidence)
        for key, value in envelope.items():
            props.setdefault(key, value)   # kind fields win, e.g. a decision's own status
        self.graph.add_node(kind, new_id, **props)
        ref = (kind, new_id)
        if norm:
            self._by_text[(kind, norm)] = ref
        self._anchor_all(ref, kind, props, anchors)
        return WriteResult(ref, created=True, similar=similar)

    def supersede(self, old_ref: NodeRef, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        self.graph.get_node(*old_ref)
        result = self.add(kind, fields, **kw)
        if result.ref == old_ref:
            return result
        self._set_status(old_ref, "superseded")
        if not self.graph.has_edge("SUPERSEDES", result.ref, old_ref):
            self.graph.add_edge("SUPERSEDES", result.ref, old_ref,
                                reason=kw.get("evidence", ""), date=utc_now())
        return result

    def retract(self, ref: NodeRef, reason: str = "") -> None:
        self._set_status(ref, "retracted")
        if reason:
            self._append_evidence(ref, f"retracted: {reason}")

    def feedback(self, ref: NodeRef, helpful: bool) -> None:
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if helpful:
            self.graph.update_node(ref[0], ref[1], helpful=rec.helpful + 1)
        else:
            self.graph.update_node(ref[0], ref[1], harmful=rec.harmful + 1)

    # -- helpers -----------------------------------------------------------

    def _set_status(self, ref: NodeRef, status: str) -> None:
        now = utc_now()
        self.graph.update_node(ref[0], ref[1], status=status, invalid_at=now, updated_at=now)

    def _append_evidence(self, ref: NodeRef, extra: str) -> None:
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        if extra and extra not in rec.evidence:
            merged = f"{rec.evidence}; {extra}" if rec.evidence else extra
            self.graph.update_node(ref[0], ref[1], evidence=merged)

    def _reinforce(self, ref: NodeRef, evidence: str) -> None:
        rec = MemoryRecord.from_node(self.graph.get_node(*ref))
        self.graph.update_node(ref[0], ref[1],
                               confidence=min(CONFIDENCE_CAP, rec.confidence + REINFORCE_STEP),
                               updated_at=utc_now())
        self._append_evidence(ref, evidence)

    def _similar(self, kind: str, text: str) -> List[NodeRef]:
        mine = set(tokens_for(text))
        if not mine:
            return []
        out = []
        for node in self.graph.nodes(kind):
            rec = MemoryRecord.from_node(node)
            if rec.status != "active":
                continue
            theirs = set(tokens_for(rec.text))
            union = mine | theirs
            if union and len(mine & theirs) / len(union) >= SIMILAR_JACCARD:
                out.append(rec.ref)
        return out

    def _anchor_all(self, ref: NodeRef, kind: str, props: Dict[str, Any], anchors: Iterable) -> None:
        commit = anchors_mod.head_commit(self.repo_root)
        for item in anchors:
            path, symbol = (item, "") if isinstance(item, str) else (item[0], item[1])
            anchors_mod.add_anchor(self.graph, ref, path, symbol, self.repo_root, commit)
        field_name = KINDS[kind].scope_field
        value = props.get(field_name) if field_name else None
        if self.repo_root and value and "," not in str(value):
            rel = anchors_mod.norm_path(str(value), self.repo_root)
            try:
                is_file = (Path(self.repo_root) / rel).is_file()
            except (OSError, ValueError):
                is_file = False
            if is_file:
                anchors_mod.add_anchor(self.graph, ref, rel, "", self.repo_root, commit)
```

- [ ] **Step 4: Give stores a writer**

In `src/agent_memory/stores/base.py`, replace the `__init__` of `BaseStore` with:

```python
    def __init__(self, graph: ISONGraph):
        from ..writer import MemoryWriter
        self.graph = graph
        self._cache: Dict[str, Any] = {}
        # AgentMemory replaces this with one writer shared by all stores.
        self.writer = MemoryWriter(graph)

    def _add_memory(self, kind: str, node_id: str, **fields) -> str:
        """Create a memory node through the shared write path.

        Returns the id actually used: the existing record's id when the same
        text is already stored, or a suffixed id when `node_id` is taken by
        a different text.
        """
        return self.writer.add(kind, fields, node_id=node_id).id
```

- [ ] **Step 5: Rewire `AgentMemory`**

In `src/agent_memory/memory.py`:

Add imports below the existing ones:

```python
from . import anchors as anchors_mod
from .record import KINDS, MemoryRecord
from .writer import MemoryWriter, WriteResult
```

Replace `__init__` (everything from `def __init__` up to the `# Session Management` banner) with:

```python
    _STORES = (
        ("codebase", CodebaseStore), ("decisions", DecisionStore),
        ("conventions", ConventionStore), ("bugs", BugPatternStore),
        ("dependencies", DependencyStore), ("tests", TestPatternStore),
        ("performance", PerformanceStore), ("techdebt", TechDebtStore),
        ("team", TeamStore), ("sessions", SessionStore),
        ("templates", TemplateStore), ("skills", SkillStore),
        ("todos", TodoStore), ("goals", GoalStore), ("issues", IssueStore),
    )

    def __init__(self, name: str = "project", repo_root: Optional[str] = None,
                 agent: str = "unknown"):
        self.name = name
        self.repo_root = str(repo_root) if repo_root else None
        self.load_warnings: List[str] = []
        self._bind(ISONGraph(name=name), agent)
        self.graph.add_node("agent", "self", name="agent")

    def _bind(self, graph: ISONGraph, agent: str) -> None:
        """Attach every store, the writer, the context builder and the consolidator to `graph`."""
        self.graph = graph
        self.writer = MemoryWriter(graph, self.repo_root, agent)
        for attr, cls in self._STORES:
            store = cls(graph)
            store.writer = self.writer
            setattr(self, attr, store)
        self._context_builder = ContextBuilder(self)
        self._consolidator = MemoryConsolidator(self)
```

Replace `start_session` with:

```python
    def start_session(self, task: str) -> str:
        """Start a new coding session"""
        session_id = self.sessions.start_session(task)

        # Link to previous session if exists
        last = self.sessions.get_last_session()
        if last and last != session_id:
            self.sessions.continue_session(session_id, last)

        self.writer.session_id = session_id
        return session_id
```

Replace the `load` classmethod with:

```python
    @classmethod
    def load(cls, path: str, repo_root: Optional[str] = None,
             agent: str = "unknown") -> "AgentMemory":
        """Load memory from file"""
        graph = ISONGraph.load(path)
        memory = cls.__new__(cls)
        memory.name = graph.name
        memory.repo_root = str(repo_root) if repo_root else None
        memory.load_warnings = []
        memory._bind(graph, agent)
        for kind in KINDS:
            for node in graph.nodes(kind):
                MemoryRecord.from_node(node, memory.load_warnings)
        return memory
```

In `export_json`, change `open(path, "w")` to `open(path, "w", encoding="utf-8")`.

Add a new section before the `# Persistence` banner:

```python
    # =========================================================================
    # Record operations
    # =========================================================================

    def set_actor(self, agent: str, session_id: str = "") -> None:
        """Set who is stamped as the author of subsequent writes."""
        self.writer.agent = agent
        self.writer.session_id = session_id

    def remember(self, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        """Record a memory. See MemoryWriter.add for keyword arguments."""
        return self.writer.add(kind, fields, **kw)

    def supersede(self, old_id, kind: str, fields: Dict[str, Any], **kw) -> WriteResult:
        """Replace `old_id` with a new record; the old one stops being returned."""
        return self.writer.supersede(self.writer.find(old_id), kind, fields, **kw)

    def retract(self, memory_id, reason: str = "") -> None:
        """Mark a memory as wrong. It is kept, and no longer returned."""
        self.writer.retract(self.writer.find(memory_id), reason)

    def feedback(self, memory_id, helpful: bool) -> None:
        """Tell the memory whether a recalled record helped."""
        self.writer.feedback(self.writer.find(memory_id), helpful)

    def anchor(self, memory_id, path: str, symbol: str = "") -> None:
        """Tie a memory to a file, recording the file's current content hash."""
        anchors_mod.add_anchor(self.graph, self.writer.find(memory_id), path, symbol,
                               self.repo_root, anchors_mod.head_commit(self.repo_root))

    def verify(self, memory_id) -> int:
        """Confirm a memory still holds: re-stamp its anchors at the current file hashes."""
        return anchors_mod.restamp(self.graph, self.writer.find(memory_id), self.repo_root)
```

- [ ] **Step 6: Run the new and the existing tests**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider --ignore=tests/eval`
Expected: 79 existing + 8 + 7 + 10 + 11 new, all passed, 1 skipped

- [ ] **Step 7: Commit**

```bash
git add src/agent_memory/writer.py src/agent_memory/stores/base.py src/agent_memory/memory.py tests/test_writer.py
git commit -m "feat: single write path with merge, supersede, retract and anchors"
```

---

### Task 6: Route store writes through the writer

**Files:**
- Modify: the 26 call sites listed below
- Test: `tests/test_store_migration.py`

**Interfaces:**
- Consumes: `BaseStore._add_memory(kind, node_id, **fields) -> str`
- Produces: every registry-kind node created by a store method carries the envelope. Store method signatures and return types are unchanged.

- [ ] **Step 1: Write the failing tests**

`tests/test_store_migration.py`:

```python
from agent_memory import AgentMemory
from agent_memory.record import KINDS, MemoryRecord


def populate(m):
    m.codebase.add_module("src/cache", "LRU caching layer")
    m.codebase.add_pattern("singleton", ["src/cache"], why_chosen="one instance")
    m.conventions.add_convention("naming", "snake_case", "ruff")
    m.conventions.add_idiom("null_check", "if x is None", "if not x")
    m.conventions.add_project_pattern("error_handling", "raise CustomError(code=)", "always")
    m.decisions.add_decision("Use async", "High concurrency expected")
    m.decisions.add_alternative("Use async", "threading", "GIL limitations")
    m.bugs.add_bug("Race condition in cache", severity="critical")
    m.bugs.add_pattern("race_condition", "shared_state_no_lock")
    m.bugs.add_fix_pattern("race_condition", "use asyncio.Lock", "check reentrant need")
    m.dependencies.add_dependency("rocksdb", "0.22.0", "storage")
    m.dependencies.add_quirk("rocksdb", "batch > 4MB causes stall", severity="high")
    m.dependencies.add_workaround("batch > 4MB causes stall", "chunk into 2MB batches")
    m.tests.add_convention("unit", "test_{module}_{function}_{case}")
    m.tests.add_required_cases("graph_operation", ["empty", "cyclic"])
    m.performance.add_hot_path("node_lookup", "very_high", untouchable=True)
    m.performance.add_constraint("node_lookup", "<5ms p99", "user experience")
    m.techdebt.add_debt("src/traverse.py", "god function", severity="high")
    m.techdebt.add_refactoring_attempt("src/traverse.py", "split by type", "failed", "circular imports")
    m.team.add_expertise("graph_algorithms", "deep", ["src/traverse"])
    m.templates.add_template("api_endpoint", "handler")
    m.templates.add_snippet("async_lock", "async with lock:", "shared state access")
    sid = m.start_session("Add batch API")
    m.sessions.add_decision(sid, "use streaming", "memory efficiency", sticky=True)
    m.sessions.add_open_question(sid, "max batch size?")
    m.sessions.add_partial_work(sid, "function", "src/batch.py", "half written")
    m.sessions.add_blocker(sid, "waiting on schema review")


def test_every_registry_node_written_by_a_store_has_an_envelope():
    m = AgentMemory("t", agent="claude-code")
    populate(m)
    seen = set()
    for kind in KINDS:
        for node in m.graph.nodes(kind):
            rec = MemoryRecord.from_node(node)
            seen.add(kind)
            assert rec.source == "explicit", f"{kind}:{node.id} was not written through the writer"
            assert rec.agent == "claude-code" and rec.created_at
            assert rec.text, f"{kind}:{node.id} renders to empty text"
    assert seen == set(KINDS), f"kinds never created: {set(KINDS) - seen}"


def test_adding_the_same_convention_twice_no_longer_raises():
    m = AgentMemory("t")
    first = m.conventions.add_convention("naming", "snake_case", "ruff")
    second = m.conventions.add_convention("naming", "snake_case", "ruff")
    assert first == second
    assert len(list(m.graph.nodes("convention"))) == 1


def test_rules_sharing_a_twenty_character_prefix_both_survive():
    m = AgentMemory("t")
    a = m.conventions.add_convention("naming", "functions use snake_case names always", "ruff")
    b = m.conventions.add_convention("naming", "functions use snake_case names in tests too", "ruff")
    assert a != b
    assert len(list(m.graph.nodes("convention"))) == 2


def test_fix_pattern_for_an_existing_fix_text_does_not_raise():
    m = AgentMemory("t")
    m.bugs.add_pattern("race_a", "shared state without a lock")
    m.bugs.add_pattern("race_b", "shared counter without a lock")
    m.bugs.add_fix_pattern("race_a", "use asyncio.Lock", "")
    m.bugs.add_fix_pattern("race_b", "use asyncio.Lock", "")
    m.bugs.add_fix_pattern("race_a", "use asyncio.Lock", "")
```

Before running, check each `populate` call against the store's real signature (`grep -n "    def " src/agent_memory/stores/<file>.py`) and adjust argument order or names if one differs. The README examples were used as the source.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_store_migration.py -q -p no:cacheprovider`
Expected: FAIL. The first test fails with `was not written through the writer`; the second with `DuplicateNodeError`.

- [ ] **Step 3: Apply the transformation to each call site**

The rule, applied to each row of the table:

1. Replace `self.graph.add_node("<kind>", <id_var>, <kwargs...>)` with `<id_var> = self._add_memory("<kind>", <id_var>, <kwargs...>)`. Keep the keyword arguments exactly as they are.
2. Leave any surrounding `if self.graph.has_node(...)` guard as it is.
3. After a migrated call, any `self.graph.add_edge(REL, a, b, ...)` later in the same method that uses `<id_var>` gets wrapped in `if not self.graph.has_edge(REL, a, b):`. A merged record already has its edges, and a duplicate edge raises `DuplicateEdgeError`.
4. If the method returns `<id_var>`, it now returns the id actually used. No other change.

Worked example, `add_fix_pattern` in `stores/bugs.py`. Before:

```python
        self.graph.add_node(
            "fix_pattern",
            fix_id,
            bug_pattern_id=pattern_id,
            fix_template=self._sanitize(fix_template),
            caveats=self._sanitize(caveats or ""),
        )

        if self.graph.has_node("bug_pattern", pattern_id):
            self.graph.add_edge(
                "FIX_FOR",
                ("fix_pattern", fix_id),
                ("bug_pattern", pattern_id)
            )
```

After:

```python
        fix_id = self._add_memory(
            "fix_pattern",
            fix_id,
            bug_pattern_id=pattern_id,
            fix_template=self._sanitize(fix_template),
            caveats=self._sanitize(caveats or ""),
        )

        if self.graph.has_node("bug_pattern", pattern_id) and not self.graph.has_edge(
            "FIX_FOR", ("fix_pattern", fix_id), ("bug_pattern", pattern_id)
        ):
            self.graph.add_edge(
                "FIX_FOR",
                ("fix_pattern", fix_id),
                ("bug_pattern", pattern_id)
            )
```

Call sites (line numbers are from before any edit in this task; work bottom-up within a file so they stay valid):

| File | Method | Line | Kind | Edges to guard afterwards |
|---|---|---|---|---|
| `stores/bugs.py` | `add_bug` | 30 | `bug` | none |
| `stores/bugs.py` | `add_pattern` | 53 | `bug_pattern` | none |
| `stores/bugs.py` | `add_fix_pattern` | 73 | `fix_pattern` | `FIX_FOR` |
| `stores/codebase.py` | `add_module` | 32 | `module` | none |
| `stores/codebase.py` | `add_pattern` | 106 | `design_pattern` | `IMPLEMENTS` |
| `stores/conventions.py` | `add_convention` | 31 | `convention` | none |
| `stores/conventions.py` | `add_idiom` | 52 | `idiom` | none |
| `stores/conventions.py` | `add_project_pattern` | 71 | `project_pattern` | none |
| `stores/decisions.py` | `add_decision` | 33 | `decision` | none |
| `stores/decisions.py` | `add_alternative` | 57 | `alternative` | `CONSIDERED_FOR` |
| `stores/dependencies.py` | `add_quirk` | 52 | `quirk` | `HAS_QUIRK` |
| `stores/dependencies.py` | `add_workaround` | 82 | `workaround` | `WORKAROUND_FOR` |
| `stores/issues.py` | `add_workaround` | 379 | `workaround` | `HAS_WORKAROUND` |
| `stores/performance.py` | `add_hot_path` | 52 | `hot_path` | none |
| `stores/performance.py` | `add_constraint` | 72 | `performance_constraint` | none |
| `stores/sessions.py` | `add_partial_work` | 75 | `partial_work` | `HAS_PARTIAL_WORK` |
| `stores/sessions.py` | `add_decision` | 103 | `session_decision` | `DECIDED_IN` |
| `stores/sessions.py` | `add_open_question` | 129 | `open_question` | `QUESTION_FROM` |
| `stores/sessions.py` | `add_blocker` | 172 | `blocker` | none |
| `stores/team.py` | `add_expertise` | 50 | `expertise_area` | none |
| `stores/techdebt.py` | `add_debt` | 30 | `tech_debt` | none |
| `stores/techdebt.py` | `add_refactoring_attempt` | 54 | `refactoring_attempt` | `ATTEMPTED_ON` |
| `stores/templates.py` | `add_template` | 28 | `template` | none |
| `stores/templates.py` | `add_snippet` | 78 | `snippet` | none |
| `stores/tests.py` | `add_convention` | 29 | `test_convention` | none |
| `stores/tests.py` | `add_required_cases` | 48 | `required_test_case` | none |

Two sites need extra care:

- `stores/codebase.py` `add_module` passes `**{k: str(v) for k, v in properties.items()}`. Keep that expansion in the `_add_memory` call.
- `stores/decisions.py` `add_decision` passes `status=...`. Keep it. The writer lets kind fields win over the envelope, and `record.py` maps `accepted` to `active`.

If a migrated method's id variable is used by a *lookup elsewhere* that recomputes the id from a name (for example `add_fix_pattern` recomputes `pattern_id` from the pattern name), nothing changes: a first write keeps the legacy id.

- [ ] **Step 4: Run the migration tests**

Run: `PYTHONPATH=src python -m pytest tests/test_store_migration.py -q -p no:cacheprovider`
Expected: 4 passed. If the first test reports `kinds never created`, a `populate` line did not create its node: fix the call, not the assertion.

- [ ] **Step 5: Run the whole suite**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider --ignore=tests/eval`
Expected: all passed, 1 skipped. If an existing test fails because it relied on `DuplicateNodeError` or on a deleted duplicate, stop and report it; do not edit the existing test without the user's say.

- [ ] **Step 6: Commit**

```bash
git add src/agent_memory/stores tests/test_store_migration.py
git commit -m "refactor: stores write memories through the shared writer"
```

---

### Task 7: Scope and graph channels, budget, rendering

**Files:**
- Create: `src/agent_memory/retrieval/channels.py`
- Create: `src/agent_memory/retrieval/budget.py`
- Create: `src/agent_memory/retrieval/render.py`
- Test: `tests/test_channels_render.py`

**Interfaces:**
- Consumes: `MemoryRecord`, `KINDS`, tiers from `record.py`
- Produces:
  - `proximity(query_file: str, anchored_file: str) -> float`
  - `scope_scores(graph, records: Dict[NodeRef, MemoryRecord], files: List[str]) -> Tuple[Dict[NodeRef, float], Dict[NodeRef, str]]` (scores, promoted decisions with their `how`)
  - `graph_neighbours(graph, seeds: Dict[NodeRef, float], candidates) -> Dict[NodeRef, Tuple[float, NodeRef]]` (inherited score, seed)
  - `GRAPH_EDGES`, `GRAPH_INHERIT`
  - `TokenCounter` with `.count(text) -> int`, `TokenCounter.heuristic()`, `TokenCounter.tiktoken(encoding="cl100k_base")`
  - `render_item(item, compact=False) -> str`, `render(items, compact=False) -> str`, `HEADERS`, `STALE_FLAGS`
  - Rendering reads these attributes from an item: `.record.id`, `.text`, `.tier`, `.staleness`, `.why`

- [ ] **Step 1: Write the failing tests**

`tests/test_channels_render.py`:

```python
from types import SimpleNamespace

from agent_memory import AgentMemory
from agent_memory.record import GUARDRAIL, GUIDANCE, KINDS, MemoryRecord
from agent_memory.retrieval.budget import TokenCounter
from agent_memory.retrieval.channels import graph_neighbours, proximity, scope_scores
from agent_memory.retrieval.render import render, render_item


def records(m):
    return {n.ref: MemoryRecord.from_node(n) for k in KINDS for n in m.graph.nodes(k)}


def test_proximity_tiers():
    assert proximity("a/b/c.py", "a/b/c.py") == 1.0
    assert proximity("a/b/c.py", "a/b/d.py") == 0.6
    assert proximity("a/b/x/c.py", "a/b/y/d.py") == 0.3
    assert proximity("a/x/c.py", "a/y/d.py") == 0.0
    assert proximity("c.py", "d.py") == 0.6


def test_scope_scores_from_globs_and_anchors(tmp_path):
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "a.py").write_text("x\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    glob = m.remember("convention", {"category": "c", "rule": "scoped rule"}, scope=["svc/**"])
    anchored = m.remember("bug_pattern", {"name": "p", "signature": "s"}, anchors=["svc/a.py"])
    everywhere = m.remember("convention", {"category": "c", "rule": "global rule"})
    scores, promoted = scope_scores(m.graph, records(m), ["svc/b.py"])
    assert scores[glob.ref] == 1.0
    assert scores[anchored.ref] == 0.6
    assert everywhere.ref not in scores and promoted == {}


def test_decision_constraining_the_module_is_promoted():
    m = AgentMemory("t")
    m.codebase.add_module("src/api", "HTTP layer")
    m.decisions.add_decision("Use async", "concurrency")
    m.decisions.add_constraint("Use async", "src/api", "all handlers must be async")
    scores, promoted = scope_scores(m.graph, records(m), ["src/api/orders.py"])
    ref = next(r for r in promoted)
    assert ref[0] == "decision" and scores[ref] == 1.0
    assert promoted[ref] == "all handlers must be async"


def test_graph_neighbours_follow_whitelisted_edges_both_ways():
    m = AgentMemory("t")
    m.bugs.add_pattern("race", "shared state without a lock")
    m.bugs.add_fix_pattern("race", "use asyncio.Lock", "")
    recs = records(m)
    pattern = next(r for r in recs if r[0] == "bug_pattern")
    fix = next(r for r in recs if r[0] == "fix_pattern")
    out = graph_neighbours(m.graph, {pattern: 0.02}, set(recs))
    assert out[fix] == (0.01, pattern)
    assert graph_neighbours(m.graph, {fix: 0.02}, set(recs))[pattern][0] == 0.01


def test_heuristic_counter_is_ceil_of_chars_over_four():
    c = TokenCounter.heuristic()
    assert (c.count(""), c.count("abcd"), c.count("abcde")) == (0, 1, 2)


def item(id_, text, tier, staleness="fresh", why=""):
    return SimpleNamespace(record=SimpleNamespace(id=id_), text=text, tier=tier,
                           staleness=staleness, why=why)


def test_render_groups_by_tier_and_flags_stale_items():
    out = render([
        item("c1", "[naming] snake_case", GUIDANCE, why="matched: naming"),
        item("b1", "storm: unbounded retry", GUARDRAIL, staleness="drifted"),
    ])
    lines = out.splitlines()
    assert lines[0] == "## Guardrails"
    assert lines[1] == "- [b1] storm: unbounded retry"
    assert lines[2].startswith("  ⚠")
    assert "## Guidance" in lines and "  why: matched: naming" in lines
    assert "## Background" not in out


def test_render_compact_and_empty():
    assert render([]) == ""
    assert "why:" not in render_item(item("c1", "x", GUIDANCE, why="matched: x"), compact=True)
```

Check `add_constraint`'s parameter order in `stores/decisions.py` before running; the README shows `add_constraint("Use async", "src/api", "all handlers must be async")`.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_channels_render.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `retrieval/channels.py`**

```python
"""Non-lexical retrieval channels: file scope and one-hop graph expansion."""
from __future__ import annotations

import fnmatch
from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

from ..record import MemoryRecord, NodeRef

GRAPH_EDGES = ("FIX_FOR", "WORKAROUND_FOR", "HAS_QUIRK", "CONSIDERED_FOR",
               "ATTEMPTED_ON", "INSTANCE_OF", "TEST_FOR_BUG")
GRAPH_INHERIT = 0.5


def proximity(query_file: str, anchored_file: str) -> float:
    """1.0 same file, 0.6 same directory, 0.3 shared ancestor two or more levels deep."""
    if query_file == anchored_file:
        return 1.0
    q, a = query_file.split("/")[:-1], anchored_file.split("/")[:-1]
    if q == a:
        return 0.6
    shared = 0
    for x, y in zip(q, a):
        if x != y:
            break
        shared += 1
    return 0.3 if shared >= 2 else 0.0


def scope_scores(graph, records: Dict[NodeRef, MemoryRecord],
                 files: List[str]) -> Tuple[Dict[NodeRef, float], Dict[NodeRef, str]]:
    scores: Dict[NodeRef, float] = {}
    promoted: Dict[NodeRef, str] = {}
    if not files:
        return scores, promoted

    anchored = defaultdict(list)
    for edge in graph.edges("ANCHORED_TO"):
        if edge.source in records and graph.has_node(*edge.target):
            anchored[edge.source].append(str(graph.get_node(*edge.target).properties.get("path") or ""))

    for ref, rec in records.items():
        best = 0.0
        for f in files:
            for pattern in rec.scope:
                if pattern != "*" and fnmatch.fnmatchcase(f, pattern):
                    best = 1.0
            for path in anchored.get(ref, ()):
                best = max(best, proximity(f, path))
        if best:
            scores[ref] = best

    for edge in graph.edges("CONSTRAINS"):
        if edge.source not in records or not graph.has_node(*edge.target):
            continue
        module_path = str(graph.get_node(*edge.target).properties.get("path") or "")
        module_path = module_path.replace("\\", "/").strip("/")
        if module_path and any(f.startswith(module_path) for f in files):
            scores[edge.source] = 1.0
            promoted[edge.source] = str(edge.properties.get("how") or "")
    return scores, promoted


def graph_neighbours(graph, seeds: Dict[NodeRef, float],
                     candidates: Iterable[NodeRef]) -> Dict[NodeRef, Tuple[float, NodeRef]]:
    """Candidates one whitelisted edge away from a seed, with the score they inherit and from whom."""
    allowed = set(candidates)
    out: Dict[NodeRef, Tuple[float, NodeRef]] = {}
    for rel in GRAPH_EDGES:
        for edge in graph.edges(rel):
            for seed, other in ((edge.source, edge.target), (edge.target, edge.source)):
                if seed in seeds and other in allowed and other not in seeds:
                    inherited = seeds[seed] * GRAPH_INHERIT
                    if other not in out or inherited > out[other][0]:
                        out[other] = (inherited, seed)
    return out
```

- [ ] **Step 4: Implement `retrieval/budget.py`**

```python
"""Token counting for budget packing."""
import math
from typing import Callable


class TokenCounter:
    def __init__(self, fn: Callable[[str], int], name: str):
        self._fn = fn
        self.name = name

    def count(self, text: str) -> int:
        return self._fn(text) if text else 0

    @classmethod
    def heuristic(cls) -> "TokenCounter":
        """ceil(chars / 4). The default: no dependency and no network."""
        return cls(lambda text: math.ceil(len(text) / 4), "chars/4")

    @classmethod
    def tiktoken(cls, encoding: str = "cl100k_base") -> "TokenCounter":
        """Exact counts. Opt-in, because loading an encoding may download it."""
        import tiktoken
        enc = tiktoken.get_encoding(encoding)
        return cls(lambda text: len(enc.encode(text)), f"tiktoken:{encoding}")
```

- [ ] **Step 5: Implement `retrieval/render.py`**

```python
"""Markdown rendering. One generic path driven by each item's text, so no kind can be left out."""
from typing import Iterable, List

from ..record import BACKGROUND, GUARDRAIL, GUIDANCE, TIER_ORDER

HEADERS = {GUARDRAIL: "## Guardrails", GUIDANCE: "## Guidance", BACKGROUND: "## Background"}
STALE_FLAGS = {
    "drifted": "⚠ anchored file changed since this was recorded",
    "broken": "⚠ anchored symbol no longer found in its file",
    "orphaned": "⚠ anchored file no longer exists",
}


def render_item(item, compact: bool = False) -> str:
    lines = [f"- [{item.record.id}] {item.text}"]
    flag = STALE_FLAGS.get(item.staleness)
    if flag:
        lines.append(f"  {flag}")
    if not compact and item.why:
        lines.append(f"  why: {item.why}")
    return "\n".join(lines)


def render(items: Iterable, compact: bool = False) -> str:
    items = list(items)
    out: List[str] = []
    for tier in TIER_ORDER:
        group = [i for i in items if i.tier == tier]
        if group:
            out.append(HEADERS[tier])
            out.extend(render_item(i, compact) for i in group)
    return "\n".join(out)
```

- [ ] **Step 6: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_channels_render.py -q -p no:cacheprovider`
Expected: 7 passed

- [ ] **Step 7: Commit**

```bash
git add src/agent_memory/retrieval tests/test_channels_render.py
git commit -m "feat: scope and graph channels, token counter, renderer"
```

---

### Task 8: Retriever and `recall()`

**Files:**
- Create: `src/agent_memory/retrieval/retriever.py`
- Modify: `src/agent_memory/retrieval/__init__.py`
- Modify: `src/agent_memory/memory.py` (add `recall`, `attach_semantic`, `_retriever`)
- Test: `tests/test_retriever.py`

**Interfaces:**
- Consumes: everything from Tasks 2–7
- Produces:
  - `ScoredMemory(record, score, channels: Dict[str, float], staleness, tier, text, why, matched: frozenset, pinned: bool)`
  - `RecallResult(task, files, items: List[ScoredMemory], tokens: int, compact: bool)` with `.to_prompt() -> str`, `.to_dict() -> dict`
  - `RecallEvent(task, files, ids)`
  - `Retriever(graph, repo_root=None, counter=None, semantic=None, sink=None)` with `.recall(task="", files=None, symbols=None, kinds=None, max_tokens=2000, compact=False) -> RecallResult` and `.rank(query, files, kinds=None, strict=False) -> List[ScoredMemory]`
  - `AgentMemory.recall(...)` with the same parameters; `AgentMemory.attach_semantic(searcher)` where `searcher.search(query, k=...)` returns objects with `.node_ref` and `.score`

- [ ] **Step 1: Write the failing tests**

`tests/test_retriever.py`:

```python
from types import SimpleNamespace

import pytest

from agent_memory import AgentMemory
from agent_memory.retrieval.budget import TokenCounter


@pytest.fixture
def repo(tmp_path):
    for rel in ("services/payments/webhook.py", "services/notifications/email.py"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("def run():\n    pass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def memory(repo):
    m = AgentMemory("t", repo_root=str(repo))
    storm_id = m.bugs.add_pattern("webhook_storm", "unbounded retry on external webhook")
    m.anchor(storm_id, "services/payments/webhook.py")
    m.storm = SimpleNamespace(id=storm_id, ref=("bug_pattern", storm_id))
    m.bugs.add_fix_pattern("webhook_storm", "cap retries at five attempts", "")
    m.email = m.remember("bug_pattern", {"name": "email_dup",
                                         "signature": "retry of email send delivers duplicates"},
                         anchors=["services/notifications/email.py"])
    m.naming = m.remember("convention", {"category": "naming", "rule": "cache keys use colon namespaces"})
    return m


def ids(result):
    return [i.record.id for i in result.items]


def test_task_text_alone_finds_the_relevant_record(memory):
    result = memory.recall(task="add retry logic to the payment webhook")
    assert ids(result)[0] == memory.storm.id
    assert memory.naming.id not in ids(result)


def test_files_alone_find_anchored_records(memory):
    result = memory.recall(files=["services/payments/webhook.py"])
    assert memory.storm.id in ids(result)
    assert memory.email.id not in ids(result)


def test_windows_and_absolute_paths_match(memory, repo):
    assert memory.storm.id in ids(memory.recall(files=["services\\payments\\webhook.py"]))
    absolute = str(repo / "services" / "payments" / "webhook.py")
    assert memory.storm.id in ids(memory.recall(files=[absolute]))


def test_graph_hop_brings_the_fix_along(memory):
    result = memory.recall(task="payment webhook", files=["services/payments/webhook.py"])
    texts = [i.text for i in result.items]
    assert any("cap retries at five attempts" in t for t in texts)
    fix = next(i for i in result.items if i.record.kind == "fix_pattern")
    assert "linked to" in fix.why


def test_irrelevant_and_empty_queries_return_nothing(memory):
    assert memory.recall(task="add a dark mode toggle").items == []
    assert memory.recall(task="", files=[]).items == []
    assert memory.recall(task="the and of").items == []
    assert memory.recall(task="add a dark mode toggle").to_prompt() == ""


def test_file_match_outranks_a_same_vocabulary_record_elsewhere(memory):
    result = memory.recall(task="add retry", files=["services/payments/webhook.py"])
    order = ids(result)
    assert order.index(memory.storm.id) < order.index(memory.email.id)


def test_superseded_and_retracted_records_never_appear(memory):
    new = memory.supersede(memory.storm.id, "bug_pattern",
                           {"name": "webhook_storm_v2", "signature": "unbounded retry on external webhook, cap at 5"})
    memory.retract(memory.email.id, "wrong")
    result = memory.recall(task="retry webhook email")
    assert new.id in ids(result)
    assert memory.storm.id not in ids(result) and memory.email.id not in ids(result)


def test_drifted_record_is_flagged_and_ranked_below_a_fresh_twin(memory, repo):
    f = repo / "services" / "search.py"
    f.write_text("a = 1\n", encoding="utf-8")
    stale = memory.remember("bug_pattern", {"name": "idx_a", "signature": "indexer alpha primary bulk request needs backoff"},
                            anchors=["services/search.py"])
    f.write_text("a = 2\n", encoding="utf-8")
    fresh = memory.remember("bug_pattern", {"name": "idx_b", "signature": "indexer beta replica bulk request needs backoff"},
                            anchors=["services/search.py"])
    result = memory.recall(task="indexer bulk request backoff", files=["services/search.py"])
    order = ids(result)
    assert order.index(fresh.id) < order.index(stale.id)
    stale_item = next(i for i in result.items if i.record.id == stale.id)
    assert stale_item.staleness == "drifted"
    assert "⚠" in result.to_prompt()


def test_pinned_records_always_appear_within_their_share(memory):
    pinned = memory.remember("convention", {"category": "security", "rule": "never log secrets"},
                             priority="must")
    result = memory.recall(task="add a dark mode toggle")
    assert ids(result) == [pinned.id] and result.items[0].pinned


def test_near_duplicates_collapse(memory):
    memory.remember("quirk", {"description": "redis pipeline drops commands over the payload limit silently"})
    memory.remember("quirk", {"description": "redis pipeline drops commands over the payload limit"})
    result = memory.recall(task="redis pipeline drops commands")
    assert len([i for i in result.items if i.record.kind == "quirk"]) == 1


@pytest.mark.parametrize("budget", [0, 5, 40, 200, 2000])
def test_rendered_prompt_never_exceeds_the_budget(memory, budget):
    for n in range(30):
        memory.remember("quirk", {"description": f"webhook retry quirk number {n} with a longer explanation"})
    result = memory.recall(task="webhook retry quirk", max_tokens=budget)
    assert TokenCounter.heuristic().count(result.to_prompt()) <= budget
    assert result.tokens <= budget
    if budget < 10:
        assert result.items == []


def test_everything_selected_is_rendered(memory):
    result = memory.recall(task="retry webhook email cache keys",
                           files=["services/payments/webhook.py"])
    prompt = result.to_prompt()
    assert result.items and all(f"[{i.record.id}]" in prompt for i in result.items)


def test_an_item_too_big_for_the_budget_is_skipped_not_fatal(memory):
    memory.remember("snippet", {"name": "retry_helper", "code": "x = 1; " * 400,
                                "usage_context": "webhook retry"})
    result = memory.recall(task="webhook retry", max_tokens=120)
    assert memory.storm.id in ids(result)
    assert all(i.record.kind != "snippet" for i in result.items)


def test_open_items_of_the_unfinished_session_are_carried(memory):
    sid = memory.start_session("refund worker")
    memory.sessions.add_open_question(sid, "max batch size for the refund worker?")
    result = memory.recall(task="add a dark mode toggle")
    assert any("max batch size" in i.text for i in result.items)
    memory.end_session(sid, completion=1.0)
    assert not any("max batch size" in i.text
                   for i in memory.recall(task="add a dark mode toggle").items)


def test_semantic_channel_is_optional_and_failure_tolerant(memory):
    class Hit:
        def __init__(self, ref, score):
            self.node_ref, self.score = ref, score

    class Searcher:
        def search(self, query, k=20):
            return [Hit(memory.naming.ref, 0.9)]

    class Broken:
        def search(self, query, k=20):
            raise RuntimeError("index missing")

    memory.attach_semantic(Searcher())
    assert memory.naming.id in ids(memory.recall(task="how are identifiers organised"))
    memory.attach_semantic(Broken())
    assert memory.recall(task="how are identifiers organised").items == []


def test_to_dict_is_json_serialisable(memory):
    import json
    data = memory.recall(task="payment webhook retry").to_dict()
    json.dumps(data)
    assert data["items"][0]["id"] == memory.storm.id
    assert {"kind", "text", "score", "why", "staleness", "tier", "agent"} <= set(data["items"][0])


def test_recall_events_reach_the_sink(memory):
    seen = []
    memory._retriever.sink = seen.append
    memory.recall(task="payment webhook retry")
    assert seen and memory.storm.id in seen[0].ids
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_retriever.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: 'AgentMemory' object has no attribute 'recall'`

- [ ] **Step 3: Implement `retrieval/retriever.py`**

```python
"""Ranks memories for a task: channels, fusion, modifiers, floor, de-duplication, budget."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional

from ..anchors import STALENESS_FACTOR, HashCache, norm_path, staleness
from ..record import BACKGROUND, GUARDRAIL, GUIDANCE, KINDS, MemoryRecord, NodeRef
from .budget import TokenCounter
from .channels import graph_neighbours, scope_scores
from .lexical import BM25
from .render import HEADERS, render, render_item
from .tokenize import tokenize, tokens_for

log = logging.getLogger(__name__)

# Tuned against tests/eval. Change here only.
RRF_K = 60
WEIGHTS = {"lexical": 1.0, "scope": 1.0, "semantic": 1.0}
TIER_BOOST = {GUARDRAIL: 1.3, GUIDANCE: 1.0, BACKGROUND: 0.8}
RECENCY_FLOOR = 0.3
SCOPE_FLOOR = 0.6
SEMANTIC_FLOOR = 0.35
SEMANTIC_K = 20
DEDUP_JACCARD = 0.8
PINNED_BUDGET_SHARE = 0.15
GRAPH_SEEDS = 10
DISTINCTIVE_DF = 2
SESSION_KINDS = ("open_question", "blocker", "partial_work")
_RESOLVED_FIELD = {"open_question": "answered_in", "blocker": "resolved_in"}


@dataclass
class ScoredMemory:
    record: MemoryRecord
    score: float
    channels: Dict[str, float]
    staleness: str
    tier: str
    text: str
    why: str = ""
    matched: FrozenSet[str] = frozenset()
    pinned: bool = False

    def to_dict(self) -> Dict[str, Any]:
        r = self.record
        return {"id": r.id, "kind": r.kind, "text": self.text, "score": round(self.score, 6),
                "why": self.why, "staleness": self.staleness, "tier": self.tier,
                "agent": r.agent, "created_at": r.created_at, "evidence": r.evidence,
                "confidence": r.confidence, "pinned": self.pinned, "channels": self.channels}


@dataclass
class RecallResult:
    task: str
    files: List[str]
    items: List[ScoredMemory] = field(default_factory=list)
    tokens: int = 0
    compact: bool = False

    def to_prompt(self) -> str:
        return render(self.items, self.compact)

    def to_dict(self) -> Dict[str, Any]:
        return {"task": self.task, "files": self.files, "tokens": self.tokens,
                "items": [i.to_dict() for i in self.items]}


@dataclass
class RecallEvent:
    task: str
    files: List[str]
    ids: List[Any]


def _dense_ranks(scores: Dict[NodeRef, float]) -> Dict[NodeRef, int]:
    ranks, rank, last = {}, 0, None
    for ref, score in sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        if last is None or score < last - 1e-12:
            rank, last = rank + 1, score
        ranks[ref] = rank
    return ranks


def _age_days(timestamp: str) -> Optional[float]:
    if not timestamp:
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400)


def _recency(rec: MemoryRecord) -> float:
    half_life = KINDS[rec.kind].half_life_days
    age = _age_days(rec.updated_at or rec.created_at)
    if half_life is None or age is None:
        return 1.0
    return max(RECENCY_FLOOR, 0.5 ** (age / half_life))


def _feedback(rec: MemoryRecord) -> float:
    return (1 + rec.helpful) / (1 + rec.helpful + 2 * rec.harmful)


class Retriever:
    def __init__(self, graph, repo_root: Optional[str] = None,
                 counter: Optional[TokenCounter] = None, semantic=None,
                 sink: Optional[Callable[[RecallEvent], None]] = None):
        self.graph = graph
        self.repo_root = repo_root
        self.counter = counter or TokenCounter.heuristic()
        self.semantic = semantic
        self.sink = sink
        self._semantic_warned = False

    # -- candidates --------------------------------------------------------

    def _candidates(self, kinds: Optional[Iterable[str]]) -> Dict[NodeRef, MemoryRecord]:
        out: Dict[NodeRef, MemoryRecord] = {}
        for kind in (kinds or KINDS):
            if kind not in KINDS:
                continue
            resolved = _RESOLVED_FIELD.get(kind)
            for node in self.graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status != "active":
                    continue
                if resolved and rec.fields.get(resolved):
                    continue
                out[rec.ref] = rec
        return out

    def _semantic_scores(self, query: str, cands) -> Dict[NodeRef, float]:
        if self.semantic is None or not query.strip():
            return {}
        try:
            hits = self.semantic.search(query, k=SEMANTIC_K)
            return {tuple(h.node_ref): float(h.score) for h in hits
                    if tuple(h.node_ref) in cands and float(h.score) >= SEMANTIC_FLOOR}
        except Exception as exc:  # the channel is optional; never fail a recall on it
            if not self._semantic_warned:
                log.warning("semantic channel failed and was skipped: %s", exc)
                self._semantic_warned = True
            return {}

    # -- ranking -----------------------------------------------------------

    def rank(self, query: str, files: List[str], kinds: Optional[Iterable[str]] = None,
             strict: bool = False, extra_refs: Iterable[NodeRef] = (),
             cands: Optional[Dict[NodeRef, MemoryRecord]] = None) -> List[ScoredMemory]:
        """Ranked survivors of the relevance floor. `strict` applies the guardrail-check floor."""
        cands = dict(cands) if cands is not None else self._candidates(kinds)
        for ref in extra_refs:   # promoted decisions when kinds is restricted
            if ref not in cands and self.graph.has_node(*ref):
                rec = MemoryRecord.from_node(self.graph.get_node(*ref))
                if rec.status == "active":
                    cands[ref] = rec
        if not cands:
            return []

        stems = " ".join(os.path.splitext(os.path.basename(f))[0] for f in files)
        query_tokens = tokenize(f"{query} {stems}")
        bm25 = BM25({ref: tokens_for(rec.text) for ref, rec in cands.items()})
        hits = {h.key: h for h in bm25.search(query_tokens)}
        lexical = {ref: h.score for ref, h in hits.items()}
        scope, promoted = scope_scores(self.graph, cands, files)
        semantic = self._semantic_scores(query, cands)

        fused: Dict[NodeRef, float] = {}
        channels: Dict[NodeRef, Dict[str, float]] = {}
        for name, scores, quality in (("lexical", lexical, None), ("scope", scope, scope),
                                      ("semantic", semantic, None)):
            ranks = _dense_ranks(scores)
            for ref, rank in ranks.items():
                q = quality[ref] if quality is not None else 1.0
                fused[ref] = fused.get(ref, 0.0) + WEIGHTS[name] * q / (RRF_K + rank)
                channels.setdefault(ref, {})[name] = round(scores[ref], 4)

        def passes(ref: NodeRef) -> bool:
            matched = hits[ref].matched if ref in hits else frozenset()
            in_scope = scope.get(ref, 0.0) >= SCOPE_FLOOR
            if strict:
                distinctive = any(bm25.df[t] <= DISTINCTIVE_DF for t in matched)
                return len(matched) >= 2 or (len(matched) == 1 and (in_scope or distinctive))
            return bool(matched) or in_scope or ref in semantic

        survivors = {ref: score for ref, score in fused.items() if passes(ref)}

        linked: Dict[NodeRef, NodeRef] = {}
        if not strict and survivors:
            seeds = dict(sorted(survivors.items(), key=lambda kv: -kv[1])[:GRAPH_SEEDS])
            for ref, (inherited, seed) in graph_neighbours(self.graph, seeds, cands).items():
                if ref not in survivors:
                    survivors[ref] = inherited
                    channels.setdefault(ref, {})["graph"] = round(inherited, 6)
                    linked[ref] = seed

        cache = HashCache(self.repo_root)
        out: List[ScoredMemory] = []
        for ref, base in survivors.items():
            rec = cands[ref]
            state = staleness(self.graph, ref, cache)
            if strict and state == "orphaned":
                continue
            tier = GUARDRAIL if ref in promoted else rec.tier
            score = (base * rec.confidence * STALENESS_FACTOR[state] * _recency(rec)
                     * TIER_BOOST[tier] * _feedback(rec))
            text = rec.text
            if promoted.get(ref):
                text = f"{text} (constraint: {promoted[ref]})"
            matched = hits[ref].matched if ref in hits else frozenset()
            out.append(ScoredMemory(rec, score, channels.get(ref, {}), state, tier, text,
                                    self._why(rec, matched, scope.get(ref), ref in semantic,
                                              linked.get(ref)), matched))
        out.sort(key=lambda s: (-s.score, str(s.record.id)))
        return self._dedup(out)

    @staticmethod
    def _why(rec: MemoryRecord, matched, scope_score, semantic: bool, seed) -> str:
        parts = []
        if matched:
            parts.append("matched: " + ", ".join(sorted(matched)))
        if scope_score:
            parts.append("applies to this file" if scope_score >= 1.0 else "nearby file")
        if semantic:
            parts.append("semantically similar")
        if seed is not None:
            parts.append(f"linked to {seed[1]}")
        if rec.agent and rec.agent != "unknown":
            parts.append(f"{rec.agent}, {rec.created_at[:10]}")
        return " · ".join(parts)

    @staticmethod
    def _dedup(items: List[ScoredMemory]) -> List[ScoredMemory]:
        kept: List[ScoredMemory] = []
        kept_tokens: List[set] = []
        for item in items:
            tokens = set(tokens_for(item.record.text))
            duplicate = False
            for other in kept_tokens:
                union = tokens | other
                if union and len(tokens & other) / len(union) >= DEDUP_JACCARD:
                    duplicate = True
                    break
            if not duplicate:
                kept.append(item)
                kept_tokens.append(tokens)
        return kept

    # -- always-on sets ----------------------------------------------------

    @staticmethod
    def _pinned(cands: Dict[NodeRef, MemoryRecord], taken: set) -> List[ScoredMemory]:
        out = []
        for ref, rec in cands.items():
            if rec.priority == "must" and rec.scope == ["*"] and ref not in taken:
                out.append(ScoredMemory(rec, rec.confidence, {"pinned": 1.0}, "unknown",
                                        rec.tier, rec.text, "always applies", pinned=True))
        out.sort(key=lambda s: (-s.score, str(s.record.id)))
        return out

    def _open_session_items(self, cands: Dict[NodeRef, MemoryRecord], taken: set) -> List[ScoredMemory]:
        latest, started = None, ""
        for node in self.graph.nodes("session"):
            if not node.properties.get("ended") and str(node.properties.get("started") or "") >= started:
                latest, started = node.id, str(node.properties.get("started") or "")
        if latest is None:
            return []
        out = []
        for ref, rec in cands.items():
            # session kinds carry their session in the envelope's session_id
            if ref[0] in SESSION_KINDS and rec.session_id == latest and ref not in taken:
                out.append(ScoredMemory(rec, 0.0, {"session": 1.0}, "unknown", BACKGROUND,
                                        rec.text, "open from the current session"))
        return out

    # -- public ------------------------------------------------------------

    def recall(self, task: str = "", files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None, kinds: Optional[List[str]] = None,
               max_tokens: int = 2000, compact: bool = False) -> RecallResult:
        files = [norm_path(f, self.repo_root) for f in (files or [])]
        query = " ".join([task or ""] + list(symbols or []))
        cands = self._candidates(kinds)     # read once per recall
        ranked = self.rank(query, files, kinds, cands=cands)
        taken = {item.record.ref for item in ranked}
        pinned = self._pinned(cands, taken) if kinds is None else []
        taken |= {item.record.ref for item in pinned}
        carried = self._open_session_items(cands, taken) if kinds is None else []

        budget = max_tokens - self.counter.count("\n".join(HEADERS.values()) + "\n")
        pinned_cap = int(max_tokens * PINNED_BUDGET_SHARE)
        selected: List[ScoredMemory] = []
        used = pinned_used = 0
        for group, cap in ((pinned, pinned_cap), (ranked, None), (carried, None)):
            for item in group:
                cost = self.counter.count(render_item(item, compact) + "\n")
                if used + cost > budget:
                    continue          # skip, keep packing smaller items
                if cap is not None and pinned_used + cost > cap:
                    continue
                selected.append(item)
                used += cost
                if cap is not None:
                    pinned_used += cost

        result = RecallResult(task or "", files, selected, 0, compact)
        while result.items and self.counter.count(result.to_prompt()) > max_tokens:
            result.items.pop()
        result.tokens = self.counter.count(result.to_prompt())
        if self.sink is not None:
            self.sink(RecallEvent(result.task, files, [i.record.id for i in result.items]))
        return result
```

- [ ] **Step 4: Export from the package**

`src/agent_memory/retrieval/__init__.py`:

```python
"""Retrieval: ranking memories for a coding task."""
from .budget import TokenCounter
from .retriever import RecallEvent, RecallResult, Retriever, ScoredMemory

__all__ = ["TokenCounter", "RecallEvent", "RecallResult", "Retriever", "ScoredMemory"]
```

- [ ] **Step 5: Add `recall` to `AgentMemory`**

In `src/agent_memory/memory.py`, add the import:

```python
from .retrieval import RecallResult, Retriever
```

In `_bind`, after `self.writer = MemoryWriter(...)`, add:

```python
        self._retriever = Retriever(graph, self.repo_root)
```

Add to the "Record operations" section:

```python
    def recall(self, task: str = "", files: Optional[List[str]] = None,
               symbols: Optional[List[str]] = None, kinds: Optional[List[str]] = None,
               max_tokens: int = 2000, compact: bool = False) -> RecallResult:
        """Memories relevant to a task, ranked, within a token budget.

        Returns an empty result when nothing stored is relevant.
        """
        return self._retriever.recall(task=task, files=files, symbols=symbols, kinds=kinds,
                                      max_tokens=max_tokens, compact=compact)

    def attach_semantic(self, searcher) -> None:
        """Add an optional embedding channel. `searcher.search(query, k=...)` must return
        objects with `.node_ref` and `.score`; GraphEmbeddedMemory qualifies."""
        self._retriever.semantic = searcher
```

- [ ] **Step 6: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_retriever.py -q -p no:cacheprovider`
Expected: 21 passed (17 tests, one parametrised five ways)

If `test_graph_hop_brings_the_fix_along` fails because the fix was merged into the lexical survivors and has no `linked to`, change the task text in that test to `"payment provider"` so only the pattern matches lexically; the behaviour under test is the hop.

- [ ] **Step 7: Run everything and commit**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider --ignore=tests/eval`
Expected: all passed, 1 skipped

```bash
git add src/agent_memory/retrieval src/agent_memory/memory.py tests/test_retriever.py
git commit -m "feat: recall() with fused ranking, relevance floor and measured budget"
```

---

### Task 9: Guardrail `check()`

**Files:**
- Modify: `src/agent_memory/retrieval/retriever.py` (add `check`)
- Modify: `src/agent_memory/memory.py` (add `check`, extend `check_suggestion`)
- Test: `tests/test_check.py`

**Interfaces:**
- Consumes: `Retriever.rank(query, files, kinds, strict, extra_refs)`
- Produces:
  - `Retriever.check(approach: str, files=None, limit=10) -> List[ScoredMemory]`
  - `AgentMemory.check(approach, files=None) -> List[ScoredMemory]`
  - `AgentMemory.check_suggestion(suggestion) -> List[Dict]` keeps every dict it returned before and appends `{"type": "guardrail", "id", "kind", "message", "score"}` entries.

- [ ] **Step 1: Write the failing tests**

`tests/test_check.py`:

```python
import pytest

from agent_memory import AgentMemory


@pytest.fixture
def memory(tmp_path):
    p = tmp_path / "webhooks" / "payment.py"
    p.parent.mkdir()
    p.write_text("def handle():\n    pass\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    m.storm = m.remember("bug_pattern", {"name": "webhook_storm",
                                         "signature": "unbounded retry on external webhook"},
                         anchors=["webhooks/payment.py"])
    m.remember("bug_pattern", {"name": "email_dup", "signature": "retry of email send duplicates"})
    m.remember("bug_pattern", {"name": "queue_dup", "signature": "retry of queue publish duplicates"})
    m.decisions.add_decision("Use Postgres for billing", "transactions")
    m.decisions.add_alternative("Use Postgres for billing", "MongoDB", "no transactions")
    m.convention = m.remember("convention", {"category": "style", "rule": "retry helpers live in utils"})
    return m


def kinds(warnings):
    return {w.record.kind for w in warnings}


def test_paraphrased_approach_with_the_file_finds_the_bug_pattern(memory):
    warnings = memory.check("Retry indefinitely with exponential backoff",
                            files=["webhooks/payment.py"])
    assert warnings[0].record.id == memory.storm.id


def test_single_common_term_without_a_file_is_not_enough(memory):
    assert memory.check("Retry indefinitely with exponential backoff") == []


def test_distinctive_single_term_finds_the_rejected_alternative(memory):
    warnings = memory.check("switch the billing ledger to MongoDB")
    assert [w.record.kind for w in warnings] == ["alternative"]


def test_only_guardrail_kinds_are_returned(memory):
    warnings = memory.check("put the retry helpers for the external webhook somewhere new",
                            files=["webhooks/payment.py"])
    assert "convention" not in kinds(warnings)


def test_unrelated_approach_returns_nothing(memory):
    assert memory.check("render the avatar as a circle") == []


def test_orphaned_records_are_excluded(memory, tmp_path):
    (tmp_path / "webhooks" / "payment.py").unlink()
    assert memory.check("unbounded retry on the external webhook") == []


def test_constraining_decision_is_checked(memory):
    memory.codebase.add_module("src/api", "HTTP layer")
    memory.decisions.add_decision("Handlers stay async", "event loop must not block")
    memory.decisions.add_constraint("Handlers stay async", "src/api", "no blocking calls in handlers")
    warnings = memory.check("add a blocking call in the async handlers", files=["src/api/orders.py"])
    assert any(w.record.kind == "decision" and "constraint:" in w.text for w in warnings)


def test_check_suggestion_keeps_its_old_shape_and_adds_guardrails(memory):
    out = memory.check_suggestion("let us use mongodb for the ledger")
    old = [w for w in out if w["type"] == "decision_conflict"]
    new = [w for w in out if w["type"] == "guardrail"]
    assert old and {"decision", "alternative", "conflict"} <= set(old[0])
    assert new and {"id", "kind", "message", "score"} <= set(new[0])
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_check.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: 'AgentMemory' object has no attribute 'check'`

- [ ] **Step 3: Add `check` to `Retriever`**

Append to the `Retriever` class in `retrieval/retriever.py`:

```python
    def check(self, approach: str, files: Optional[List[str]] = None,
              limit: int = 10) -> List[ScoredMemory]:
        """Prior negative experience relevant to a proposed approach.

        Guardrail kinds only, plus decisions that constrain a module holding
        one of `files`. Stricter floor than recall(); orphaned records are
        excluded. This reports relevance, not contradiction: deciding whether
        the approach actually conflicts is the caller's job.
        """
        files = [norm_path(f, self.repo_root) for f in (files or [])]
        guardrails = [kind for kind, spec in KINDS.items() if spec.tier == GUARDRAIL]
        _, promoted = scope_scores(self.graph, self._candidates(["decision"]), files)
        ranked = self.rank(approach or "", files, guardrails, strict=True, extra_refs=promoted)
        return ranked[:limit]
```

- [ ] **Step 4: Add `check` to `AgentMemory` and extend `check_suggestion`**

In `src/agent_memory/memory.py`, add to the "Record operations" section:

```python
    def check(self, approach: str, files: Optional[List[str]] = None):
        """Rejected alternatives, bug patterns, failed refactors and constraints
        relevant to `approach`. Empty when nothing relevant is stored."""
        return self._retriever.check(approach, files)
```

In `check_suggestion`, immediately before `return warnings`, add:

```python
        # Ranked guardrails, in addition to the exact matches above.
        for item in self.check(suggestion):
            warnings.append({
                "type": "guardrail",
                "id": item.record.id,
                "kind": item.record.kind,
                "message": item.text,
                "score": item.score,
            })
```

- [ ] **Step 5: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_check.py -q -p no:cacheprovider`
Expected: 8 passed

- [ ] **Step 6: Run everything and commit**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider --ignore=tests/eval`
Expected: all passed, 1 skipped

```bash
git add src/agent_memory/retrieval/retriever.py src/agent_memory/memory.py tests/test_check.py
git commit -m "feat: ranked guardrail check"
```

---

### Task 10: `get_context()` as a view over `recall()`

**Files:**
- Modify: `src/agent_memory/context.py`
- Test: `tests/test_context_compat.py`

**Interfaces:**
- Consumes: `AgentMemory.recall`, `RecallResult`, `ScoredMemory`
- Produces: `CodingContext` keeps every existing attribute and gains `items: List[ScoredMemory]` and `result: Optional[RecallResult]`. `ContextBuilder.build(task, files, include, exclude, max_tokens)` keeps its signature. `to_prompt()` renders from `result` when present and falls back to the old section rendering for a hand-built context.

- [ ] **Step 1: Write the failing tests**

`tests/test_context_compat.py`:

```python
from agent_memory import AgentMemory
from agent_memory.context import CodingContext


def build():
    m = AgentMemory("t")
    m.conventions.add_convention("naming", "cache keys use colon namespaces", "ruff", None, "src/cache")
    m.conventions.add_idiom("null_check", "if x is None", "if not x")
    m.decisions.add_decision("Use Postgres for billing", "transactions")
    m.decisions.add_decision("Cache entries expire after an hour", "bounded staleness")
    m.bugs.add_pattern("cache_stampede", "concurrent cache misses recompute the key",
                       modules_affected=["src/cache"])
    m.bugs.add_fix_pattern("cache_stampede", "take a per-key lock", "")
    m.codebase.add_module("src/cache", "LRU caching layer")
    m.performance.add_hot_path("cache_lookup", "very_high", untouchable=True)
    return m


def test_context_is_task_specific_now():
    ctx = build().get_context("fix the cache stampede", files=["src/cache/lru.py"])
    assert [b["pattern"]["name"] for b in ctx.bug_risks if "pattern" in b] == ["cache_stampede"]
    assert any("fix" in b for b in ctx.bug_risks)      # the fix arrives by graph hop
    assert any(c.get("rule") == "cache keys use colon namespaces" for c in ctx.conventions)
    titles = [d.get("title") for d in ctx.decisions]
    assert "Cache entries expire after an hour" in titles
    assert "Use Postgres for billing" not in titles
    assert ctx.estimated_tokens > 0 and ctx.items


def test_unrelated_task_gets_an_empty_context():
    ctx = build().get_context("fix CSS on the landing page", files=["web/landing.css"])
    assert ctx.items == [] and ctx.to_prompt() == ""
    assert ctx.conventions == [] and ctx.decisions == []


def test_prompt_contains_every_selected_item():
    ctx = build().get_context("fix the cache stampede lookup", files=["src/cache/lru.py"])
    prompt = ctx.to_prompt()
    assert all(f"[{i.record.id}]" in prompt for i in ctx.items)
    assert "Cache entries expire after an hour" in prompt


def test_to_dict_keeps_its_keys():
    data = build().get_context("fix the cache stampede").to_dict()
    assert {"task", "files", "warnings", "constraints", "hot_paths", "conventions", "patterns",
            "bug_risks", "dependencies", "module_context", "decisions", "team_experts",
            "session_context", "estimated_tokens"} <= set(data)


def test_include_and_exclude_filter_by_the_old_store_names():
    m = build()
    only = m.get_context("fix the cache stampede", files=["src/cache/lru.py"], include=["bug_risks"])
    assert only.bug_risks and not only.conventions and not only.decisions
    without = m.get_context("fix the cache stampede", files=["src/cache/lru.py"], exclude=["bug_risks"])
    assert not without.bug_risks and without.conventions


def test_hand_built_context_still_renders_the_old_way():
    ctx = CodingContext(task="t", conventions=[{"category": "naming", "rule": "snake_case"}])
    assert "## CODE CONVENTIONS" in ctx.to_prompt()


def test_quick_context_returns_a_string():
    assert isinstance(build().get_quick_context("fix the cache stampede"), str)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_context_compat.py -q -p no:cacheprovider`
Expected: FAIL (`AttributeError: 'CodingContext' object has no attribute 'items'`, and the unrelated-task test returns conventions and decisions)

- [ ] **Step 3: Rework `context.py`**

In `CodingContext`, add two fields after `estimated_tokens: int = 0`:

```python
    # Ranked items behind the lists above, when built by ContextBuilder
    items: List[Any] = field(default_factory=list)
    result: Optional[Any] = None
```

Rename the existing `to_prompt` method to `_legacy_prompt` (body unchanged) and add:

```python
    def to_prompt(self) -> str:
        """Convert context to a prompt-ready string"""
        if self.result is not None:
            return self.result.to_prompt()
        return self._legacy_prompt()
```

Replace the whole `ContextBuilder` class with:

```python
class ContextBuilder:
    """
    Builds a CodingContext from AgentMemory.recall().

    The category lists on CodingContext are a view over the ranked items,
    kept so existing callers continue to work.
    """

    # Old store name -> memory kinds it covers. Order is the legacy priority order.
    STORE_KINDS = {
        "warnings": ["alternative", "refactoring_attempt"],
        "constraints": [],            # decisions promoted by a CONSTRAINS edge
        "hot_paths": ["hot_path", "performance_constraint"],
        "conventions": ["convention", "idiom", "test_convention", "required_test_case"],
        "patterns": ["project_pattern", "template", "snippet"],
        "bug_risks": ["bug_pattern", "fix_pattern", "bug"],
        "dependencies": ["quirk", "workaround"],
        "module_context": ["module", "design_pattern", "tech_debt"],
        "decisions": ["decision"],
        "team_experts": ["expertise_area"],
    }
    STORES = [(name, 1.0) for name in STORE_KINDS]
    SESSION_KINDS = ["session_decision", "open_question", "blocker", "partial_work"]

    def __init__(self, memory: "AgentMemory"):
        self.memory = memory

    def build(
        self,
        task: str,
        files: List[str],
        include: Optional[List[str]] = None,
        exclude: Optional[List[str]] = None,
        max_tokens: int = 4000,
    ) -> CodingContext:
        """Build context for a task"""
        names = include or list(self.STORE_KINDS)
        if exclude:
            names = [n for n in names if n not in exclude]
        filtered = bool(include or exclude)

        kinds = None
        if filtered:
            kinds = [k for n in names for k in self.STORE_KINDS.get(n, [])]
            if "constraints" in names and "decision" not in kinds:
                kinds.append("decision")
            kinds += self.SESSION_KINDS

        result = self.memory.recall(task=task, files=files, kinds=kinds, max_tokens=max_tokens)
        if filtered:
            result.items = [i for i in result.items if self._bucket(i) in names + ["session"]]
            result.tokens = self.memory._retriever.counter.count(result.to_prompt())

        context = CodingContext(task=task, files=files, items=result.items, result=result,
                                estimated_tokens=result.tokens)
        for item in result.items:
            self._place(context, item)
        return context

    def _bucket(self, item) -> str:
        kind = item.record.kind
        if kind in self.SESSION_KINDS:
            return "session"
        if kind == "decision":
            return "constraints" if item.tier == "guardrail" else "decisions"
        for name, kinds in self.STORE_KINDS.items():
            if kind in kinds:
                return name
        return "module_context"

    def _place(self, context: CodingContext, item) -> None:
        rec = item.record
        props = {"id": rec.id, **rec.fields}
        bucket = self._bucket(item)
        if bucket == "session":
            key = {"open_question": "open_questions", "session_decision": "decisions",
                   "blocker": "blockers", "partial_work": "partial_work"}[rec.kind]
            context.session_context.setdefault(key, []).append(props)
        elif bucket == "warnings":
            context.warnings.append({"type": rec.kind, "message": item.text, **props})
        elif bucket == "constraints":
            context.constraints.append({"decision": rec.fields.get("title", ""),
                                        "how": item.text, **props})
        elif bucket == "hot_paths":
            context.hot_paths.append({"warning": item.text, **props})
        elif bucket == "bug_risks":
            key = "fix" if rec.kind == "fix_pattern" else "pattern"
            context.bug_risks.append({"type": "bug_risk", key: props})
        elif bucket == "dependencies":
            key = "workaround" if rec.kind == "workaround" else "quirk"
            context.dependencies.append({key: props})
        else:
            getattr(context, bucket).append({"type": rec.kind, **props})
```

Add `Any` to the `typing` import at the top of the file if it is not already there.

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_context_compat.py -q -p no:cacheprovider`
Expected: 7 passed

- [ ] **Step 5: Check the callers**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider --ignore=tests/eval`
Expected: all passed, 1 skipped

Run: `grep -rn "TOKEN_ESTIMATES\|_get_from_store\|_estimate_tokens\|_get_warnings\|_get_decision_context" src examples`
Expected: no matches. If `cache_integration.py` or another module references one of these removed members, replace that use with `memory.get_context(...)` and its public attributes, and say so in the commit message.

Run each example to confirm it still executes: `PYTHONPATH=src python examples/agent_workflow.py` and `PYTHONPATH=src python examples/greenfield_workflow.py`
Expected: both exit 0. Their printed context is now task-specific and may be shorter.

- [ ] **Step 6: Commit**

```bash
git add src/agent_memory/context.py tests/test_context_compat.py
git commit -m "refactor: get_context is a view over recall"
```

---

### Task 11: Consolidation without deletion

**Files:**
- Modify: `src/agent_memory/consolidation.py`
- Test: `tests/test_consolidation.py`

**Interfaces:**
- Consumes: `MemoryRecord`, `KINDS`, `normalize_text`, `utc_now` from `record.py`
- Produces: `MemoryConsolidator.consolidate()` returns the same four keys. `decayed` counts nodes archived; `merged` counts duplicates actually merged.

- [ ] **Step 1: Write the failing tests**

`tests/test_consolidation.py`:

```python
from datetime import datetime, timedelta

from agent_memory import AgentMemory
from agent_memory.record import MemoryRecord


def old(days):
    return (datetime.now() - timedelta(days=days)).isoformat()


def test_decay_archives_and_never_deletes():
    m = AgentMemory("t")
    m.graph.add_node("partial_work", "pw_old", session_id="s", work_type="fn",
                     location="a.py", state="half", added=old(30))
    summary = m._consolidator.consolidate()
    assert summary["decayed"] == 1
    assert m.graph.has_node("partial_work", "pw_old")
    assert MemoryRecord.from_node(m.graph.get_node("partial_work", "pw_old")).status == "archived"
    assert m._consolidator.consolidate()["decayed"] == 0


def test_an_edge_no_longer_makes_a_node_immortal(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    m = AgentMemory("t", repo_root=str(tmp_path))
    m.graph.add_node("partial_work", "pw_old", session_id="s", work_type="fn",
                     location="b.py", state="half", added=old(30))
    m.anchor("pw_old", "a.py")
    assert m._consolidator.consolidate()["decayed"] == 1


def test_exact_duplicates_are_merged_into_the_most_confident():
    m = AgentMemory("t")
    m.graph.add_node("convention", "c1", category="naming", rule="snake_case", confidence=0.6)
    m.graph.add_node("convention", "c2", category="naming", rule="Snake_Case", confidence=0.8,
                     evidence="lint config")
    summary = m._consolidator.consolidate()
    assert summary["merged"] == 1
    keep = MemoryRecord.from_node(m.graph.get_node("convention", "c2"))
    drop = MemoryRecord.from_node(m.graph.get_node("convention", "c1"))
    assert keep.status == "active" and drop.status == "superseded"
    assert m.graph.has_edge("SUPERSEDES", ("convention", "c2"), ("convention", "c1"))
    assert m._consolidator.consolidate()["merged"] == 0


def test_archived_records_are_not_recalled():
    m = AgentMemory("t")
    m.graph.add_node("open_question", "q_old", session_id="s", question="max batch size?",
                     answered_in="", answer="", added=old(60))
    m._consolidator.consolidate()
    assert m.recall(task="max batch size").items == []
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_consolidation.py -q -p no:cacheprovider`
Expected: FAIL (the node is deleted; `merged` counts but does not merge)

- [ ] **Step 3: Edit `consolidation.py`**

Add below the existing imports:

```python
from .record import KINDS, MemoryRecord, normalize_text, utc_now
```

In `_decay_old_memories`, replace the loop body's last section and the removal loop. The block that currently reads:

```python
                # Check if actively used
                if self._is_actively_used(node):
                    continue

                nodes_to_remove.append((node.type, node.id))
                decayed += 1

        # Remove decayed nodes
        for node_type, node_id in nodes_to_remove:
            try:
                self.memory.graph.remove_node(node_type, node_id)
            except:
                pass

        return decayed
```

becomes:

```python
                if node.properties.get("status") == "archived":
                    continue

                nodes_to_remove.append((node.type, node.id))
                decayed += 1

        # Archive, never delete: the record stays for history and is not recalled.
        now_iso = utc_now()
        for node_type, node_id in nodes_to_remove:
            self.memory.graph.update_node(node_type, node_id, status="archived",
                                          invalid_at=now_iso, updated_at=now_iso)

        return decayed
```

Delete the `_is_actively_used` method.

Replace `_merge_similar` with:

```python
    def _merge_similar(self) -> int:
        """Merge active records of the same kind with the same normalised text."""
        merged = 0
        graph = self.memory.graph
        for kind in KINDS:
            groups: Dict[str, List[MemoryRecord]] = {}
            for node in graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status == "active" and rec.text:
                    groups.setdefault(normalize_text(rec.text), []).append(rec)
            for records in groups.values():
                if len(records) < 2:
                    continue
                records.sort(key=lambda r: (-r.confidence, r.created_at or "", str(r.id)))
                keep, rest = records[0], records[1:]
                evidence = [keep.evidence] + [r.evidence for r in rest]
                evidence = "; ".join(dict.fromkeys(e for e in evidence if e))
                now = utc_now()
                graph.update_node(kind, keep.id, confidence=min(0.95, keep.confidence + 0.1),
                                  evidence=evidence, updated_at=now)
                for rec in rest:
                    graph.update_node(kind, rec.id, status="superseded", invalid_at=now, updated_at=now)
                    if not graph.has_edge("SUPERSEDES", keep.ref, rec.ref):
                        graph.add_edge("SUPERSEDES", keep.ref, rec.ref, reason="duplicate", date=now)
                    merged += 1
        return merged
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_consolidation.py -q -p no:cacheprovider`
Expected: 4 passed

- [ ] **Step 5: Run everything and commit**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider --ignore=tests/eval`
Expected: all passed, 1 skipped

```bash
git add src/agent_memory/consolidation.py tests/test_consolidation.py
git commit -m "fix: consolidation archives and merges instead of deleting and counting"
```

---

### Task 12: Embedding index reads the registry

**Files:**
- Modify: `src/agent_memory/embeddings_integration.py`
- Test: `tests/test_embeddings_integration.py` (existing, must still pass; one test added)

**Interfaces:**
- Consumes: `KINDS`, `MemoryRecord`
- Produces: `GraphEmbeddedMemory.index_all()` indexes every active registry kind by its `text`. `GraphEmbeddedMemory` is usable with `AgentMemory.attach_semantic`.

- [ ] **Step 1: Add a failing test**

Append to `tests/test_embeddings_integration.py`, inside the module (reuse the file's existing skip condition and imports; look at how the `embedded` fixture builds a `GraphEmbeddedMemory` with `use_mock_encoder=True` and follow it):

```python
def test_indexes_registry_kinds_and_plugs_into_recall():
    from agent_memory import AgentMemory
    m = AgentMemory("t")
    quirk = m.remember("quirk", {"description": "pipeline drops commands over the payload limit"})
    gone = m.remember("quirk", {"description": "an old belief that was wrong"})
    m.retract(gone.id, "wrong")
    embedded = GraphEmbeddedMemory(m, use_mock_encoder=True)
    counts = embedded.index_all()
    assert counts["quirk"] == 1 and counts["total"] == 1
    m.attach_semantic(embedded)
    m.recall(task="pipeline drops commands")   # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src python -m pytest tests/test_embeddings_integration.py -q -p no:cacheprovider`
Expected: the new test FAILS with `KeyError: 'quirk'` (or the module is skipped if `ison-graph-embeddings` is not installed; in that case install it from `../isongraph/hiding_emb/ison-graph-embeddings` with `pip install --user -e` before continuing).

- [ ] **Step 3: Switch the index to the registry**

In `src/agent_memory/embeddings_integration.py`:

Add the import after the `TYPE_CHECKING` block:

```python
from .record import KINDS, MemoryRecord
```

Replace the `INDEXABLE_TYPES` dictionary and its comment with:

```python
# Kept for importers. The kind registry in record.py is the source of truth;
# each kind is embedded by its rendered text.
INDEXABLE_TYPES: Dict[str, List[str]] = {kind: [] for kind in KINDS}
```

In `index_all`, replace the `for node in self.memory.graph.nodes():` loop with:

```python
        for kind in KINDS:
            for node in self.memory.graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status != "active" or not rec.text:
                    continue
                self._store.add(node.ref, rec.text)
                counts[kind] = counts.get(kind, 0) + 1
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src python -m pytest tests/test_embeddings_integration.py -q -p no:cacheprovider`
Expected: all passed, including the existing count assertions (`convention == 2`, `decision == 1`, `total == 3`). If an existing count changes because the fixture holds another registry kind that was not indexed before, stop and report it rather than editing the existing assertion.

- [ ] **Step 5: Commit**

```bash
git add src/agent_memory/embeddings_integration.py tests/test_embeddings_integration.py
git commit -m "refactor: embedding index covers every registry kind"
```

---

### Task 13: Evaluation gate and latency

**Files:**
- Create: `tests/eval/test_thresholds.py`
- Create: `tests/eval/RESULTS.md`

**Interfaces:**
- Consumes: `tests/eval/harness.evaluate`, `tests/eval/run.build`, `tests/eval/run.make_fns`

- [ ] **Step 1: Write the threshold tests**

`tests/eval/test_thresholds.py`:

```python
"""The retrieval-level gate from the spec, section 9."""
import statistics
import time

import pytest

from agent_memory import AgentMemory

from .harness import evaluate
from .run import build, make_fns
from .scenarios import SCENARIOS


@pytest.fixture(scope="module")
def card(tmp_path_factory):
    memory = build(tmp_path_factory.mktemp("shopline"))
    return evaluate(*make_fns(memory))


def test_scenario_count_and_balance():
    by = {}
    for s in SCENARIOS:
        by[s.condition] = by.get(s.condition, 0) + 1
    assert len(SCENARIOS) >= 48 and len(by) == 8 and min(by.values()) >= 6


def test_must_include_recall(card):
    assert card.recall >= 0.90, card.failures


def test_no_superseded_or_retracted_leaks(card):
    assert card.leaks == 0, card.failures


def test_abstention(card):
    assert card.abstention >= 0.90, card.failures


def test_every_condition_passes_most_of_its_scenarios(card):
    weak = {c: f"{sum(r)}/{len(r)}" for c, r in card.by_condition.items() if sum(r) < len(r) - 1}
    assert not weak, (weak, card.failures)


def test_p95_latency_at_five_thousand_records(tmp_path):
    m = AgentMemory("big", repo_root=str(tmp_path))
    topics = ["webhook", "cache", "token", "migration", "indexer", "refund", "ledger", "queue"]
    for n in range(5000):
        topic = topics[n % len(topics)]
        m.graph.add_node("quirk", f"q{n}", dependency_id="d",
                         description=f"{topic} component {n} misbehaves when input {n * 7} is large",
                         scope=f'["services/{topic}/**"]')
    m.recall(task="warm up", files=["services/cache/lru.py"])
    samples = []
    for n in range(40):
        topic = topics[n % len(topics)]
        start = time.perf_counter()
        m.recall(task=f"{topic} misbehaves on large input", files=[f"services/{topic}/main.py"])
        samples.append((time.perf_counter() - start) * 1000)
    p95 = statistics.quantiles(samples, n=20)[18]
    assert p95 < 50, f"p95 {p95:.1f} ms"
```

- [ ] **Step 2: Run the gate**

Run: `PYTHONPATH=src python -m pytest tests/eval -q -p no:cacheprovider`
Expected: 6 passed.

- [ ] **Step 3: If a threshold fails, tune in this order**

The scenarios were written before the implementation existed and have not been run against it. Treat a failure as information, in this order:

1. Read `card.failures`. Run `PYTHONPATH=src python -m tests.eval.run` for the per-condition table.
2. If a scenario's *expectation* is wrong (a marker typo, or a task that shares a word with an unrelated memory by accident), fix the scenario and note it in the commit message. Do not weaken an expectation that describes desired behaviour.
3. Otherwise tune the constants at the top of `retrieval/retriever.py` (`WEIGHTS`, `TIER_BOOST`, `SCOPE_FLOOR`, `DISTINCTIVE_DF`) or the stopword list in `retrieval/tokenize.py`. One change at a time, rerun, keep the unit tests green.
4. If guardrail scenarios fail on paraphrase that lexical matching cannot bridge, do not loosen the strict floor. Record the failures in `RESULTS.md` and report to the user that the semantic channel is needed for that condition; the spec lists this as an open risk.
5. If latency fails, profile one `recall()` first (`python -m cProfile -s cumtime`). The expected cost is decoding 5,000 nodes in `_candidates`. The fix is a cache keyed by a fingerprint: add an integer `revision` to `MemoryWriter`, bump it in `add`, `_set_status`, `_reinforce`, `feedback` and `_append_evidence`, and bump it from the two `update_node` loops in `consolidation.py`. Give `Retriever` a `writer` reference and cache the full candidate dictionary and the `BM25` object under `(writer.revision, graph.node_count(), graph.edge_count())`. Rerun.

- [ ] **Step 4: Record the result**

Run: `PYTHONPATH=src python -m tests.eval.run`
Write `tests/eval/RESULTS.md` with a first line `# Results after the retrieval rework`, the exact output, the measured p95 from Step 2 (rerun that test with `-s` after temporarily printing `p95`, or read it from the assertion message by lowering the bound once), and a two-column table of baseline versus result for the three headline numbers taken from `BASELINE.md`.

- [ ] **Step 5: Run the complete suite and commit**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider`
Expected: all passed, 1 skipped

```bash
git add tests/eval
git commit -m "test: retrieval evaluation gate and latency bound"
```

---

### Task 14: Documentation matches the code

**Files:**
- Modify: `DESCRIBE.md`
- Modify: `README.md`
- Modify: `src/agent_memory/__init__.py`
- Modify: `docs/superpowers/specs/2026-11-16-retrieval-and-record-model-design.md`

**Interfaces:**
- Produces: `from agent_memory import MemoryRecord, RecallResult, ScoredMemory, WriteResult, TokenCounter`

- [ ] **Step 1: Export the new public names**

In `src/agent_memory/__init__.py`, after `from .context import CodingContext, ContextBuilder`, add:

```python
from .record import MemoryRecord
from .writer import WriteResult
from .retrieval import RecallResult, ScoredMemory, TokenCounter
```

and add the five names to `__all__`.

- [ ] **Step 2: Write a test that the documented example runs**

Append to `tests/test_retriever.py`:

```python
def test_the_describe_md_example(tmp_path):
    (tmp_path / "webhooks").mkdir()
    (tmp_path / "webhooks" / "payment.py").write_text("def handle():\n    pass\n", encoding="utf-8")

    from agent_memory import AgentMemory
    memory = AgentMemory("checkout-service", repo_root=str(tmp_path), agent="claude-code")
    session = memory.start_session("Add retry logic to payment webhook")
    memory.remember(
        "bug_pattern",
        {"name": "webhook_storm", "signature": "unbounded retry on external webhook"},
        anchors=["webhooks/payment.py"], evidence="INC-2026-03")
    context = memory.recall(task="add retry logic", files=["webhooks/payment.py"])
    assert "webhook_storm" in context.to_prompt()
    warnings = memory.check("Retry indefinitely with exponential backoff",
                            files=["webhooks/payment.py"])
    assert warnings and "webhook_storm" in warnings[0].text
    memory.decisions.add_decision("Cap payment webhook retries at 5 attempts",
                                  "Unbounded retry caused INC-2026-03")
    memory.end_session(session, completion=1.0)
    memory.save(str(tmp_path / "project.memory"))
    again = AgentMemory.load(str(tmp_path / "project.memory"), repo_root=str(tmp_path))
    assert "Cap payment webhook retries" in again.recall(task="payment webhook retries").to_prompt()
```

Run: `PYTHONPATH=src python -m pytest tests/test_retriever.py::test_the_describe_md_example -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 3: Correct `DESCRIBE.md`**

Replace the code block under `## Example: a coding session that remembers what came before` with the body of the test from Step 2 (from `from agent_memory import AgentMemory` through the final `recall`, without the `assert` lines and the `tmp_path` setup, using `repo_root="/repo"`).

Replace the `## Maturity` section's text with one sentence stating the current test count as reported by the last full run, and that retrieval is gated by the evaluation suite in `tests/eval`.

In `## What it can actually do`, replace the "Session lifecycle" and "Context retrieval" paragraphs with:

```markdown
**Session lifecycle** — `start_session`/`end_session`; open questions,
blockers and partial work from an unfinished session are carried into the
next `recall()`.

**Recall** — `recall(task, files)` ranks memories by task text, file scope
and graph links, returns nothing when nothing is relevant, and fits the
result into a measured token budget. Each item says why it was returned and
who recorded it.

**Guardrail check** — `check(approach, files)` returns rejected
alternatives, bug patterns and failed refactors relevant to a proposed
approach.

**Provenance and staleness** — every record carries its author, evidence,
confidence and status. A record anchored to a file is flagged when that
file changes, and records are superseded or archived, never deleted.
```

Delete the "Conventions" paragraph's reference to `check_before_coding` (that method lives on `AgentIntegration`, not `AgentMemory`); say `check()` instead.

In `## Limitations`, keep the two existing bullets and add:

```markdown
- Matching is lexical unless the optional embedding channel is attached, so
  a paraphrase that shares no word with a memory will not find it.
- Staleness is tracked per file, not per function; a busy file marks every
  memory anchored to it as changed.
- Concurrent writers are not yet safe: two processes saving the same file
  overwrite each other.
```

- [ ] **Step 4: Correct `README.md`**

In the Quick Start Python block, replace the `# Get coding context before a task` stanza with:

```python
# Recall what matters for a task
result = memory.recall(
    task="modify the cache module",
    files=["src/cache.py"],
    max_tokens=2000,
)
print(result.to_prompt())

# Check an approach against prior experience
for warning in memory.check("add a global lock around the cache", files=["src/cache.py"]):
    print(warning.text, "-", warning.why)
```

Replace the `## Memory Consolidation` code block's two calls with `memory._consolidator.consolidate()` and `memory._consolidator.get_memory_health()` unchanged, and replace the "Consolidation strategies" list with:

```markdown
Consolidation archives old session items, merges exact duplicates, and never
deletes a record.
```

Replace the `## Context Building` section body with a short description: `get_context()` still works and returns a `CodingContext` whose lists are a view over `recall()`; new code should call `recall()`.

Add a section before `## ISONQL Queries`:

```markdown
## Records

Every memory carries who recorded it, the evidence, a confidence, a scope
and a status.

```python
r = memory.remember(
    "bug_pattern",
    {"name": "cache_stampede", "signature": "concurrent misses recompute the key"},
    anchors=["src/cache.py"],
    evidence="PR 412",
)
memory.supersede(r.id, "bug_pattern",
                 {"name": "cache_stampede", "signature": "concurrent misses recompute the key; fixed by per-key lock"})
memory.feedback(r.id, helpful=True)
memory.verify(r.id)      # the anchored file changed and the memory still holds
memory.retract(r.id, "no longer true")
```
```

- [ ] **Step 5: Bring the spec in line**

Apply the seven items under "Deviations from the spec" at the top of this plan to the spec file:
- 4.1: in the `text` row, replace the Meaning with "Canonical one-statement form of the memory, derived from kind fields at read time (4.3). Not persisted."
- 4.3: change the "Default priority" column to `should`, `should`, `info`, and add below the table: "`must` is never a default; it is set explicitly."
- 4.3: change the first sentence to "A single table replaces `TOKEN_ESTIMATES` and the `INDEXABLE_TYPES` table in `embeddings_integration.py`."
- 4.5: add `anchor(id, path, symbol)` to the list of write operations.
- 6.2 step 2, Graph: replace "A neighbour inherits half the seed's score." with "A neighbour inherits half the seed's fused score directly; it does not enter rank fusion."
- 6.2 step 3: remove "graph 0.5" from the weights.
- 6.2 step 8: replace the parenthesis with "(`ceil(chars / 4)` by default; `tiktoken` is opt-in because loading an encoding can require a download)".
- 6.4: replace "two matching terms, or one term plus a scope hit" with "two matching terms, or one term plus a scope hit, or one term that appears in at most two guardrail records".
- 7: remove `cache_integration.py` from the last sentence.

- [ ] **Step 6: Final verification**

Run: `PYTHONPATH=src python -m pytest tests -q -p no:cacheprovider`
Expected: all passed, 1 skipped

Run: `PYTHONPATH=src python -c "from agent_memory import AgentMemory, MemoryRecord, RecallResult, ScoredMemory, WriteResult, TokenCounter; print('ok')"`
Expected: `ok`

Run: `grep -n "session_start\|get_handoff\|project_id=" DESCRIBE.md README.md`
Expected: no matches

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: README, DESCRIBE and spec match the implemented API"
```
