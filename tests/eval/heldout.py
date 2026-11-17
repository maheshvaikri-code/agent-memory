"""Held-out abstention scenarios: plausible tasks the world stores nothing about.

The main suite's abstention scenarios were used while tuning the retriever, so their
score is an in-sample figure. These were written afterwards and are never used for
tuning: they measure how often an unrelated task gets an empty context (apart from
the pinned set) on tasks the retriever was not shaped around. The first twelve are
the final reviewer's; the last four were added with the set, before it was ever run.
Same harness, same rule as the main suite: nothing but the pinned set may appear.
"""
from typing import List

from .scenarios import Scenario

H = lambda task, files: Scenario("heldout", task, files, empty=True)

HELDOUT: List[Scenario] = [
    H("add a settings page for notification preferences", []),
    H("write unit tests for the date formatting helper", ["utils/dates.py"]),
    H("refactor the logging setup for the CLI", ["cli/main.py"]),
    H("improve error messages in the onboarding wizard", ["web/onboarding.tsx"]),
    H("bump numpy to the latest version", ["requirements.txt"]),
    H("add a dark theme to the docs site", ["docs/conf.py"]),
    H("handle timezone conversion in reports", ["reports/tz.py"]),
    H("document the release process", ["docs/RELEASE.md"]),
    H("speed up image upload", ["web/upload.tsx"]),
    H("add pagination to the admin user list", ["admin/users.py"]),
    H("set up a new lint rule for unused imports", [".ruff.toml"]),
    H("migrate the test runner to pytest-xdist", ["pytest.ini"]),
    H("add keyboard shortcuts to the text editor", ["web/editor.tsx"]),
    H("write a script to resize product images", ["scripts/resize_images.py"]),
    H("show a progress bar during file export", ["web/export_progress.tsx"]),
    H("configure the sentry error reporting DSN", ["config/sentry.py"]),
]
