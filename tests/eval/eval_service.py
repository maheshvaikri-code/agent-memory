"""Recall and check functions for the evaluation harness that go through MemoryService."""


def service_fns(service):
    recall_fn = lambda task, files: service.recall(task, files, max_tokens=2000)["prompt"]
    check_fn = lambda approach, files: "\n".join(
        f"- [{i['id']}] {i['text']}" for i in service.check(approach, files)["items"])
    return recall_fn, check_fn
