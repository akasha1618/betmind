"""Ture de chat detasate de conexiunea SSE.

Clientul poate disparea (Safari in background pe iPhone); tura continua pe
server, evenimentele se pastreaza in memorie, iar la reconectare se reiau
de unde s-au oprit — fara un al doilea apel de analiza.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, AsyncIterator, Optional

TURN_TTL_SECONDS = 30 * 60
_MAX_TURNS = 80


class Turn:
    def __init__(self, turn_id: str, conversation_id: str, user_key: str):
        self.turn_id = turn_id
        self.conversation_id = conversation_id
        self.user_key = user_key
        self.events: list[dict] = []
        self.done = False
        self.created_at = time.monotonic()
        self.task: Optional[asyncio.Task] = None
        self.cancel_requested = False
        self._cv = asyncio.Condition()

    async def publish(self, event: dict) -> None:
        async with self._cv:
            self.events.append(event)
            self._cv.notify_all()

    async def finish(self) -> None:
        async with self._cv:
            self.done = True
            self._cv.notify_all()

    async def subscribe(self, after: int = 0) -> AsyncIterator[dict]:
        i = max(0, int(after))
        while True:
            async with self._cv:
                while i >= len(self.events) and not self.done:
                    await self._cv.wait()
                if i >= len(self.events):
                    return
                batch = self.events[i:]
                i = len(self.events)
            for ev in batch:
                yield ev


class TurnHub:
    def __init__(self) -> None:
        self._turns: dict[str, Turn] = {}

    def get(self, turn_id: str) -> Optional[Turn]:
        return self._turns.get(turn_id)

    def create(self, turn_id: str, conversation_id: str, user_key: str) -> Turn:
        self.prune()
        turn = Turn(turn_id, conversation_id, user_key)
        self._turns[turn_id] = turn
        return turn

    def prune(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        dead = [tid for tid, t in self._turns.items()
                if t.done and (now - t.created_at) > TURN_TTL_SECONDS]
        for tid in dead:
            self._turns.pop(tid, None)
        if len(self._turns) > _MAX_TURNS:
            finished = sorted(
                ((tid, t) for tid, t in self._turns.items() if t.done),
                key=lambda kv: kv[1].created_at,
            )
            for tid, _ in finished[: max(0, len(self._turns) - _MAX_TURNS)]:
                self._turns.pop(tid, None)

    def clear(self) -> None:
        self._turns.clear()

    def cancel(self, turn_id: str, user_key: str) -> bool:
        turn = self._turns.get(turn_id)
        if not turn or turn.user_key != user_key:
            return False
        turn.cancel_requested = True
        if turn.task and not turn.task.done():
            turn.task.cancel()
        return True


hub = TurnHub()
