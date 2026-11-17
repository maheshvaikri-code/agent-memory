# Baseline: implementation before the retrieval rework (2026-11-16)

| Condition | Passed |
|---|---|
| task_only | 1/6 |
| files_only | 6/6 |
| absent | 6/6 |
| irrelevant | 0/6 |
| adjacent | 6/6 |
| superseded | 1/6 |
| stale | 1/6 |
| guardrail | 1/6 |

- must-include recall: 0.39 (11/28)
- superseded or retracted leaks: 0
- abstention: 0.50 (6/12)

Failures:
- [task_only] 'add retry logic to the payment webhook handler' []
- [task_only] 'why do we expire JWT access tokens so quickly' []
- [task_only] 'speed up the order lookup query' []
- [task_only] 'write a database migration for the orders table' []
- [task_only] 'concurrent cache misses recompute the same key' []
- [irrelevant] 'tidy the changelog formatting' ['docs/CHANGELOG.md']
- [irrelevant] 'bump the copyright year in the footer' ['site/footer.html']
- [irrelevant] 'rename the CI job for lint' ['.github/workflows/lint.yml']
- [irrelevant] 'fix a typo in the contributor guide' ['docs/CONTRIBUTING.md']
- [irrelevant] 'adjust editor indentation settings' ['.editorconfig']
- [irrelevant] 'add a license banner to the scripts' ['scripts/release.sh']
- [superseded] 'how are refund webhooks processed' []
- [superseded] '' ['services/payments/refunds.py']
- [superseded] 'add logging to the order handler' []
- [superseded] 'what is our logging convention' []
- [superseded] 'refund worker queue processing' []
- [stale] 'search indexer batch overflow' ['services/search/indexer.py']
- [stale] 'indexer bulk request retries backoff' ['services/search/indexer.py']
- [stale] 'billing export CSV to SFTP' []
- [stale] '' ['services/search/indexer.py']
- [stale] 'nightly billing export' []
- [guardrail] 'Retry indefinitely with exponential backoff' ['services/payments/webhook.py']
- [guardrail] 'store sessions server side in Redis instead of JWT' ['services/auth/tokens.py']
- [guardrail] 'wrap all the migrations in one transaction' ['db/migrations/runner.py']
- [guardrail] 'recompute the cache value on every miss without locking' ['services/cache/lru.py']
- [guardrail] 'skip the leeway and compare expiry timestamps exactly' ['services/auth/tokens.py']
