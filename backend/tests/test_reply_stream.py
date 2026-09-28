"""Exercise live delivery independently of clients that buffer ASGI responses."""
import asyncio
import json
import pytest
from fastapi import HTTPException
from backend.reply_stream import stream_reply as backend_stream
from codex_bridge.reply_stream import stream_reply as bridge_stream
from codex_bridge.generation import Generation


@pytest.mark.parametrize("stream_reply", [backend_stream, bridge_stream])
@pytest.mark.parametrize("fail", [False, True])
def test_text_is_delivered_before_completion_and_admission_is_released(stream_reply, fail):
    async def run():
        finish = asyncio.Event()
        released = []
        async def work(update):
            update("First words")
            await finish.wait()
            if fail:
                raise HTTPException(429, "Usage limit", headers={"X-Evencomms-Codex-Error": "rate_limit"})
            return {"text": "Final words"}
        response = stream_reply(work, lambda: released.append(True))
        assert response.headers["x-accel-buffering"] == "no"
        events = response.body_iterator
        await anext(events)  # initial heartbeat
        event = json.loads(await asyncio.wait_for(anext(events), 1))
        assert event == {"type": "text", "text": "First words"}
        assert not released
        finish.set()
        event = json.loads(await asyncio.wait_for(anext(events), 1))
        assert event["type"] == ("error" if fail else "done")
        if fail:
            assert event["code"] == "rate_limit"
        await events.aclose()
        assert released == [True]
    asyncio.run(run())


@pytest.mark.parametrize("stream_reply", [backend_stream, bridge_stream])
def test_disconnect_cleans_up_stream_waiter(stream_reply):
    async def run():
        stopped = asyncio.Event()
        released = []
        async def work(update):
            try:
                update("Partial")
                await asyncio.Event().wait()
            finally:
                stopped.set()
        events = stream_reply(work, lambda: released.append(True)).body_iterator
        await anext(events)
        await anext(events)
        await events.aclose()
        assert stopped.is_set() and released == [True]
    asyncio.run(run())


def test_native_deltas_stream_only_answer_items_and_reconcile_final_text():
    async def run():
        generation = Generation()
        generation.thread_id, generation.turn_id = "thread", "turn"
        seen = []
        generation.on_text = seen.append
        scope = {"threadId": "thread", "turnId": "turn"}
        try:
            for identity, phase in [("comment", "commentary"), ("answer", "final_answer")]:
                generation.event("item/started", {**scope, "item": {
                    "type": "agentMessage", "id": identity, "text": "", "phase": phase}})
                generation.event("item/agentMessage/delta", {**scope, "itemId": identity, "delta": "Hello"})
            assert seen == ["", "Hello"]
            assert not generation.done.done()
            generation.event("item/agentMessage/delta", {**scope, "itemId": "answer", "delta": " world"})
            assert seen[-1] == "Hello world"
            generation.event("item/completed", {**scope, "item": {
                "type": "agentMessage", "id": "answer", "text": "Hello world!", "phase": "final_answer"}})
            assert seen[-1] == "Hello world!"
        finally:
            generation.done.cancel()
    asyncio.run(run())
