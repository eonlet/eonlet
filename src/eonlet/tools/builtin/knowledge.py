"""knowledge: the curated knowledge axis (ADR-0005).

A single action-style tool for the durable, agent-curated knowledge tree.
Replaces the old ``remember`` + ``note`` write surface: there is now one way
to write something the agent should keep — ``knowledge.write`` — and the map
(``index.md``, injected every call) stays in sync automatically.

Actions:
- ``open``  (path)                      read a file's body
- ``list``                              show the curated map
- ``write`` (path, content, index_line) create/replace a file's full body
- ``edit``  (path, old_string, new_string) string-replace inside a file
- ``delete``(path)                      remove a file + its map entry
- ``move``  (path, new_path)            rename/relocate a file
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...errors import KnowledgeError, KnowledgePathError
from ...memory.knowledge import KnowledgeStore
from ...runtime.events import kb_deleted, kb_moved, kb_written
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class KnowledgeArgs(BaseModel):
    action: Literal["open", "list", "write", "edit", "delete", "move"]
    path: str | None = Field(
        default=None,
        description="Relative path under knowledge/, e.g. 'rules/testing.md'.",
    )
    content: str | None = Field(
        default=None,
        description="Full file body for action='write'.",
    )
    index_line: str | None = Field(
        default=None,
        description=(
            "One-line relevance hook shown in the always-injected map "
            "(for 'write'/'move'). E.g. 'never mock the DB in tests'."
        ),
    )
    old_string: str | None = Field(
        default=None,
        description="Exact text to replace for action='edit' (must be unique in the file).",
    )
    new_string: str | None = Field(
        default=None,
        description="Replacement text for action='edit'.",
    )
    new_path: str | None = Field(
        default=None,
        description="Destination relative path for action='move'.",
    )


@tool
class KnowledgeTool:
    name = "knowledge"
    description = (
        "Curated, durable knowledge base — never auto-deleted. The map "
        "(index.md) is always in your context; open file bodies on demand. "
        "Actions: 'open' (path), 'list', 'write' (path + content + index_line), "
        "'edit' (path + old_string + new_string), 'delete' (path), "
        "'move' (path + new_path)."
    )
    input_schema = KnowledgeArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
        store = KnowledgeStore(ctx.memory_dir)

        try:
            if args.action == "open":
                return await self._open(store, args)
            if args.action == "list":
                return await self._list(store)
            if args.action == "write":
                return await self._write(store, args, ctx)
            if args.action == "edit":
                return await self._edit(store, args, ctx)
            if args.action == "delete":
                return await self._delete(store, args, ctx)
            if args.action == "move":
                return await self._move(store, args, ctx)
        except KnowledgePathError as e:
            return ToolResult(content=f"knowledge {args.action}: {e}", is_error=True)
        except KnowledgeError as e:
            return ToolResult(content=f"knowledge {args.action}: {e}", is_error=True)

        # Unreachable thanks to Literal[...].
        return ToolResult(content=f"knowledge: unknown action {args.action!r}", is_error=True)

    # ── read ─────────────────────────────────────────────────────────────
    @staticmethod
    async def _open(store: KnowledgeStore, args: KnowledgeArgs) -> ToolResult:
        if not args.path:
            return ToolResult(content="knowledge open: 'path' is required", is_error=True)
        body = await store.open(args.path)
        if body is None:
            return ToolResult(content=f"no such knowledge file: {args.path}", is_error=True)
        return ToolResult(content=body, structured_output={"path": args.path, "size": len(body)})

    @staticmethod
    async def _list(store: KnowledgeStore) -> ToolResult:
        entries = await store.list_entries()
        if not entries:
            return ToolResult(content="(knowledge base is empty)")
        return ToolResult(
            content="\n".join(e.render() for e in entries),
            structured_output={"count": len(entries)},
        )

    # ── write ────────────────────────────────────────────────────────────
    @staticmethod
    async def _write(store: KnowledgeStore, args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
        if not args.path or args.content is None:
            return ToolResult(
                content="knowledge write: 'path' and 'content' are required", is_error=True
            )
        rel = await store.write(path=args.path, content=args.content, index_line=args.index_line)
        if ctx.record_event is not None:
            await ctx.record_event(
                kb_written(path=rel, size=len(args.content), action="write", content=args.content)
            )
        out = f"wrote {rel}" + _index_budget_warning(store, ctx)
        return ToolResult(content=out, structured_output={"path": rel})

    @staticmethod
    async def _edit(store: KnowledgeStore, args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
        if not args.path or args.old_string is None or args.new_string is None:
            return ToolResult(
                content="knowledge edit: 'path', 'old_string', 'new_string' are required",
                is_error=True,
            )
        rel = await store.edit(
            path=args.path, old_string=args.old_string, new_string=args.new_string
        )
        if ctx.record_event is not None:
            # The event carries the complete post-edit body so the log can
            # reconstruct the file (knowledge is never auto-deleted).
            body = await store.open(rel) or ""
            await ctx.record_event(
                kb_written(path=rel, size=len(body), action="edit", content=body)
            )
        return ToolResult(content=f"edited {rel}", structured_output={"path": rel})

    # ── delete / move ──────────────────────────────────────────────────
    @staticmethod
    async def _delete(store: KnowledgeStore, args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
        if not args.path:
            return ToolResult(content="knowledge delete: 'path' is required", is_error=True)
        existed = await store.delete(path=args.path)
        if not existed:
            return ToolResult(content=f"no such knowledge file: {args.path}", is_error=True)
        if ctx.record_event is not None:
            await ctx.record_event(kb_deleted(path=args.path))
        return ToolResult(content=f"deleted {args.path}")

    @staticmethod
    async def _move(store: KnowledgeStore, args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
        if not args.path or not args.new_path:
            return ToolResult(
                content="knowledge move: 'path' and 'new_path' are required", is_error=True
            )
        src_rel, dst_rel = await store.move(
            src=args.path, dst=args.new_path, index_line=args.index_line
        )
        if ctx.record_event is not None:
            await ctx.record_event(kb_moved(src=src_rel, dst=dst_rel))
        return ToolResult(
            content=f"moved {src_rel} → {dst_rel}" + _index_budget_warning(store, ctx),
            structured_output={"src": src_rel, "dst": dst_rel},
        )


def _index_budget_warning(store: KnowledgeStore, ctx: ToolContext) -> str:
    """A visible nudge when the always-injected index outgrows its budget.

    The agent is the only entity that can prune its own index, and a silent
    log warning never reaches it — so the signal has to ride the ToolResult.
    Empty string when within budget or when no runtime config is reachable.
    """
    runtime = (ctx.extra or {}).get("runtime")
    if runtime is None:
        return ""
    from ...memory.tokens import estimate

    limit = runtime.definition.config.memory.knowledge.index_max_tokens
    tokens = estimate(store.index_text())
    if tokens <= limit:
        return ""
    return (
        f"\nWARNING: the knowledge index is ~{tokens} tokens (budget {limit}). "
        "It is injected into every call — prune or merge index lines."
    )
