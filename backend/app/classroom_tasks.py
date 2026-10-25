from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import HTTPException

from app.security import StudentIdentity


async def run_in_classroom(student: StudentIdentity, operation):
    """Cancel queued and in-flight work when its original classroom is revoked."""
    lease = student.lease
    task = asyncio.ensure_future(operation)
    if lease and not lease.register(task):
        task.cancel()
    try:
        result = await task
        if lease and lease.revoked:
            raise HTTPException(status_code=403, detail="Classroom has ended or its entry has changed")
        return result
    except asyncio.CancelledError:
        if lease and lease.revoked:
            raise HTTPException(status_code=403, detail="Classroom has ended or its entry has changed") from None
        raise
    finally:
        if lease:
            lease.unregister(task)


async def stream_in_classroom(student: StudentIdentity, source):
    """Keep the source cancellable even while a slow client blocks ASGI send."""
    iterator = source.__aiter__()
    queue = asyncio.Queue(maxsize=1)
    pending = None

    async def produce():
        try:
            async for chunk in iterator:
                await queue.put(chunk)
        finally:
            await iterator.aclose()

    producer = asyncio.create_task(run_in_classroom(student, produce()))
    try:
        while True:
            if producer.done() and queue.empty():
                await producer
                return
            pending = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({pending, producer}, return_when=asyncio.FIRST_COMPLETED)
            if pending not in done:
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
                await producer
                continue
            chunk = pending.result()
            pending = None
            # Never deliver buffered data after its source classroom was revoked.
            if producer.done():
                producer.result()
            if student.lease and student.lease.revoked:
                await producer
                raise HTTPException(status_code=403, detail="Classroom has ended or its entry has changed")
            yield chunk
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending
        producer.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await producer
