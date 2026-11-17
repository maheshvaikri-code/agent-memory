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
