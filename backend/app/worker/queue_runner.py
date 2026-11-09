from app.agents.runner import TaskRunner
from app.worker.celery_app import celery_app, make_enqueue


class QueueRunner(TaskRunner):
    """The API-side runner when CELERY_ENABLED=true. It does the database work (claiming tasks, expiring
    approvals) but never runs a graph itself: `start` just puts the task id on the queue."""

    def __init__(self, session_factory, gateway, settings, celery=None):
        super().__init__(session_factory, gateway, checkpointer=None, settings=settings)
        self._enqueue = make_enqueue(celery or celery_app)
        self.dispatch = lambda task_id, llm: self._enqueue(task_id)

    def start(self, task_id, llm=None, resume=None, recover=False):
        self._enqueue(task_id)
