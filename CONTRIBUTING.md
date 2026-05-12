# Contributing to Eonlet

Thank you for your interest. Eonlet is in **pre-alpha** and developing rapidly. Here's how to participate constructively at the current stage.

## Current Stage: Pre-Alpha (v0.0.x → v0.1.0)

During this phase, the project is built primarily by one author. **Code contributions are not yet open in the usual way.** The codebase is changing too fast for parallel work to be productive. This will change at v0.2.

What we welcome right now:

### Bug Reports
Open an issue. Include:
- OS and Python version
- Eonlet version (`eonlet version`)
- Minimal reproduction steps
- Expected vs. actual behavior

### Feature Requests
Open an issue with the label `feature-request`. Include:
- What you're trying to accomplish (not just the feature itself)
- Why existing capabilities don't suffice
- Whether you'd be willing to drive the design

### Design Feedback
The most valuable contribution today. Read [`docs/SPEC.md`](docs/SPEC.md), [`docs/AGENT_CONFIG_SPEC.md`](docs/AGENT_CONFIG_SPEC.md), and [`docs/adr/`](docs/adr/). Open issues challenging assumptions, suggesting alternatives, or pointing out blind spots.

### Documentation PRs
Always welcome. Fix typos, clarify confusing sections, add examples.

### Example Agents
After v0.1.0 ships, contributions of example agents (with full `tools/` and `system.md`) are a high-leverage way to help. Open an issue first to coordinate.

## After v0.2.0: Code Contributions

When code PRs open, the standard process will be:

1. **Open an issue first** for anything non-trivial. We will not merge PRs that come without a prior issue.
2. **One PR, one concern.** If you're changing three things, that's three PRs.
3. **Tests pass and coverage doesn't regress.**
4. **Conventional commits.** Commit messages follow [conventionalcommits.org](https://www.conventionalcommits.org/).
5. **Sign-off.** PRs include `Signed-off-by:` lines (DCO).

## Development Setup (When Code Lands)

```bash
git clone https://github.com/eonlet/eonlet.git
cd eonlet
uv venv
uv sync --dev
pre-commit install
pytest
```

We use:
- **uv** for package management (no poetry, no pip-tools)
- **ruff** for linting and formatting
- **mypy** in strict mode
- **pytest** for testing
- **anyio** for async (not raw asyncio)

## Code Style

Detailed conventions live in [`docs/SPEC.md` §15](docs/SPEC.md#15-coding-standards). Highlights:

- Python ≥ 3.11
- `from __future__ import annotations` everywhere
- Type annotations on all public APIs
- No `print()` — use `structlog` for logs, `rich` for CLI output
- No bare `Exception` — raise from a custom hierarchy
- Avoid `asyncio.run` — use `anyio.run`

## Architecture Decisions

Major architectural choices are recorded as ADRs in [`docs/adr/`](docs/adr/). New architectural changes should propose an ADR before implementation. ADRs are short — usually one page — and follow the format in `0001-no-supervisor-mvp.md`.

## Code of Conduct

We pledge to make participation a harassment-free experience for everyone. The community is small enough that we don't have a formal CoC document yet, but the spirit is: be kind, assume good faith, focus on the work. If something is wrong, talk to the author directly.

## Communication

- **Issues:** discussion of specific bugs/features
- **Discussions:** open-ended questions, design conversations
- **Discord:** coming with v0.2.0
- **Email:** for sensitive matters

## Recognition

Contributors are listed in `CHANGELOG.md` per release. Substantial contributors who want to be co-maintainers are welcomed, but we won't add maintainers in the v0.0–v0.1 phase. After v0.2, we'll formalize a maintainer process.

## What Not to Send

To save your time, please don't send PRs that:

- Add a new agent framework wrapper "to make Eonlet more like \[X\]"
- Add cryptocurrency / NFT / blockchain integration of any kind
- Add LLM-generated agent definitions as "examples" without a clear use case
- Refactor large sections of the codebase for stylistic reasons
- Add dependencies on heavy ML frameworks (transformers, langchain, etc.) — we want to stay minimal

Thanks for reading this far. Eonlet exists because real software gets built by people who care about details. We're glad you might be one of them.
