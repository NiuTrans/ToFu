"""Build a real podcast carrier when a worker spec bypasses admission."""


def new_podcast_task(task_id, paper_hash, mode, lang, voice, model, *, user_id: int):
    from lib.paper import podcast_runtime

    return podcast_runtime._production.create_task(
        task_id, user_id=user_id,
        **podcast_runtime._podcast_task_shape(paper_hash, mode, lang, voice, model))
