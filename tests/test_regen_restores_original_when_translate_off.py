"""Regression test: regenerate with auto-translate OFF must restore the original.

Bug: an auto-translated user message stores ``content`` = the English
translation (what the model saw) and ``originalContent`` = the user's original
input (what the UI shows). When the user later turns auto-translate OFF and
clicks plain *Regenerate* (which sends no ``editedContent``), the backend's
translate block is skipped — so ``user_msg['content']`` stayed the STALE
English translation and the model silently received English instead of the
user's original text.

Fix: ``chat_regenerate`` (routes/chat.py, step 3) now, when auto-translate is
OFF and a leftover ``originalContent`` is present, restores ``content`` from
``originalContent``, drops the translation metadata, and signals
``restoredOriginal`` so the frontend clears its stale local fields.

Run:  pytest tests/test_regen_restores_original_when_translate_off.py -m api
"""
from __future__ import annotations

import time

import pytest


@pytest.mark.api
class TestRegenRestoresOriginalWhenTranslateOff:

    def _seed(self, flask_client, conv_id, now):
        """Persist a conv whose first user msg was auto-translated CN→EN."""
        save_resp = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Translate off regen test",
            "messages": [
                {
                    "role": "user",
                    # content = English translation that was actually sent
                    "content": "Help me write a quicksort",
                    # originalContent = the user's real input (shown in UI)
                    "originalContent": "帮我写一个快速排序",
                    "_translateDone": True,
                    "_translateModel": "mock-translate-model",
                    "timestamp": now,
                },
                {"role": "assistant", "content": "first answer", "timestamp": now + 1},
            ],
            "createdAt": now,
            "updatedAt": now,
        })
        assert save_resp.status_code == 200

    def test_translate_off_restores_original(self, flask_client, monkeypatch):
        conv_id = f"test-regen-troff-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        self._seed(flask_client, conv_id, now)
        monkeypatch.setattr('routes.chat._start_task_for_conv',
                            lambda *a, **k: ('stub-task-id', None))

        # Regenerate with auto-translate OFF, no editedContent (plain Regenerate).
        resp = flask_client.post("/api/v1/chat/regenerate", json={
            "convId": conv_id,
            "truncateToIndex": 0,
            "config": {"model": "mock-model", "autoTranslate": False},
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()

        # The server must signal the restore and return the corrected message.
        assert body.get("restoredOriginal") is True
        um = body.get("userMessage")
        assert um is not None
        assert um["content"] == "帮我写一个快速排序"   # original restored
        assert "originalContent" not in um             # metadata dropped
        assert "_translateDone" not in um

        flask_client.delete(f"/api/v1/conversations/{conv_id}")

    def test_translate_on_keeps_existing_translation(self, flask_client, monkeypatch):
        """Sanity: with auto-translate ON and an already-translated msg (no
        edit), the existing translation is kept and NOT flagged as restored."""
        conv_id = f"test-regen-tron-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        self._seed(flask_client, conv_id, now)
        monkeypatch.setattr('routes.chat._start_task_for_conv',
                            lambda *a, **k: ('stub-task-id', None))

        resp = flask_client.post("/api/v1/chat/regenerate", json={
            "convId": conv_id,
            "truncateToIndex": 0,
            "config": {"model": "mock-model", "autoTranslate": True},
        })
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        # No restore happened; already_translated short-circuit keeps content.
        assert body.get("restoredOriginal") is False

        flask_client.delete(f"/api/v1/conversations/{conv_id}")
