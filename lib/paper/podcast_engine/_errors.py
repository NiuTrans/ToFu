"""Dependency-free exception types shared by the podcast worker stages."""


class PodcastGenerationAborted(Exception):
    """The task's abort signal fired in any podcast production stage."""


class AudioSynthesisAborted(PodcastGenerationAborted):
    """The podcast task's abort signal fired between audio chunks."""


__all__ = ['AudioSynthesisAborted', 'PodcastGenerationAborted']
