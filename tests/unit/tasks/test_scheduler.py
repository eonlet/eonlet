"""TaskScheduler core — runnable selection + post-run classification (ADR-0007 M2)."""

from __future__ import annotations

from eonlet.runtime.events import Event, task_created, task_transitioned
from eonlet.tasks import TaskForest, classify_post_run, fold_tasks, next_runnable, preemptor
from eonlet.tasks.scheduler import PostRun, synthesis_ready


def _forest(*events: Event) -> TaskForest:
    stamped = [e.model_copy(update={"id": i + 1}) for i, e in enumerate(events)]
    return fold_tasks(stamped)


def _id(forest: TaskForest) -> str | None:
    t = next_runnable(forest)
    return t.id if t else None


def test_empty_forest_idle() -> None:
    assert next_runnable(TaskForest()) is None


def test_single_pending_leaf_is_runnable() -> None:
    forest = _forest(task_created(id="t", content="x"))
    assert _id(forest) == "t"


def test_all_terminal_is_idle() -> None:
    forest = _forest(
        task_created(id="t", content="x"),
        task_transitioned(id="t", from_state="pending", to_state="done"),
    )
    assert next_runnable(forest) is None


def test_highest_priority_root_first() -> None:
    forest = _forest(
        task_created(id="lo", content="low", priority=1),
        task_created(id="hi", content="high", priority=9),
        task_created(id="mid", content="mid", priority=5),
    )
    assert _id(forest) == "hi"


def test_descends_into_children_before_parent() -> None:
    # Parent blocked, waiting on a pending child → the child runs, not the parent.
    forest = _forest(
        task_created(id="root", content="project"),
        task_created(id="c1", content="leaf", parent_id="root"),
        task_transitioned(id="root", from_state="pending", to_state="blocked"),
    )
    assert _id(forest) == "c1"


def test_highest_priority_sibling_first() -> None:
    forest = _forest(
        task_created(id="root", content="project"),
        task_created(id="a", content="a", parent_id="root", priority=1),
        task_created(id="b", content="b", parent_id="root", priority=8),
        task_transitioned(id="root", from_state="pending", to_state="blocked"),
    )
    assert _id(forest) == "b"


def test_blocked_parent_synthesizes_when_children_terminal() -> None:
    forest = _forest(
        task_created(id="root", content="project"),
        task_created(id="c1", content="c1", parent_id="root"),
        task_created(id="c2", content="c2", parent_id="root"),
        task_transitioned(id="root", from_state="pending", to_state="blocked"),
        task_transitioned(id="c1", from_state="pending", to_state="done"),
        task_transitioned(id="c2", from_state="pending", to_state="cancelled"),
    )
    # Both children terminal → the parent becomes runnable for its synthesis turn.
    assert _id(forest) == "root"
    assert synthesis_ready(forest, "root") is True


def test_blocked_parent_not_ready_while_a_child_runs() -> None:
    forest = _forest(
        task_created(id="root", content="project"),
        task_created(id="c1", content="c1", parent_id="root"),
        task_created(id="c2", content="c2", parent_id="root"),
        task_transitioned(id="root", from_state="pending", to_state="blocked"),
        task_transitioned(id="c1", from_state="pending", to_state="done"),
        # c2 still pending → c2 runs; root not yet ready.
    )
    assert _id(forest) == "c2"
    assert synthesis_ready(forest, "root") is False


def test_suspended_leaf_is_not_picked_up() -> None:
    forest = _forest(
        task_created(id="t", content="x"),
        task_transitioned(id="t", from_state="pending", to_state="active"),
        task_transitioned(id="t", from_state="active", to_state="suspended"),
    )
    assert next_runnable(forest) is None


def test_priority_across_trees_then_within() -> None:
    forest = _forest(
        task_created(id="r1", content="r1", priority=2),
        task_created(id="r1a", content="r1a", parent_id="r1", priority=9),
        task_transitioned(id="r1", from_state="pending", to_state="blocked"),
        task_created(id="r2", content="r2", priority=5),  # higher-priority root tree
    )
    # r2 tree (priority 5) outranks r1 tree (priority 2), even though r1a is p9.
    assert _id(forest) == "r2"


def test_classify_post_run_done() -> None:
    forest = _forest(
        task_created(id="t", content="x"),
        task_transitioned(id="t", from_state="pending", to_state="active"),
        task_transitioned(id="t", from_state="active", to_state="done"),
    )
    assert classify_post_run(forest, "t") is PostRun.DONE


def test_classify_post_run_decomposed() -> None:
    forest = _forest(
        task_created(id="t", content="x"),
        task_transitioned(id="t", from_state="pending", to_state="active"),
        task_created(id="c", content="child", parent_id="t"),
    )
    assert classify_post_run(forest, "t") is PostRun.DECOMPOSED


def test_classify_post_run_yielded() -> None:
    forest = _forest(
        task_created(id="t", content="x"),
        task_transitioned(id="t", from_state="pending", to_state="active"),
    )
    # Ran (active), no children, not terminal → yielded.
    assert classify_post_run(forest, "t") is PostRun.YIELDED


def test_classify_post_run_gone() -> None:
    forest = TaskForest()
    assert classify_post_run(forest, "ghost") is PostRun.GONE


# ── preemption (M3) ──────────────────────────────────────────────────────────


def test_next_runnable_excludes_subtree() -> None:
    forest = _forest(
        task_created(id="a", content="a", priority=5),
        task_created(id="a1", content="a1", parent_id="a", priority=9),
        task_transitioned(id="a", from_state="pending", to_state="active"),
        task_created(id="b", content="b", priority=3),
    )
    # Excluding a's subtree (a + a1), the only other runnable is b.
    got = next_runnable(forest, exclude_id="a")
    assert got is not None and got.id == "b"


def test_preemptor_returns_strictly_higher_priority() -> None:
    forest = _forest(
        task_created(id="cur", content="current", priority=2),
        task_transitioned(id="cur", from_state="pending", to_state="active"),
        task_created(id="hot", content="urgent", priority=9),
    )
    cur = forest.get("cur")
    assert cur is not None
    p = preemptor(forest, cur)
    assert p is not None and p.id == "hot"


def test_preemptor_ignores_equal_or_lower_priority() -> None:
    forest = _forest(
        task_created(id="cur", content="current", priority=5),
        task_transitioned(id="cur", from_state="pending", to_state="active"),
        task_created(id="eq", content="equal", priority=5),
        task_created(id="lo", content="lower", priority=1),
    )
    cur = forest.get("cur")
    assert cur is not None
    assert preemptor(forest, cur) is None  # neither equal nor lower preempts


def test_preemptor_ignores_own_subtask() -> None:
    forest = _forest(
        task_created(id="cur", content="current", priority=2),
        task_transitioned(id="cur", from_state="pending", to_state="active"),
        # A high-priority *subtask* of the running task must not preempt it.
        task_created(id="sub", content="subtask", parent_id="cur", priority=9),
    )
    cur = forest.get("cur")
    assert cur is not None
    assert preemptor(forest, cur) is None
