"""Trigger subsystem — what causes the agent to *act*.

Per TRIGGER_SPEC §1: the worker owns a trigger queue. Producers are the cron
scheduler (this package) and the IPC server (`message.send`, `trigger.fire`).
The worker's single ``main_loop`` task drains the queue and hands each
``TriggerItem`` to ``AgentRuntime.handle_user_message``.
"""

from .scheduler import CronScheduler, TriggerItem

__all__ = ["CronScheduler", "TriggerItem"]
