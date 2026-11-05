"""Application observations for the shared standard-library demand kernel.

The physical worker/Future lifecycle lives in tofu_workers.executor. Root task
policy remains outside it; this adapter only supplies process metric observers.
Scheduling gauges are read from the kernel at scrape time through an
``ExecutorObservation``; an owner aggregating peers passes its own.
"""
from tofu_workers import DemandExecutor


class RecoverableAgentExecutor(DemandExecutor):
    def __init__(self, *, observation=None, **kwargs):
        from lib import observability
        super().__init__(metrics=observability, **kwargs)
        self._observation = (
            observability.ExecutorObservation(self.metric_pool, include_idle=True)
            if observation is None else observation)
        self._observation.attach(self)

    def shutdown(self, wait=True, *, cancel_futures=False):
        try:
            super().shutdown(wait=wait, cancel_futures=cancel_futures)
        finally:
            self._observation.retire()
