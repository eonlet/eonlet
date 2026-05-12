"""File-related builtin tools: file_read, file_write, file_edit, glob, grep."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

# ── file_read ────────────────────────────────────────────────────────────────


class FileReadArgs(BaseModel):
    path: str = Field(description="Absolute or workspace-relative path.")
    offset: int = Field(default=0, ge=0, description="Skip this many lines.")
    limit: int = Field(default=2000, ge=1, le=20000, description="Max lines to return.")


@tool
class FileReadTool:
    name = "file_read"
    description = (
        "Read a file's contents. Paginated by line count. "
        "Relative paths anchor to your workspace (same root as bash's cwd) — "
        "use 'foo.txt', not 'workspace/foo.txt'."
    )
    input_schema = FileReadArgs
    annotations = ToolAnnotations(read_only=True)

    async def __call__(self, args: FileReadArgs, ctx: ToolContext) -> ToolResult:
        try:
            p = _resolve(args.path, ctx)
        except _WorkspacePrefixError as e:
            return ToolResult(content=str(e), is_error=True)
        if not p.exists():
            return ToolResult(content=f"not found: {p}", is_error=True)
        if not p.is_file():
            return ToolResult(content=f"not a file: {p}", is_error=True)
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            return ToolResult(content=f"read failed: {e}", is_error=True)
        total = len(lines)
        sliced = lines[args.offset : args.offset + args.limit]
        body = "\n".join(f"{args.offset + i + 1}\t{line}" for i, line in enumerate(sliced))
        has_more = args.offset + args.limit < total
        return ToolResult(
            content=body,
            structured_output={"total_lines": total, "has_more": has_more},
        )


# ── file_write ───────────────────────────────────────────────────────────────


class FileWriteArgs(BaseModel):
    path: str
    content: str
    mode: str = Field(default="overwrite", pattern="^(overwrite|append)$")


@tool
class FileWriteTool:
    name = "file_write"
    description = (
        "Write or append a file. Modes: overwrite (default), append. "
        "Relative paths anchor to your workspace (same root as bash's cwd) — "
        "use 'foo.txt', not 'workspace/foo.txt'."
    )
    input_schema = FileWriteArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: FileWriteArgs, ctx: ToolContext) -> ToolResult:
        try:
            p = _resolve(args.path, ctx)
        except _WorkspacePrefixError as e:
            return ToolResult(content=str(e), is_error=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if args.mode == "append":
                with p.open("a", encoding="utf-8") as f:
                    n = f.write(args.content)
            else:
                p.write_text(args.content, encoding="utf-8")
                n = len(args.content.encode("utf-8"))
        except Exception as e:
            return ToolResult(content=f"write failed: {e}", is_error=True)
        return ToolResult(
            content=f"wrote {n} bytes to {p}",
            structured_output={"bytes_written": n, "path": str(p)},
        )


# ── file_edit ────────────────────────────────────────────────────────────────


class FileEditArgs(BaseModel):
    path: str
    search: str
    replace: str
    expected_count: int = Field(default=1, ge=1)


@tool
class FileEditTool:
    name = "file_edit"
    description = (
        "Exact SEARCH/REPLACE edit. Errors if `search` doesn't appear exactly "
        "`expected_count` times — preventing accidental over-replacement."
    )
    input_schema = FileEditArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: FileEditArgs, ctx: ToolContext) -> ToolResult:
        try:
            p = _resolve(args.path, ctx)
        except _WorkspacePrefixError as e:
            return ToolResult(content=str(e), is_error=True)
        if not p.exists():
            return ToolResult(content=f"not found: {p}", is_error=True)
        text = p.read_text(encoding="utf-8")
        count = text.count(args.search)
        if count != args.expected_count:
            return ToolResult(
                content=f"search appears {count} times, expected {args.expected_count}",
                is_error=True,
            )
        new = text.replace(args.search, args.replace)
        p.write_text(new, encoding="utf-8")
        return ToolResult(content=f"replaced {count} occurrence(s) in {p}")


# ── glob ─────────────────────────────────────────────────────────────────────


class GlobArgs(BaseModel):
    pattern: str = Field(description='e.g. "**/*.py"')
    cwd: str | None = None


@tool
class GlobTool:
    name = "glob"
    description = "Find files by glob pattern, relative to workspace by default."
    input_schema = GlobArgs
    annotations = ToolAnnotations(read_only=True)

    async def __call__(self, args: GlobArgs, ctx: ToolContext) -> ToolResult:
        try:
            base = _resolve(args.cwd, ctx) if args.cwd else ctx.workspace
        except _WorkspacePrefixError as e:
            return ToolResult(content=str(e), is_error=True)
        if not base.exists():
            return ToolResult(content="no matches", structured_output={"paths": []})
        matches = sorted(str(p) for p in base.glob(args.pattern) if p.is_file())[:500]
        return ToolResult(
            content="\n".join(matches) if matches else "no matches",
            structured_output={"paths": matches},
        )


# ── grep ─────────────────────────────────────────────────────────────────────


class GrepArgs(BaseModel):
    pattern: str = Field(description="Python regex.")
    path: str | None = None
    include: str = Field(default="*", description="Glob filter for filenames.")
    context_lines: int = Field(default=0, ge=0, le=10)


@tool
class GrepTool:
    name = "grep"
    description = "Search file contents with a Python regex. Returns matches with file:line."
    input_schema = GrepArgs
    annotations = ToolAnnotations(read_only=True)

    async def __call__(self, args: GrepArgs, ctx: ToolContext) -> ToolResult:
        try:
            base = _resolve(args.path, ctx) if args.path else ctx.workspace
        except _WorkspacePrefixError as e:
            return ToolResult(content=str(e), is_error=True)
        try:
            regex = re.compile(args.pattern)
        except re.error as e:
            return ToolResult(content=f"bad regex: {e}", is_error=True)
        matches: list[dict[str, object]] = []
        for p in base.rglob(args.include) if base.is_dir() else [base]:
            if not p.is_file():
                continue
            try:
                for i, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
                ):
                    if regex.search(line):
                        matches.append({"file": str(p), "line": i, "content": line})
                        if len(matches) >= 200:
                            break
            except OSError:
                continue
            if len(matches) >= 200:
                break
        body = (
            "\n".join(f"{m['file']}:{m['line']}: {m['content']}" for m in matches) or "no matches"
        )
        return ToolResult(
            content=body, structured_output={"matches": matches, "total": len(matches)}
        )


# ── helpers ──────────────────────────────────────────────────────────────────


class _WorkspacePrefixError(ValueError):
    """Raised when a path starts with a literal ``workspace/`` segment.

    Why this is an error and not silently stripped: file tools and ``bash``
    both anchor relative paths to the workspace (``bash`` via ``cwd``, file
    tools via this resolver). If we silently stripped ``workspace/`` for
    file tools, the LLM would observe ``file_write("workspace/foo")``
    succeeding and reuse the same path in ``bash("python workspace/foo")``,
    which would then look for ``<workspace>/workspace/foo`` and fail — the
    inconsistency leaks at the seam. Failing loudly here teaches the model
    a single rule that works for every tool.
    """


def _resolve(path: str, ctx: ToolContext) -> Path:
    """Resolve a tool-supplied path. Absolute paths pass through; relative
    paths are anchored to the workspace — which is also ``bash``'s default cwd,
    so the same relative path works in both tool families.
    """
    raw = path.lstrip()
    if raw.startswith("./"):
        raw = raw[2:]
    if raw == "workspace" or raw.startswith("workspace/"):
        raise _WorkspacePrefixError(
            "path starts with 'workspace/'. Your workspace IS the cwd for "
            "every file tool and for bash — use a bare relative path like "
            f"'{raw[len('workspace') :].lstrip('/') or 'file.txt'}' instead."
        )
    p = Path(raw).expanduser() if raw else Path()
    if not p.is_absolute():
        p = ctx.workspace / p
    return p.resolve()
