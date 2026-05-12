# The Eonlet Manifesto

## Agents should live for ages.

Today's AI agents are mostly amnesiacs. Every conversation starts fresh. Every task starts fresh. We pour expensive intelligence into context windows that get garbage-collected the moment we close a tab.

This is wrong. It is not how human collaborators work. It is not how operating systems work. It is not how the universe works.

We believe agents should:

**Live as processes, not as function calls.** A real assistant has a continuous existence — it knows what it did yesterday, it has unfinished work from last week, it sleeps when not needed and wakes when summoned. A function call has none of that. Calling `generate()` 10,000 times is not the same as having a colleague for 10,000 hours.

**Be configured, not coded.** You shouldn't need to be a Python expert to give yourself an assistant. You should write YAML and Markdown — describe what the agent does, what tools it has, when it runs — and the runtime should handle the rest. Code is the escape hatch, not the default.

**Be local, not rented.** The agent that reads your X feed and emails you a summary should run on your machine, not someone else's server. The agent that analyzes your portfolio should never see another customer's holdings — because there *is* no other customer. Local-first is not just about privacy; it's about ownership. The agent is yours.

**Be composable, like Unix tools.** `eonlet ls` and `eonlet attach` should feel as natural as `ps` and `tmux attach`. Agents should be processes you can list, pause, kill, and inspect. They should be combinable: today one agent, tomorrow a small society of them, and the runtime shouldn't break between the two.

**Be transparent, all the way down.** Every action an agent takes — every tool call, every memory write, every permission decision — should land in an append-only event log on your disk. No black box. No "trust us." You can replay the entire history of an eonlet's existence with `eonlet replay`.

## What we are not building

We are not building a hosted SaaS where you give us your credentials and we run agents for you. Other people are building that. They will be successful at it. We are doing the opposite thing.

We are not building a Web UI to compete with Letta ADE. Terminal-first is a choice, not a limitation. The kinds of users who want long-lived background agents are the same users who live in terminals.

We are not building Yet Another Agent Framework to compete with LangGraph or smolagents or Pydantic AI. Those are for *writing* agents. We are for *running* them. You will absolutely be able to use those frameworks to write the brains of your eonlets. We just won't write the brain ourselves.

We are not building a multi-agent system first. That comes later, when we've earned it. First we make one eonlet excellent.

## What we believe about open source

We believe a project gets one chance at a launch. We will not launch before the experience is good enough to keep the people who arrive.

We believe documentation is not separate from the product — it is the product, when the product is a tool other people use.

We believe in showing, not telling. The clearest argument for Eonlet is a thirty-second demo of one eonlet remembering what we talked about yesterday.

We believe in dogfood. If the author isn't using Eonlet every day to do real work, no one else should be expected to. v0.1 will ship after — and only after — the author has lived inside Eonlet for two weeks of real work without it getting in the way.

We believe small, ruthless scope beats sprawling ambition. Eonlet v0.1 will say "no" to a hundred good ideas so it can say "yes" to one excellent thing: a single agent that lives in your terminal and remembers.

We believe in Apache-2.0, in perpetuity. No bait-and-switch to BSL or SSPL. No "enterprise edition" that hides essential features behind a paywall. If we ever monetize, it will be hosted convenience or premium support — never a tax on the open source.

## A word about the name

*Eonlet*: from Greek **αἰών** ("eon" — an age, an indefinitely long span of time) plus the diminutive suffix **-let** (as in *applet*, *servlet*, *droplet*).

A small thing that lives for an age. A bounded process whose lifetime is much larger than yours. A daemon that has read its own history.

This is what we want every agent to be.

## Where this is going

The end state Eonlet is built toward is not "an agent" — it is a **society of specialist agents**, structured the way human work has always been structured: individuals, teams, organizations.

We do not believe in The One Agent — the chat box that does everything for everyone. Generalists are mediocre by construction. The deepest research, the best writing, the most reliable analysis come from specialists with deep context.

So we build toward a world where:

- **Each eonlet has a specialty.** Code review, market analysis, news curation, technical writing. Not "everything". One thing, done well.
- **Specialists form teams.** A small group of complementary specialists, coordinated by a leader, sharing context, building a track record together.
- **Teams form organizations.** A tree of teams. Same structure as a human company. Tasks flow down, results flow up, peers communicate sparingly.

This is not Phase A. This is not Phase B. This is what Eonlet is *for* — the destination that justifies the journey.

Phase A is one excellent specialist. Phase B is many specialists discovering each other. Phase C is teams. Phase D is organizations. **Each phase ships before the next is designed in detail.** But the trajectory is clear from day one.

See [`docs/concepts/teams-and-organizations.md`](docs/concepts/teams-and-organizations.md) for the full vision.

---

> *Define agents as files, run them as eonlets, attach like tmux. One specialist today; a society of them eventually.*
