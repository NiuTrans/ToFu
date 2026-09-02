"""Dependency-free exception types shared by the podcast worker stages."""


class AudioSynthesisAborted(Exception):
    """The podcast task's abort signal fired between audio chunks."""


__all__ = ['AudioSynthesisAborted']
