"""Application observations for the shared standard-library demand kernel.

The physical worker/Future lifecycle lives in tofu_workers.executor. Root task
policy remains outside it; this adapter only supplies process metric observers.
"""
from tofu_workers import DemandExecutor


class RecoverableAgentExecutor(DemandExecutor):
    def __init__(self, **kwargs):
        from lib import observability
        super().__init__(metrics=observability, **kwargs)
