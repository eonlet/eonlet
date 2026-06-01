"""TaskForest — the event-sourced task projection (ADR-0007)."""

from __future__ import annotations

from eonlet.runtime.events import (
    Event,
    EventKind,
    task_checkpointed,
    task_created,
    task_deleted,
    task_transitioned,
    task_updated,
)
from eonlet.tasks import can_transition, fold_tasks, reduce_task
from eonlet.tasks.forest import TaskForest


def _stamp(events: list[Event]) -> list[Event]:
    """Assign monotonic ids like the store does on append."""
    return [e.model_copy(update={"id": i + 1}) for i, e in enumerate(events)]


def test_create_and_list() -> None:
    forest = fold_tasks(_stamp([task_created(id="t1", content="do x")]))
    assert len(forest) == 1
    t = forest.get("t1")
    assert t is not None and t.content == "do x" and t.status == "pending"
    assert t.created_at  # derived from the event ts
    assert forest.is_leaf("t1")


def test_tree_parent_child() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="root", content="project"),
                task_created(id="c1", content="part 1", parent_id="root"),
                task_created(id="c2", content="part 2", parent_id="root"),
            ]
        )
    )
    assert [t.id for t in forest.roots()] == ["root"]
    assert [t.id for t in forest.children("root")] == ["c1", "c2"]
    assert not forest.is_leaf("root")
    # DFS pre-order with depth.
    assert [(t.id, d) for t, d in forest.dfs()] == [("root", 0), ("c1", 1), ("c2", 1)]


def test_roots_priority_ordered() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="lo", content="low", priority=1),
                task_created(id="hi", content="high", priority=9),
            ]
        )
    )
    assert [t.id for t in forest.roots()] == ["hi", "lo"]


def test_pending_leaves_excludes_parents_and_done() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="root", content="project"),
                task_created(id="c1", content="leaf 1", parent_id="root"),
                task_created(id="c2", content="leaf 2", parent_id="root", priority=5),
                task_transitioned(id="c1", from_state="pending", to_state="done"),
            ]
        )
    )
    leaves = forest.pending_leaves()
    # root has children → not a leaf; c1 is done; only c2 remains, surfaced.
    assert [t.id for t in leaves] == ["c2"]


def test_transition_done_sets_done_at() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="t", content="x"),
                task_transitioned(id="t", from_state="pending", to_state="done"),
            ]
        )
    )
    t = forest.get("t")
    assert t is not None and t.status == "done" and t.done_at is not None


def test_illegal_transition_dropped_not_fatal() -> None:
    # done is terminal; a later transition off it must be ignored, not crash.
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="t", content="x"),
                task_transitioned(id="t", from_state="pending", to_state="done"),
                task_transitioned(id="t", from_state="done", to_state="active"),
            ]
        )
    )
    t = forest.get("t")
    assert t is not None and t.status == "done"  # stuck terminal


def test_update_applies_only_provided_fields() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="t", content="old", priority=0, tags=["a"]),
                task_updated(id="t", content="new", priority=7),
            ]
        )
    )
    t = forest.get("t")
    assert t is not None
    assert t.content == "new" and t.priority == 7
    assert t.tags == ["a"]  # untouched


def test_checkpoint_sets_progress_summary() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="t", content="x"),
                task_transitioned(id="t", from_state="pending", to_state="active"),
                task_checkpointed(id="t", progress_summary="did half"),
                task_transitioned(id="t", from_state="active", to_state="suspended"),
            ]
        )
    )
    t = forest.get("t")
    assert t is not None and t.progress_summary == "did half" and t.status == "suspended"


def test_delete_orphans_children_to_roots() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="root", content="r"),
                task_created(id="c1", content="c", parent_id="root"),
                task_deleted(id="root"),
            ]
        )
    )
    assert forest.get("root") is None
    # Orphan surfaces as a root rather than disappearing.
    assert [t.id for t in forest.roots()] == ["c1"]


def test_missing_node_events_ignored() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_updated(id="ghost", content="x"),
                task_transitioned(id="ghost", from_state="pending", to_state="done"),
                task_checkpointed(id="ghost", progress_summary="y"),
                task_deleted(id="ghost"),
            ]
        )
    )
    assert len(forest) == 0  # nothing created, nothing crashed


def test_duplicate_create_ignored() -> None:
    forest = fold_tasks(
        _stamp(
            [
                task_created(id="t", content="first"),
                task_created(id="t", content="second"),
            ]
        )
    )
    t = forest.get("t")
    assert t is not None and t.content == "first" and len(forest) == 1


def test_replay_is_deterministic() -> None:
    events = _stamp(
        [
            task_created(id="root", content="r"),
            task_created(id="c1", content="c1", parent_id="root", priority=2),
            task_transitioned(id="c1", from_state="pending", to_state="active"),
            task_checkpointed(id="c1", progress_summary="midway"),
            task_transitioned(id="c1", from_state="active", to_state="suspended"),
        ]
    )
    # Folding twice (simulating a worker restart) yields identical state.
    f1 = fold_tasks(events)
    f2 = fold_tasks(events)
    assert [t.to_dict() for t in f1.all_tasks()] == [t.to_dict() for t in f2.all_tasks()]
    # And incremental reduce matches a fresh fold.
    incr = TaskForest()
    for e in events:
        reduce_task(incr, e)
    assert [t.to_dict() for t in incr.all_tasks()] == [t.to_dict() for t in f1.all_tasks()]


def test_non_task_events_are_noops() -> None:
    forest = TaskForest()
    reduce_task(forest, Event(id=1, kind=EventKind.USER_MESSAGE, payload={"content": "hi"}))
    assert len(forest) == 0


def test_can_transition_rules() -> None:
    assert can_transition("pending", "active")
    assert can_transition("active", "suspended")
    assert can_transition("suspended", "active")
    assert can_transition("done", "done")  # no-op allowed
    assert not can_transition("done", "active")
    assert not can_transition("cancelled", "pending")
