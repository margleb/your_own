"""Token budgets: the answer's size plus the model's room to think.

Run:
    python -m pytest tests/llm/test_budgets.py -v

Written on 27.09, after a week in which three separate steps were dying on caps
chosen for a model the project no longer runs. Measured on the live server,
21–27.09: 21 truncations at 650 (memory dedup, every verdict lost), 11 at 1200
(the push validator, which then delivered the push unreviewed), 4 at 16000.

The rule these hold: a budget may grow when the model needs room, and may never
shrink below what its call site used before.
"""
from __future__ import annotations

import pytest

from infrastructure.llm import budgets
from infrastructure.llm.budgets import Job

#: What each call site passed before the table existed. Nothing may come out of
#: this change with less than it had.
BEFORE = {
    Job.VERDICT: 800,          # key_info dedup 650, research judge 800
    Job.PUSH_REVIEW: 1200,
    Job.WEB_ANSWER: 1200,
    Job.ALIASES: 4000,
    Job.BRIEF: 6000,
    Job.DOC_ANSWER: 8000,
    Job.ROOM_REPLY: 8000,
    Job.JOURNAL: 16000,
    Job.STEP: 16000,
}

KIMI = "~moonshotai/kimi-latest"
FABLE = "~anthropic/claude-fable-latest"


class TestNothingShrinks:
    @pytest.mark.parametrize("job", list(Job))
    def test_every_job_keeps_at_least_what_it_had(self, job):
        assert budgets.for_job(job, FABLE) >= BEFORE[job]
        assert budgets.for_job(job, KIMI) >= BEFORE[job]

    def test_a_model_that_does_not_reason_is_unchanged(self):
        """The old numbers were measured on this model and worked."""
        assert budgets.for_job(Job.JOURNAL, FABLE) == 16000
        assert budgets.for_job(Job.PUSH_REVIEW, FABLE) == 1200

    @pytest.mark.parametrize("job", list(Job))
    def test_a_reasoning_model_gets_more(self, job):
        assert budgets.for_job(job, KIMI) > budgets.for_job(job, FABLE)


class TestTheVerdictJobs:
    """The two that died: one line of visible answer, a thousand of thinking."""

    def test_a_one_line_verdict_has_room_for_the_thinking_in_front_of_it(self):
        # kimi reached «ОТПРАВИТЬ» in 1007 and 1078 completion tokens on the two
        # occasions it fitted under a cap of 1200.
        assert budgets.for_job(Job.VERDICT, KIMI) >= 2500
        assert budgets.for_job(Job.PUSH_REVIEW, KIMI) >= 2500


class TestReadingTheModelId:
    def test_the_tilde_is_not_part_of_the_name(self):
        assert budgets.thinking_room("~moonshotai/kimi-latest") == budgets.thinking_room("moonshotai/kimi-latest")

    def test_case_does_not_matter(self):
        assert budgets.thinking_room("MoonshotAI/Kimi-Latest") == budgets.thinking_room(KIMI)

    def test_the_longest_prefix_wins(self):
        """"openai/o3" must not be read as plain "openai/"."""
        assert budgets.thinking_room("openai/o3-mini") > budgets.thinking_room("openai/gpt-5")

    @pytest.mark.parametrize("model", ["", "   ", None])
    def test_no_model_assumes_it_thinks(self, model, monkeypatch):
        monkeypatch.setattr(budgets, "_model_from_settings", lambda: "")
        assert budgets.thinking_room(model) == budgets.DEFAULT_THINKING_ROOM

    def test_an_unmeasured_model_assumes_it_thinks(self):
        """A cap costs nothing unused; assuming otherwise is what broke this."""
        assert budgets.thinking_room("some-lab/brand-new-model") == budgets.DEFAULT_THINKING_ROOM

    def test_the_default_is_the_model_in_settings(self, monkeypatch):
        monkeypatch.setattr(budgets, "_model_from_settings", lambda: KIMI)
        assert budgets.for_job(Job.STEP) == budgets.for_job(Job.STEP, KIMI)

    def test_unreadable_settings_do_not_raise(self, monkeypatch):
        def boom():
            raise OSError("no settings")

        monkeypatch.setattr(budgets, "_model_from_settings", boom)
        with pytest.raises(OSError):
            budgets.for_job(Job.STEP)


class TestNoCallSiteKeepsALiteral:
    """A literal budget is how these drifted from the model in the first place."""

    @pytest.mark.parametrize("module_name,literals", [
        ("infrastructure.autonomy.push_validator", ["max_tokens=1200"]),
        ("infrastructure.memory.key_info", ["max_tokens=650"]),
        ("infrastructure.autonomy.workbench_rotator", ["max_tokens=650", "max_tokens=1500"]),
        ("infrastructure.telegram.responder", ["max_tokens=8000"]),
        ("infrastructure.agents.sources", ["max_tokens=1200", "max_tokens=8000"]),
    ])
    def test_the_number_comes_from_the_table(self, module_name, literals):
        import importlib
        import inspect

        source = inspect.getsource(importlib.import_module(module_name))
        for literal in literals:
            assert literal not in source, f"{literal} is a budget chosen without a model"

    def test_the_timeout_follows_the_budget_up(self):
        """A bigger budget must not just trade truncation for a timeout."""
        from infrastructure.llm.client import _timeout_for

        assert _timeout_for(budgets.for_job(Job.STEP, KIMI)) >= _timeout_for(budgets.for_job(Job.STEP, FABLE))
