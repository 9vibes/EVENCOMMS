"""Bounded, coalesced live text transport; only complete results are authoritative."""
import asyncio
import contextlib
import json
from fastapi import HTTPException
from fastapi.responses import StreamingResponse


def stream_reply(work, release=lambda: None):
    async def events():
        changed = asyncio.Event()
        latest = None
        closed = False

        def update(text):
            nonlocal latest
            if not closed:
                latest = text
                changed.set()

        task = asyncio.create_task(work(update))
        task.add_done_callback(lambda _: changed.set())
        try:
            yield b'\n'
            while True:
                try:
                    await asyncio.wait_for(changed.wait(), 10)
                except TimeoutError:
                    yield b'\n'
                    continue
                changed.clear()
                if latest is not None:
                    text, latest = latest, None
                    yield (json.dumps({"type": "text", "text": text}) + "\n").encode()
                if task.done():
                    try:
                        event = {"type": "done", "response": task.result()}
                    except HTTPException as error:
                        event = {"type": "error", "status": error.status_code, "detail": error.detail,
                                 "code": (error.headers or {}).get("X-Evencomms-Codex-Error")}
                    except Exception:
                        event = {"type": "error", "status": 502, "detail": "Response stream failed."}
                    yield (json.dumps(event) + "\n").encode()
                    break
        finally:
            closed = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            release()

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
