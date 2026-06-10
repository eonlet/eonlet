"""TaskScheduler core — runnable selection + post-run classification (ADR-0007 M2)."""

from __future__ import annotations

from eonlet.runtime.events import Event, task_created, task_transitioned
from eonlet.tasks import (
    TaskForest,
    classify_post_run,
    creation_guard_error,
    fold_tasks,
    next_runnable,
    preemptor,
)
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


def test_siblings_run_in_creation_order() -> None:
    # ADR-0008 §2: no scheduling within a tree — subtasks run depth-first in
    # creation order, *not* by priority (priority schedules only at the root).
    forest = _forest(
        task_created(id="root", content="project"),
        task_created(id="a", content="a", parent_id="root", priority=1),
        task_created(id="b", content="b", parent_id="root", priority=8),
        task_transitioned(id="root", from_state="pending", to_state="blocked"),
    )
    assert _id(forest) == "a"  # created first, despite b's higher priority


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


# ── creation guards (M4) ─────────────────────────────────────────────────────


def test_depth_helper() -> None:
    forest = _forest(
        task_created(id="a", content="a"),
        task_created(id="b", content="b", parent_id="a"),
        task_created(id="c", content="c", parent_id="b"),
    )
    assert forest.depth("a") == 1
    assert forest.depth("b") == 2
    assert forest.depth("c") == 3


def test_guard_depth() -> None:
    forest = _forest(
        task_created(id="a", content="a"),
        task_created(id="b", content="b", parent_id="a"),
    )
    # under b → depth 3; cap 2 → rejected. under a → depth 2 → ok.
    assert creation_guard_error(forest, "b", max_depth=2, max_fanout=0) is not None
    assert creation_guard_error(forest, "a", max_depth=2, max_fanout=0) is None
    # A new root is never depth-bounded, and 0 disables the cap.
    assert creation_guard_error(forest, None, max_depth=2, max_fanout=0) is None
    assert creation_guard_error(forest, "b", max_depth=0, max_fanout=0) is None


def test_guard_fanout() -> None:
    forest = _forest(
        task_created(id="p", content="p"),
        task_created(id="c1", content="c1", parent_id="p"),
        task_created(id="c2", content="c2", parent_id="p"),
    )
    assert creation_guard_error(forest, "p", max_depth=0, max_fanout=2) is not None  # full
    assert creation_guard_error(forest, "p", max_depth=0, max_fanout=3) is None
    assert creation_guard_error(forest, "p", max_depth=0, max_fanout=0) is None  # unlimited


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


def test_preemptor_compares_root_priority_not_node() -> None:
    # ADR-0008 §2/§3: scheduling is by ROOT priority. A low-priority root tree
    # does not preempt, even when the running node deep inside the current tree
    # carries a high priority.
    forest = _forest(
        task_created(id="r1", content="r1", priority=5),
        task_created(id="r1c", content="r1 child", parent_id="r1", priority=9),
        task_transitioned(id="r1", from_state="pending", to_state="blocked"),
        task_transitioned(id="r1c", from_state="pending", to_state="active"),
        task_created(id="r2", content="r2", priority=3),  # root p3 < r1 root p5
    )
    cur = forest.get("r1c")
    assert cur is not None
    assert preemptor(forest, cur) is None


def test_preemptor_higher_root_tree_preempts_from_within() -> None:
    # A strictly-higher-priority *other* root tree preempts; the contender is the
    # runnable node within that tree (here the higher tree's pending child).
    forest = _forest(
        task_created(id="r1", content="r1", priority=2),
        task_created(id="r1c", content="r1 child", parent_id="r1"),
        task_transitioned(id="r1", from_state="pending", to_state="blocked"),
        task_transitioned(id="r1c", from_state="pending", to_state="active"),
        task_created(id="r2", content="r2", priority=5),
        task_created(id="r2c", content="r2 child", parent_id="r2"),
        task_transitioned(id="r2", from_state="pending", to_state="blocked"),
    )
    cur = forest.get("r1c")
    assert cur is not None
    p = preemptor(forest, cur)
    assert p is not None and p.id == "r2c"


def test_preemptor_skips_trigger_origin_root() -> None:
    # ADR-0008 §4: a trigger-origin (scheduled/autonomous) tree never preempts
    # foreground work, even at higher priority.
    forest = _forest(
        task_created(id="cur", content="current", priority=2),
        task_transitioned(id="cur", from_state="pending", to_state="active"),
        task_created(id="trig", content="scheduled", priority=9, origin="trigger"),
    )
    cur = forest.get("cur")
    assert cur is not None
    assert preemptor(forest, cur) is None


def test_preemptor_prefers_eligible_root_over_skipped_trigger() -> None:
    # The top contender is a trigger tree (skipped); the next-highest eligible
    # root (user-origin) still preempts.
    forest = _forest(
        task_created(id="cur", content="current", priority=2),
        task_transitioned(id="cur", from_state="pending", to_state="active"),
        task_created(id="trig", content="scheduled", priority=9, origin="trigger"),
        task_created(id="usr", content="user task", priority=5, origin="user"),
    )
    cur = forest.get("cur")
    assert cur is not None
    p = preemptor(forest, cur)
    assert p is not None and p.id == "usr"
