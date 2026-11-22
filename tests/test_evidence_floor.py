from pathlib import Path

from agent_memory import AgentMemory


def conv(m, category, rule, **kw):
    return m.remember("convention", {"category": category, "rule": rule,
                                     "enforced_by": "manual", "exceptions": ""}, **kw)


def store(tmp_path, order=None, fillers=30, common=8):
    """`common` records share the word "process"; `fillers` records share nothing.
    Forty records in all (with the one rare-word note and the pinned rule), so the store is not small."""
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    m = AgentMemory("t", repo_root=str(tmp_path))
    steps = []
    for n in range(common):
        steps.append(lambda n=n: conv(m, f"area{n}", f"process step{n} runs through widget{n}",
                                      scope=[f"svc{n}/**"]))
    for n in range(fillers):
        steps.append(lambda n=n: conv(m, f"fill{n}", f"zebra{n} policy gadget{n}",
                                      scope=[f"filler/{n}/*"]))
    steps.append(lambda: conv(m, "audio", "xylophone tuning drifts under load"))
    steps.append(lambda: m.remember("convention", {"category": "security",
                                                   "rule": "never log secrets", "enforced_by": "manual",
                                                   "exceptions": ""}, priority="must", scope=["*"]))
    for i in (order or range(len(steps))):
        steps[i]()
    return m


def ids(result):
    return [i.record.id for i in result.items]


def texts(result):
    return " ".join(i.text for i in result.items)


def test_a_record_matched_only_by_a_common_word_is_dropped(tmp_path):
    m = store(tmp_path)
    result = m.recall(task="process image uploads", files=[])
    assert "widget" not in texts(result)
    assert "never log secrets" in texts(result)             # the pinned rule still arrives


def test_a_record_matched_by_a_rare_word_is_kept(tmp_path):
    m = store(tmp_path)
    assert "xylophone" in texts(m.recall(task="xylophone tuning", files=[]))


def test_two_informative_concepts_keep_a_record_even_if_each_word_is_common(tmp_path):
    m = store(tmp_path)
    result = m.recall(task="process step3 widget3", files=[])
    assert "step3" in texts(result)


def test_a_generic_task_with_a_file_in_scope_still_returns_the_scoped_record(tmp_path):
    """Review focus 1: scope is strong evidence, whatever the words are."""
    m = store(tmp_path)
    result = m.recall(task="process it", files=["svc3/handler.py"])
    assert "step3" in texts(result)
    assert "step4" not in texts(result)


def test_the_pinned_rule_is_unaffected_by_the_tiers(tmp_path):
    """Review focus 2."""
    m = store(tmp_path)
    assert "never log secrets" in texts(m.recall(task="process", files=[]))
    assert "never log secrets" in texts(m.recall(task="", files=[]))


def test_results_do_not_depend_on_the_order_records_were_written(tmp_path):
    """Review focus 3."""
    forward = store(tmp_path / "a")
    total = 8 + 30 + 2
    backward = store(tmp_path / "b", order=list(reversed(range(total))))
    for task, files in (("process image uploads", []), ("xylophone tuning", []),
                        ("process it", ["svc3/handler.py"])):
        a = sorted(i.text for i in forward.recall(task=task, files=files).items)
        b = sorted(i.text for i in backward.recall(task=task, files=files).items)
        assert a == b, (task, a, b)


def test_check_keeps_its_own_floor(tmp_path):
    m = AgentMemory("t", repo_root=str(tmp_path))
    m.remember("bug_pattern", {"name": "retry_storm",
                               "signature": "unbounded retry of a failing webhook floods the provider"})
    assert m.check("retry the webhook forever")             # two concepts: reported


def test_a_precise_identifier_in_the_task_keeps_records_however_common_it_is(tmp_path):
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    m = AgentMemory("t", repo_root=str(tmp_path))
    for n in range(35):
        conv(m, f"pay{n}", f"StripeSignatureError note {n} means the secret rotated")
    assert "StripeSignatureError" in texts(m.recall(task="fix the StripeSignatureError", files=[]))
    assert "StripeSignatureError" not in texts(m.recall(task="fix the error", files=[]))
