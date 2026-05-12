# `assistant` — General-Purpose Interactive Assistant

The default agent template that ships with Eonlet. A persistent, conversational assistant that lives in your terminal across days and weeks.

## What it does

- Conversational, like a colleague
- Keeps a free-form journal (`memory/notes.md`) and active task list (`memory/todo.md`)
- Has access to filesystem, web, and shell tools (with permission prompts)
- Builds up context over time — remembers what you've discussed before

## What it's good for

- Daily research, writing, light coding
- "Remind me what I was working on"
- Long-running questions that span days
- A general thinking partner

## What it's NOT good for

- Autonomous tasks (no triggers; needs you to attach)
- Time-sensitive scheduled work (use a scheduled agent instead, like `x-digest`)
- Sensitive automated operations (this template uses `mode: ask`, so destructive tool calls always prompt)

## Setup

No env vars required beyond `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`).

```bash
eonlet create assistant --name=alice
eonlet attach alice
```

## Customizing

Most useful changes:

- **Model**: swap `runtime.model` in `agent.yaml` to use a different LLM
- **Tools**: trim or extend `tools.builtin` list
- **System prompt**: edit `system.md` to give the agent a more specific identity ("you are a writing coach", "you are a research assistant for ML papers", etc.)
- **Memory files**: add custom Markdown files to `memory.notes_files` for domain-specific notes

If you want to fork into something more specialized:

```bash
eonlet def init writing-coach --from-template=assistant
# Edit ~/.eonlet/agents/writing-coach/system.md
```
