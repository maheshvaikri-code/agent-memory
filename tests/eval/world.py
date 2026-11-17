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
    "query_builder": "query builder",
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
    m.codebase.add_module("services/payments", "payment provider integration and refunds")
    m.decisions.add_constraint("Use Postgres for the billing ledger", "services/payments",
                               "never format SQL strings by hand; go through the query builder")

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
