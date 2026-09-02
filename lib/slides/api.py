"""Public application API for editable-slide production."""

from lib.slides.engine import (
    resume_interrupted_decks,
    run_slides_task,
    slides_root,
    start_slides_job,
)
from lib.slides.recipe import build_deck_from_topic, slides_recipe_stages

__all__ = (
    'build_deck_from_topic',
    'resume_interrupted_decks',
    'run_slides_task',
    'slides_recipe_stages',
    'slides_root',
    'start_slides_job',
)
