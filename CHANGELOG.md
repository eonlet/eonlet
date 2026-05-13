# Changelog

All notable changes to Eonlet will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) starting at v1.0.0.

## [Unreleased]

### Designed

- Initial project specification
- Agent configuration schema (`agent.yaml`)
- CLI surface area
- Tool interface
- Trigger system (cron + interactive)
- Three example agent templates: `assistant`, `x-digest`, `portfolio`
- **Long-term vision: specialty → teams → organizations** (Phase C/D), documented in [`docs/concepts/teams-and-organizations.md`](docs/concepts/teams-and-organizations.md)
- Forward-compatible `metadata.specialty` and `metadata.capabilities` fields added to `agent.yaml` schema; all three bundled agents updated to declare them

### Revisions to design (round 2)

- Tool count corrected to 13 across all docs (was inconsistent — 12 in README)
- `{{fired_at_date}}` and `{{fired_at_time}}` template variables added to TRIGGER_SPEC
- `BROKER_API_SECRET` added to portfolio agent's `env.required` (was missing despite tool needing it)
- Removed misleading `send_report.py` stub tool from portfolio agent (custom tools cannot invoke other tools in MVP)
- Comparison table in README corrected: Claude Code does have scheduled tasks and limited filesystem-defined agents
- Roadmap restructured: Phase B (multi-eonlet substrate) v0.4–0.5, Phase C (teams) v0.6–0.7, Phase D (organizations) v0.8–0.9, then 1.0
- Code execution and framework adapters moved from Phase B to "After 1.0" since teams take priority

### Implementation status

Pre-alpha. No code released yet. Design is being finalized; implementation starts after spec review.
