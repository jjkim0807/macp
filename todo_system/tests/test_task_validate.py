from __future__ import annotations

from todo_system import paths
from todo_system import task_validate as tv
from todo_system import worker as W


def _levels(issues, field):
    return [i.level for i in issues if i.field == field]


def test_list_form_allowed_tools_is_an_error():
    """The bug that shipped: a YAML list is discarded by the worker."""
    issues = tv.validate_frontmatter({"allowed_tools": ["Read", "Bash"]})
    assert _levels(issues, "allowed_tools") == [tv.ERROR]


def test_string_form_allowed_tools_is_clean():
    issues = tv.validate_frontmatter(
        {"allowed_tools": "Read,Write,Bash(git:*),Bash(gh:*),Bash(gcloud:*)"}
    )
    assert issues == []


def test_forbidden_pattern_is_an_error():
    issues = tv.validate_frontmatter({"allowed_tools": "Read,Bash(rm:*)"})
    assert _levels(issues, "allowed_tools") == [tv.ERROR]


def test_validator_agrees_with_the_worker():
    """Validation must be judged by the same code path the worker uses."""
    bad = {"allowed_tools": ["Read", "Bash(gh:*)"]}
    task = W.WorkerTask(slug="s", path=None, rel_path="s.md", frontmatter=bad)
    # Worker discards the list...
    assert task.allowed_tools_implement == W.DEFAULT_IMPLEMENT_TOOLS
    assert "gh" not in task.allowed_tools_implement
    # ...and the validator flags exactly that.
    assert any(i.level == tv.ERROR for i in tv.validate_frontmatter(bad))


def test_unknown_worker_mode_warns_but_does_not_error():
    issues = tv.validate_frontmatter({"worker_mode": "atuo"})
    assert _levels(issues, "worker_mode") == [tv.WARN]


def test_unknown_model_tier_warns_and_names_the_tiers():
    issues = tv.validate_frontmatter({"model_tier": "ultra"})
    assert _levels(issues, "model_tier") == [tv.WARN]
    assert "heavy, standard, light" in issues[0].message


def test_known_model_tiers_are_clean_case_insensitively():
    for tier in ("heavy", "standard", "light", "Heavy"):
        assert tv.validate_frontmatter({"model_tier": tier}) == []


def test_inline_comment_in_model_tier_is_an_error():
    issues = tv.validate_frontmatter({"model_tier": "light  # cheap"})
    assert _levels(issues, "model_tier") == [tv.ERROR]


def test_known_worker_modes_are_clean():
    # 'blocked' and 'quick' are live task_deps modes; an early hand-written
    # list omitted them and produced false positives on real vault tasks.
    for mode in ("auto", "review", "off", "manual", "blocked", "quick", "delegate", ""):
        assert tv.validate_frontmatter({"worker_mode": mode}) == [], mode


def test_template_placeholder_branch_warns():
    issues = tv.validate_frontmatter({"worker_branch": "worker/<slug>"})
    assert _levels(issues, "worker_branch") == [tv.WARN]


def test_non_integer_max_turns_warns():
    assert _levels(tv.validate_frontmatter({"max_turns": "many"}), "max_turns") == [tv.WARN]
    assert tv.validate_frontmatter({"max_turns": 200}) == []


def test_absent_fields_are_not_invented():
    """A task with no worker fields at all is a human task, not an error."""
    assert tv.validate_frontmatter({"status": "open", "tags": ["todo"]}) == []


def test_inline_comment_in_enum_field_is_an_error():
    """The shipped template had `worker_mode: auto  # ...`, which never matched 'auto'."""
    issues = tv.validate_frontmatter({"worker_mode": "auto   # auto | off"})
    assert _levels(issues, "worker_mode") == [tv.ERROR]


def test_hash_in_prose_fields_is_allowed():
    """'PR #8' is a legitimate quoted value, not a comment."""
    assert tv.validate_frontmatter({"next_action": "PR #8 로컬 게이트 green 확인 후 병합"}) == []


def test_shipped_template_scaffolds_a_blocked_task(tmp_path):
    """The scaffold must NOT be dispatchable while it is still half-written.

    todo-sync runs import-md on a timer, so a `cli new` file reaches the DB
    within minutes; with worker_mode auto the orchestrator dispatched the
    placeholder (dripcoffee-bean-cards-1 and -2, 2026-08-05 — two tasks, three
    pipeline runs). blocked is not in DISPATCHABLE_MODES, so the window closes.
    """
    from todo_system.orchestrator.tasks import DISPATCHABLE_MODES
    # Source the shipped template from the resolved vault root, not parents[2]/Todo:
    # in a split engine tree the templates live under the vault (MACP_VAULT_ROOT →
    # vault-template/Todo/templates), not beside the engine (public-split-10 §11.7 ③).
    tpl = paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md"
    fm, _ = W.parse_frontmatter(tpl.read_text(encoding="utf-8"))
    assert str(fm.get("worker_mode", "")).lower() == "blocked"
    assert "blocked" not in DISPATCHABLE_MODES


def test_shipped_template_is_valid_once_flipped_to_auto(tmp_path):
    """And flipping that one field must yield a task the worker actually runs —
    the scaffold has to be one edit away from dispatchable, not subtly broken."""
    tpl = paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md"
    fm, _ = W.parse_frontmatter(tpl.read_text(encoding="utf-8"))
    fm["worker_mode"] = "auto"
    assert not [i for i in tv.validate_frontmatter(fm) if i.level == tv.ERROR]


def test_space_form_bash_pattern_is_an_error():
    """worker.py argv-splits the whitelist, so `Bash(git *)` grants nothing."""
    issues = tv.validate_frontmatter({"allowed_tools": "Read,Bash(git *)"})
    assert _levels(issues, "allowed_tools") == [tv.ERROR]
    # Confirm the premise rather than trusting it: the token really is shredded.
    assert "Read,Bash(git *)".replace(",", " ").split() == ["Read", "Bash(git", "*)"]


def test_colonless_wildcard_warns_about_missing_word_boundary():
    issues = tv.validate_frontmatter({"allowed_tools": "Read,Bash(ls*)"})
    assert _levels(issues, "allowed_tools") == [tv.WARN]


def test_colon_form_is_clean():
    assert tv.validate_frontmatter({"allowed_tools": "Read,Bash(git:*),Bash(./gradlew:*)"}) == []


def test_bare_bash_is_clean():
    assert tv.validate_frontmatter({"allowed_tools": "Read,Bash,Bash(*)"}) == []


def test_multiword_exact_match_rule_is_unusable_here():
    """Valid Claude Code syntax, but tools.split() still shreds it in this vault."""
    issues = tv.validate_frontmatter({"allowed_tools": "Bash(git status)"})
    assert _levels(issues, "allowed_tools") == [tv.ERROR]


def test_keystore_spec_without_keytool_warns():
    """macp-dashboard-android-1-app: body required a committed keystore, but
    allowed_tools had no keytool rule — Implement fell back to a debug-signed
    APK and Verify only caught it at the end of the pipeline."""
    issues = tv.validate_frontmatter(
        {"allowed_tools": "Read,Bash(flutter:*)"},
        body="android/app/macp-dashboard.keystore를 생성해 커밋한다",
    )
    assert _levels(issues, "allowed_tools") == [tv.WARN]


def test_keystore_spec_with_keytool_is_clean():
    issues = tv.validate_frontmatter(
        {"allowed_tools": "Read,Bash(flutter:*),Bash(keytool:*)"},
        body="android/app/macp-dashboard.keystore를 생성해 커밋한다",
    )
    assert issues == []


def test_body_without_keystore_mention_is_unaffected():
    assert (
        tv.validate_frontmatter({"allowed_tools": "Read,Bash(flutter:*)"}, body="평범한 스펙")
        == []
    )


def test_acceptance_leaning_on_ci_warns():
    """GitHub Actions is off in every project; "CI green" can never be met."""
    issues = tv.validate_frontmatter(
        {"acceptance_criteria": ["pytest 회귀 + `ruff check .` clean + CI green"]}
    )
    assert _levels(issues, "acceptance_criteria") == [tv.WARN]


def test_korean_suffixed_ci_and_github_actions_are_caught():
    """`CI가` has no ASCII word boundary after `CI` — a `\\bCI\\b` regex misses it."""
    issues = tv.validate_frontmatter(
        {
            "outcome": "3개 플랫폼 CI가 자동 테스트한다",
            "next_action": "GitHub Actions 워크플로를 dispatch 한다",
        }
    )
    assert _levels(issues, "outcome") == [tv.WARN]
    assert _levels(issues, "next_action") == [tv.WARN]


def test_ci_actions_opt_in_silences_the_warning():
    issues = tv.validate_frontmatter(
        {"ci": "actions", "acceptance_criteria": ["CI green on the PR"]}
    )
    assert issues == []


def test_local_gate_acceptance_is_clean():
    issues = tv.validate_frontmatter(
        {
            "acceptance_criteria": [
                "`./gradlew :shared:testDebugUnitTest :app:assembleDebug` green",
                "CIRCLE and DECIMAL words do not trip the matcher",
            ]
        }
    )
    assert issues == []


def test_unknown_ci_value_warns():
    issues = tv.validate_frontmatter({"ci": "github"})
    assert _levels(issues, "ci") == [tv.WARN]


def test_touches_glob_list_is_clean():
    """The right shape: a non-empty YAML list of path-glob strings."""
    assert tv.validate_frontmatter({"touches": ["a/**", "b/c.py"]}) == []


def test_missing_touches_is_clean():
    """touches is optional — absence is never an error (inverse of the trap:
    a task that declares none is simply always serialised)."""
    assert tv.validate_frontmatter({"touches": None}) == []
    assert _levels(tv.validate_frontmatter({}), "touches") == []


def test_bare_string_touches_is_an_error():
    """The inverse of the allowed_tools trap: there a list is wrong, here a
    bare string is — it declares no globs and silently forces serial dispatch."""
    issues = tv.validate_frontmatter({"touches": "a/**"})
    assert _levels(issues, "touches") == [tv.ERROR]


def test_empty_touches_list_is_an_error():
    issues = tv.validate_frontmatter({"touches": []})
    assert _levels(issues, "touches") == [tv.ERROR]


def test_non_string_touches_entry_is_an_error():
    issues = tv.validate_frontmatter({"touches": ["a/**", 3]})
    assert _levels(issues, "touches") == [tv.ERROR]


def test_blank_touches_entry_is_an_error():
    """A whitespace-only entry is as empty as a missing one."""
    issues = tv.validate_frontmatter({"touches": ["a/**", "   "]})
    assert _levels(issues, "touches") == [tv.ERROR]


def test_mapping_touches_is_an_error():
    issues = tv.validate_frontmatter({"touches": {"a": "b"}})
    assert _levels(issues, "touches") == [tv.ERROR]


REPO = "https://example.invalid/r.git"


def test_repo_auto_task_without_touches_warns():
    """A runnable repo task without touches is serialised against every sibling
    on that repo ("겹침 미판정 (touches 없음)") — warn at authoring time."""
    for mode in ("auto", "quick", "delegate", " Auto "):
        issues = tv.validate_frontmatter({"repo": REPO, "worker_mode": mode})
        assert _levels(issues, "touches") == [tv.WARN], mode
        assert len(issues) == 1, mode


def test_repo_auto_task_with_touches_is_clean():
    assert tv.validate_frontmatter(
        {"repo": REPO, "worker_mode": "auto", "touches": ["src/**"]}
    ) == []


def test_vault_task_without_repo_does_not_warn_about_touches():
    assert tv.validate_frontmatter({"worker_mode": "auto"}) == []
    for mode in ("blocked", "manual"):
        issues = tv.validate_frontmatter({"repo": REPO, "worker_mode": mode})
        assert _levels(issues, "touches") == [], mode


def test_contaminated_worker_mode_skips_touches_warn():
    """The inline-comment ERROR already names the cause; don't pile on."""
    issues = tv.validate_frontmatter({"repo": REPO, "worker_mode": "auto # x"})
    assert _levels(issues, "touches") == []
    assert _levels(issues, "worker_mode") == [tv.ERROR]


def test_wildcard_filename_touches_warns_with_static_prefix():
    """The dispatcher reads `a/b/Foo*Test.kt` as its static prefix `a/b`, so it
    overlaps every sibling under a/b — the message must say so."""
    issues = tv.validate_frontmatter({"touches": ["a/b/Foo*Test.kt"]})
    assert _levels(issues, "touches") == [tv.WARN]
    assert "`a/b`" in issues[0].message


def test_wildcard_filename_at_root_names_the_empty_prefix():
    issues = tv.validate_frontmatter({"touches": ["Foo*Test.kt"]})
    assert _levels(issues, "touches") == [tv.WARN]
    assert "빈 문자열" in issues[0].message


def test_directory_intent_touches_globs_are_clean():
    for globs in (["a/b/**"], ["a/b/*"], ["a/b/c.py"], ["a/b/**/"]):
        assert tv.validate_frontmatter({"touches": globs}) == [], globs


def test_not_before_time_values_are_clean(tmp_path):
    """All three not_before shapes validate with no issue, quoted or bare."""
    for value in ("2026-10-12", '"2026-10-12 17:00"', "2026-10-12 17:00",
                  "2026-10-12T17:00", "2026-10-12T17:00:30"):
        path = tmp_path / "t.md"
        path.write_text(f"---\nstatus: open\nnot_before: {value}\n---\n# t\n",
                        encoding="utf-8")
        assert tv.validate_file(path) == [], value


def test_broken_yaml_frontmatter_is_an_error_with_line(tmp_path):
    """A frontmatter that yaml.safe_load rejects is an ERROR naming the line.

    The lenient worker parser never raises, so before this the same file passed
    `cli validate` even though `load_dispatchable` refuses to dispatch it. The
    bad line (`repo: [unclosed`) is file line 3 → the message reports `line 3`.
    """
    path = tmp_path / "broken.md"
    path.write_text("---\nstatus: open\nrepo: [unclosed\n---\n# x\n",
                    encoding="utf-8")
    issues = tv.validate_file(path)
    assert _levels(issues, "frontmatter") == [tv.ERROR]
    assert "line 3" in issues[0].message


def test_broken_yaml_real_case_escape_is_an_error(tmp_path):
    """The 2026-10-19 shape: a backslash-escaped single quote inside a
    double-quoted scalar (invalid YAML) is flagged, not silently passed."""
    path = tmp_path / "real.md"
    path.write_text("---\nstatus: open\nnext_action: \"can\\'t parse\"\n---\n# x\n",
                    encoding="utf-8")
    issues = tv.validate_file(path)
    assert _levels(issues, "frontmatter") == [tv.ERROR]


def test_valid_frontmatter_has_no_parse_error(tmp_path):
    """Negative control: a well-formed file carries no frontmatter ERROR."""
    path = tmp_path / "ok.md"
    path.write_text("---\nstatus: open\nrepo: \"https://h/x/y.git\"\n---\n# x\n",
                    encoding="utf-8")
    assert _levels(tv.validate_file(path), "frontmatter") == []


def test_validate_dir_surfaces_broken_yaml_even_when_closed(tmp_path):
    """A YAML-broken file can't be read for status, so only_open must not hide
    it — it is always surfaced as a frontmatter ERROR."""
    (tmp_path / "broken.md").write_text(
        "---\nstatus: done\nrepo: [unclosed\n---\n# x\n", encoding="utf-8")
    found = tv.validate_dir(tmp_path, only_open=True)
    assert "broken" in found
    assert _levels(found["broken"], "frontmatter") == [tv.ERROR]
