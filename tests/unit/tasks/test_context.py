"""build_task_prompt — per-task framing for scheduler runs (ADR-0007 M2)."""

from __future__ import annotations

from eonlet.runtime.events import Event, task_created, task_transitioned
from eonlet.tasks import TaskForest, fold_tasks
from eonlet.tasks.context import build_task_prompt


def _forest(*events: Event) -> TaskForest:
    return fold_tasks([e.model_copy(update={"id": i + 1}) for i, e in enumerate(events)])


def test_leaf_prompt_has_goal_and_howto() -> None:
    forest = _forest(task_created(id="t", content="research X", goal="produce an X brief"))
    out = build_task_prompt(forest, "t")
    assert out.startswith('<task id="t">')
    assert "produce an X brief" in out
    assert 'task(action="done")' in out
    assert out.rstrip().endswith("</task>")


def test_parent_chain_included() -> None:
    forest = _forest(
        task_created(id="root", content="ship feature", goal="ship the feature"),
        task_created(id="mid", content="backend", goal="build backend", parent_id="root"),
        task_created(id="leaf", content="schema", goal="design schema", parent_id="mid"),
    )
    out = build_task_prompt(forest, "leaf")
    assert "Parent context:" in out
    assert "ship the feature" in out and "build backend" in out
    # Order is root → nearest parent.
    assert out.index("ship the feature") < out.index("build backend")


def test_progress_summary_shown_on_resume() -> None:
    forest = _forest(
        task_created(id="t", content="x", goal="do x"),
        task_transitioned(id="t", from_state="pending", to_state="active"),
    )
    forest.get("t").progress_summary = "got halfway"  # type: ignore[union-attr]
    out = build_task_prompt(forest, "t")
    assert "Progress so far: got halfway" in out


def test_synthesis_prompt_lists_child_results() -> None:
    forest = _forest(
        task_created(id="root", content="report", goal="write the report"),
        task_created(id="c1", content="part 1", goal="gather data", parent_id="root"),
        task_created(id="c2", content="part 2", goal="analyze", parent_id="root"),
        task_transitioned(id="root", from_state="pending", to_state="blocked"),
        task_transitioned(id="c1", from_state="pending", to_state="done", result="found 3 sources"),
        task_transitioned(id="c2", from_state="pending", to_state="done", result="trend is up"),
    )
    out = build_task_prompt(forest, "root")
    assert "Subtask results" in out
    assert "found 3 sources" in out and "trend is up" in out


def test_missing_task_is_graceful() -> None:
    out = build_task_prompt(TaskForest(), "ghost")
    assert "ghost" in out and "no longer exists" in out
