"""Bounded switch commands and confirmation; related: runtime.py, controller.py."""

import asyncio
from collections.abc import Awaitable, Callable
import logging

from .const import COMMAND_TIMEOUT, OFF_RETRY_DELAYS

_LOGGER = logging.getLogger(__name__)


class SwitchActuator:
    """Confirm observed states, cancel superseded ON, and limit OFF retries."""

    def __init__(
        self, send: Callable[[bool], Awaitable[None]], changed: Callable[[], None],
        fault: Callable[[str], None], clock: Callable[[], float], *,
        timeout: float = COMMAND_TIMEOUT, retry_delays: tuple[float, ...] = OFF_RETRY_DELAYS,
    ) -> None:
        self._send, self._changed, self._fault, self._clock = send, changed, fault, clock
        self._timeout, self._retry_delays = timeout, retry_delays
        self.observed: bool | None = None
        self.changed_at = clock()
        self.desired = False
        self.pending: bool | None = None
        self.off_exhausted = False
        self._generation = 0
        self._task: asyncio.Task | None = None
        self._retired: set[asyncio.Task] = set()
        self._event = asyncio.Event()

    def observe(self, state: bool | None, *, initial: bool = False) -> bool:
        """Return whether a known-state transition was unexpected."""
        previous = self.observed
        unexpected = (not initial and previous is not None and state is not None
                      and state != previous and state != self.pending)
        if state != previous or initial:
            self.observed = state
            self.changed_at = self._clock()
        if state is False:
            self.off_exhausted = False
        self._event.set()
        return unexpected

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def request(self, heating: bool, *, retry: bool = False, force: bool = False) -> None:
        """Coalesce identical requests; OFF preempts a pending ON immediately."""
        if retry and not self.busy:
            self.off_exhausted = False
        if self.busy and self.desired == heating:
            return
        if self.busy and not self.desired and heating:
            # A new heat demand cannot interrupt an unfinished OFF confirmation.
            return
        superseded_on = self.busy and self.desired
        if self.busy:
            self._task.cancel()
            self._retired.add(self._task)
            self._task.add_done_callback(self._retired.discard)
        self.desired = heating
        self._generation += 1
        self.pending = None
        if ((self.observed is heating and not superseded_on and not force)
                or (not heating and self.off_exhausted)):
            self._task = None
            return
        generation = self._generation
        self.pending = heating
        self._task = asyncio.create_task(
            self._run(heating, generation), name="adaptive_floor_heating_switch"
        )

    async def _run(self, heating: bool, generation: int) -> None:
        """Bound service execution and state confirmation with the same timeout."""
        delays = (0.0,) if heating else (0.0, *self._retry_delays)
        try:
            for delay in delays:
                if delay:
                    await asyncio.sleep(delay)
                if generation != self._generation:
                    return
                try:
                    async with asyncio.timeout(self._timeout):
                        self._event.clear()
                        await self._send(heating)
                        while self.observed is not heating:
                            await self._event.wait()
                            self._event.clear()
                    return
                except Exception as err:
                    # Service exceptions are treated as failures, never success.
                    if generation != self._generation:
                        return
                    _LOGGER.debug("Switch command failed: %s", type(err).__name__)
            self._fault("actuation_fault")
            if heating:
                self._task = None
                self.pending = None
                self.request(False, force=True)
            else:
                self.off_exhausted = True
                _LOGGER.error("Heater OFF could not be confirmed after bounded retries")
        finally:
            if generation == self._generation:
                self.pending = None
                self._task = None
            self._changed()

    async def wait(self) -> bool:
        """Wait for the current command including any ON-failure OFF recovery."""
        while self._task is not None:
            task = self._task
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        return self.observed is False

    async def close(self) -> None:
        """Cancel internal tasks after a confirmed stop or HA shutdown deadline."""
        self._generation += 1
        tasks = list(self._retired)
        if self._task is not None:
            tasks.append(self._task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self.pending = None
