"""``bash`` tool. Run a shell command in workspace."""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel, Field

from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

MAX_OUTPUT_BYTES = 100_000  # ~25k tokens-ish, per TOOL_SPEC


class BashArgs(BaseModel):
    command: str = Field(description="Shell command to run.")
    timeout: int = Field(default=30, ge=1, le=600, description="Timeout in seconds.")
    cwd: str | None = Field(
        default=None,
        description="Working directory. If unset or relative, resolves under workspace.",
    )


@tool
class BashTool:
    name = "bash"
    description = (
        "Run a shell command. Your cwd is the workspace — the same root the "
        "file_* tools anchor to. So 'python foo.py' (not 'python workspace/foo.py') "
        "runs the file you just wrote with file_write('foo.py', ...). "
        "Returns stdout, stderr, and the exit code. Output is truncated at ~100KB."
    )
    input_schema = BashArgs
    annotations = ToolAnnotations(destructive=True, network=False, idempotent=False)

    async def __call__(self, args: BashArgs, ctx: ToolContext) -> ToolResult:
        # Resolve cwd inside workspace.
        cwd = ctx.workspace
        if args.cwd:
            candidate = (ctx.workspace / args.cwd).resolve()
            if not str(candidate).startswith(str(ctx.workspace.resolve())):
                return ToolResult(content=f"cwd outside workspace: {args.cwd}", is_error=True)
            cwd = candidate

        env = {**os.environ, **ctx.env}
        try:
            proc = await asyncio.create_subprocess_shell(
                args.command,
                cwd=str(cwd),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=args.timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ToolResult(content=f"command timed out after {args.timeout}s", is_error=True)
        except Exception as e:
            return ToolResult(content=f"exec failed: {e}", is_error=True)

        out = _truncate(stdout.decode("utf-8", errors="replace"))
        err = _truncate(stderr.decode("utf-8", errors="replace"))
        rc = proc.returncode
        body = f"exit={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
        return ToolResult(
            content=body,
            is_error=(rc != 0),
            structured_output={"stdout": out, "stderr": err, "return_code": rc},
        )


def _truncate(s: str) -> str:
    if len(s.encode("utf-8")) <= MAX_OUTPUT_BYTES:
        return s
    # truncate by chars, conservative
    return s[: MAX_OUTPUT_BYTES // 2] + "\n…[truncated]…\n" + s[-MAX_OUTPUT_BYTES // 4 :]
