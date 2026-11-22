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


def test_the_pinned_rule_that_only_shares_a_common_word_is_still_ranked(tmp_path):
    """Review focus 2, for real: the pinned rule shares one common word ("process") with eight
    other records, so its match is weak. With `kinds` restricted the always-on pinned set is
    off, so only the ranking can return it: the exemption keeps it, with its match data."""
    m = store(tmp_path)
    m.remember("convention", {"category": "ops", "rule": "never process secrets in plain text",
                              "enforced_by": "manual", "exceptions": ""}, priority="must", scope=["*"])
    result = m.recall(task="process image uploads", files=[], kinds=["convention"])
    hits = [i for i in result.items if "never process secrets" in i.text]
    assert len(hits) == 1 and "matched:" in hits[0].why
    assert "widget" not in texts(result)                    # the other common-word records are dropped


def github_store(tmp_path):
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    m = AgentMemory("t", repo_root=str(tmp_path))
    for n in range(12):
        conv(m, f"camel{n}", f"GitHub workflow{n} runs on runner{n}")
    for n in range(12):
        conv(m, f"plain{n}", f"plain github hook{n} calls endpoint{n}")
    for n in range(16):
        conv(m, f"fill{n}", f"zebra{n} policy gadget{n}")
    return m


def test_a_lower_case_task_word_is_not_lifted_by_how_a_record_spells_it(tmp_path):
    """github is in 60% of the store: a plain task word is weak whether a record writes it
    GitHub or github."""
    m = github_store(tmp_path)
    assert m.recall(task="push to github", files=[]).items == []


def test_an_identifier_given_as_a_symbol_or_context_term_lifts_the_records_that_spell_it_whole(tmp_path):
    """The service folds context identifiers to lower case and passes them as a symbol."""
    m = github_store(tmp_path)
    got = texts(m.recall(task="push the change", files=[], symbols=["github"]))
    assert "GitHub workflow" in got and "plain github" not in got


def test_an_identifier_the_task_writes_in_camel_case_lifts_every_record_with_that_word(tmp_path):
    m = github_store(tmp_path)
    got = texts(m.recall(task="push to GitHub", files=[]))
    assert "GitHub workflow" in got and "plain github" in got



def test_a_store_below_the_small_store_size_keeps_weak_matches(tmp_path):
    """Review focus 4: a new user's first notes are not dropped for being 'weak'."""
    small = AgentMemory("s", repo_root=str(tmp_path / "small"))
    for n in range(8):
        conv(small, f"a{n}", f"process step{n} runs through widget{n}")
    assert "widget" in texts(small.recall(task="process image uploads", files=[]))


def test_the_boundary_is_exact(tmp_path):
    from agent_memory.retrieval import evidence
    n = evidence.SMALL_STORE
    at = AgentMemory("at", repo_root=str(tmp_path / "at"))
    for k in range(n):                      # exactly SMALL_STORE records: not small any more
        conv(at, f"a{k}", f"process step{k} runs through widget{k}")
    below = AgentMemory("below", repo_root=str(tmp_path / "below"))
    for k in range(n - 1):
        conv(below, f"a{k}", f"process step{k} runs through widget{k}")
    assert "widget" not in texts(at.recall(task="process image uploads", files=[]))
    assert "widget" in texts(below.recall(task="process image uploads", files=[]))


def test_small_store_is_counted_per_role_not_over_the_whole_store(tmp_path):
    """Documents the PRESENT behaviour of `bm25.n`: it is the candidate count of the role being
    ranked, not the number of active records in the store. The store is not small, so
    the weak "process" matches are dropped when recalling everything (with the decisions added it holds 45); but `kinds=["decision"]`
    ranks only the five decisions (5 < SMALL_STORE), so the same weak matches are kept. If a
    later change counts the whole store instead, this test fails and the change is deliberate."""
    m = store(tmp_path)                       # 40 records, 8 conventions share "process"
    for n in range(5):
        m.remember("decision", {"title": f"process choice{n} uses gizmo{n}",
                                "context": f"chosen for reason{n}"})
    assert "gizmo" not in texts(m.recall(task="process image uploads", files=[]))
    assert "gizmo" in texts(m.recall(task="process image uploads", files=[], kinds=["decision"]))


def test_a_weak_survivor_does_not_pull_its_neighbours_in(tmp_path):
    m = AgentMemory("g", repo_root=str(tmp_path))
    weak = m.remember("bug_pattern", {"name": "a", "signature": "process step widget leaks"})
    fix = m.remember("fix_pattern", {"fix_template": "zzyzx remedy", "caveats": ""})
    m.graph.add_edge("FIX_FOR", fix.ref, weak.ref)
    for n in range(6):                      # a small store, so the weak match survives
        conv(m, f"c{n}", f"process step{n} runs through widget{n}")
    result = m.recall(task="process image uploads", files=[])
    assert "widget leaks" in texts(result)          # the weak record itself is shown (small store)
    assert "zzyzx" not in texts(result)             # but its fix is not dragged in


def test_a_weakly_matched_pinned_record_does_not_pull_its_neighbours_in(tmp_path):
    """The pinned exemption keeps a weak pinned record as a survivor, but it must not seed the graph.
    The store is not small, and the pinned rule shares only the common word "process"."""
    m = store(tmp_path)
    pinned = m.remember("convention", {"category": "ops", "rule": "never process secrets in plain text",
                                       "enforced_by": "manual", "exceptions": ""},
                        priority="must", scope=["*"])
    fix = m.remember("fix_pattern", {"fix_template": "zzyzx remedy", "caveats": ""})
    m.graph.add_edge("FIX_FOR", fix.ref, pinned.ref)
    result = m.recall(task="process image uploads", files=[], kinds=["convention", "fix_pattern"])
    assert "never process secrets" in texts(result)         # the pinned record itself is still returned
    assert "zzyzx" not in texts(result)                     # but its neighbour is not pulled in


def store_with_billing_note(tmp_path):
    m = store(tmp_path)
    m.remember("decision", {"title": "Billing export writes CSV nightly", "context": "finance pulls it"})
    return m


def test_a_generic_word_alone_does_not_keep_a_record_even_when_it_is_rare(tmp_path):
    m = store_with_billing_note(tmp_path)
    result = m.recall(task="write a script to resize images", files=[])
    assert "Billing export" not in texts(result)


def test_a_generic_word_still_counts_with_a_second_concept(tmp_path):
    m = store_with_billing_note(tmp_path)
    result = m.recall(task="write the billing csv", files=[])
    assert "Billing export" in texts(result)


def test_a_record_matched_only_by_generic_words_is_weak_however_many(tmp_path):
    m = store_with_billing_note(tmp_path)
    result = m.recall(task="write export", files=[])
    assert "Billing export" not in texts(result)


def test_a_generic_word_that_is_a_records_own_compound_token_does_not_lift_it(tmp_path):
    """"setup" is a generic task word; a record that spells it as the compound token setUp is
    not made evidence by a caller symbol "setup"."""
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    m = AgentMemory("t", repo_root=str(tmp_path))
    for n in range(35):
        conv(m, f"t{n}", f"call setUp before test {n} runs")
    assert m.recall(task="push the change", files=[], symbols=["setup"]).items == []


def test_a_generic_word_plus_one_common_informative_word_is_strong(tmp_path):
    """R9's distinguishing case. "policy" is in more than 15% of a 25+ record store, so on its own
    it is weak; "write" is a generic task word. Together they are two matched concepts, and the
    spec says a generic word still counts toward a record that has an informative concept.
    Counting informative concepts only would leave this record weak and drop it."""
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    m = AgentMemory("t", repo_root=str(tmp_path))
    for n in range(30):
        conv(m, f"fill{n}", f"zebra{n} policy gadget{n}")
    conv(m, "target", "write policy changes through the review queue")
    assert "review queue" in texts(m.recall(task="write the policy", files=[]))
    assert m.recall(task="update the policy", files=[]).items == []      # the common word alone


def _capped_store(tmp_path):
    """Sixty records: "ledger" is in 8 (under 15% of the store), "quartz" in 3, the rest are fillers."""
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    m = AgentMemory("t", repo_root=str(tmp_path))
    for n in range(8):
        conv(m, f"led{n}", f"ledger entry{n} is reconciled nightly")
    for n in range(3):
        conv(m, f"qz{n}", f"quartz fixture{n} is vendored")
    for n in range(49):
        conv(m, f"fill{n}", f"zebra{n} policy gadget{n}")
    return m


def test_the_rare_cap_drops_a_word_above_it_and_keeps_one_below_it(tmp_path, monkeypatch):
    from agent_memory.retrieval import evidence
    m = _capped_store(tmp_path)
    monkeypatch.setattr(evidence, "RARE_CAP", None)
    assert "ledger" in texts(m.recall(task="ledger", files=[]))            # no cap: in 8 of 60, rare
    monkeypatch.setattr(evidence, "RARE_CAP", 5)
    assert "ledger" not in texts(m.recall(task="ledger", files=[]))        # 8 > 5
    assert "quartz" in texts(m.recall(task="quartz", files=[]))            # 3 <= 5
