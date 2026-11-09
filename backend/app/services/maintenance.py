"""The periodic housekeeping tick. Called by an in-process loop (dev) or by Celery beat (production)."""
import logging

from app.services.scheduler import fire_due_schedules

log = logging.getLogger("app.maintenance")


async def run_maintenance(sf, runner, llm) -> dict:
    result = {}
    for name, step in (("expired_approvals", lambda: runner.expire_overdue_approvals(llm)),
                       ("schedules_fired", lambda: fire_due_schedules(sf, runner.dispatch, llm)),
                       ("reaped_tasks", lambda: runner.reap_stale_tasks(llm))):
        try:    # one failing duty must not stop the others
            out = await step()
            result[name] = out if isinstance(out, int) else len(out)
        except Exception:
            log.exception("maintenance step %s failed", name)
            result[name] = -1
    return result
