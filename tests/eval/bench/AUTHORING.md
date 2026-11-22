# Authoring the recall benchmark

You are writing data, not code. Read this file and `KINDS` in `src/agent_memory/record.py`
(only that dict). **Do not read `src/agent_memory/retrieval/`, do not call `recall()` or
`check()`, and do not look at how any retriever scores your work.** The point of the benchmark
is that its authors did not tune it to a retriever. Your only check is the validation test
named in your task.

## 1. A world

A world is the memory store of one imaginary project, plus its file tree. It lives in
`tests/eval/bench/worlds/<name>.json`:

```json
{
  "files": {"app/webhooks/handlers.py": "def handle(event):\n    ...\n"},
  "records": [
    {"key": "webhook-retry-cap", "kind": "decision",
     "fields": {"title": "Cap webhook retries at 5", "context": "INC-212: an unbounded retry storm took the queue down"},
     "anchors": [["app/webhooks/handlers.py", "handle"]]},
    {"key": "never-log-secrets", "kind": "convention", "pinned": true,
     "fields": {"category": "security", "rule": "never log secrets or tokens",
                "enforced_by": "manual", "exceptions": ""}},
    {"key": "retry-storm", "kind": "bug_pattern",
     "fields": {"name": "retry_storm", "signature": "unbounded retry of a failing webhook floods the provider"}},
    {"key": "retry-fix", "kind": "fix_pattern",
     "fields": {"fix_template": "cap attempts and add jitter", "caveats": "keep the cap in settings"},
     "links": [{"rel": "FIX_FOR", "to": "retry-storm"}]},
    {"key": "old-log-rule", "kind": "convention",
     "fields": {"category": "logging", "rule": "log with print", "enforced_by": "manual", "exceptions": ""}},
    {"key": "log-rule", "kind": "convention", "supersedes": "old-log-rule",
     "fields": {"category": "logging", "rule": "use structlog with a request id", "enforced_by": "manual", "exceptions": ""}},
    {"key": "pool-leak", "kind": "bug_pattern", "retracted": "could not reproduce",
     "fields": {"name": "pool_leak", "signature": "connections leak when the pool raises"}}
  ]
}
```

Record fields:

- `key`: short unique name you invent. Tasks label records by key. Records are written in file
  order; `supersedes` and `links[].to` must name an **earlier** record.
- `kind` and `fields`: every `{placeholder}` in the kind's template in `KINDS` must be a
  non-empty field. The template is how the record reads in a prompt, so write fields that read
  like a note a developer's agent would really have saved: a reason, an incident, a number.
- `scope`: optional list of file globs (`["app/webhooks/**"]`). Some kinds derive scope from a
  field (see `scope_field` in `KINDS`); you can also set it directly.
- `anchors`: `[[path, symbol]]`, paths from `files`. Ties the note to a file.
- `pinned`: true for the one or two always-on rules of the project (`priority: must`, scope `*`).
- `supersedes`: this record replaces an earlier one; the earlier one stops being returned.
- `retracted`: a reason; the record was found wrong and stops being returned.
- `links`: `{"rel": one of FIX_FOR, WORKAROUND_FOR, HAS_QUIRK, CONSIDERED_FOR, ATTEMPTED_ON,
  INSTANCE_OF, TEST_FOR_BUG, "to": earlier key}`. This is how a bug pattern brings its fix along.

Write 80 to 100 records per world (superseded and retracted ones count), using at least twelve
different kinds, with the project's real vocabulary. Do not write near-duplicate notes: the
engine merges them and the loader rejects a merge. Two records of a "natural key" kind (a
`decision` title, a `module` path, a `hot_path` name, and so on) must not share the key unless
one supersedes the other.

**Shared vocabulary on purpose.** Real projects reuse ordinary words in unrelated places. Use
these naturally, where they fit, in records of different areas: process, handle, document,
runner, export, import, set, write, error, test, config, log, schema, update, migrate, queue.
Do not force them, and do not make a record about the word; a decision about "document
storage" and a bug in "the export job" and a convention for "error responses" are the shape.

## 2. The file tree

`files` holds 12 to 25 small realistic source or config files (a few lines each). Anchors and
task file lists draw from it.

## 3. A task

Tasks live in `tests/eval/bench/tasks/<split>-<world>.json`:

```json
{"tasks": [
  {"world": "saas", "task": "add retry with backoff to the webhook sender",
   "files": ["app/webhooks/handlers.py"],
   "relevant": ["webhook-retry-cap", "retry-storm"], "acceptable": ["retry-fix"]}
]}
```

Write the task the way a developer would give it to a coding agent: a plain sentence or two,
sometimes naming files, sometimes only files, sometimes only text. Mix the shapes. Roughly 60%
should list files.

Label every record you judge to matter:

- **relevant**: a competent engineer doing this task would want to be told this before
  starting. Showing it is required.
- **acceptable**: related to the task, harmless or mildly useful to see, not required.
- **everything else is noise**, including records that share a word with the task but mean
  something else.

An **empty task** has `"relevant": []`: nothing stored needs to be shown (it may still have
acceptable records). About a third of your tasks must be empty: plausible work in some other
area of the project, or work the memory simply knows nothing about. Make them realistic, not
absurd. Include tasks whose words collide with records of another area ("document the release
process" when a record mentions "documents" in a search index).

Give relevant records only where you are sure. Never label a superseded, retracted or pinned
record. Never label to make anything easy or hard for a retriever; you cannot see one.

## 4. Checking your work

The validation test named in your task parses everything and checks counts. Fix what it
reports. Do not weaken a test. When it passes, you are done.
