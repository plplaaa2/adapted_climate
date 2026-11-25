"""Bounded switch commands and confirmation; related: runtime.py, controller.py."""

import asyncio
from collections.abc import Awaitable, Callable
import logging

from .const import COMMAND_SETTLE, COMMAND_TIMEOUT, OFF_RETRY_DELAYS

_LOGGER = logging.getLogger(__name__)


class SwitchActuator:
    """Confirm observed states, cancel superseded ON, and limit OFF retries."""

    def __init__(
        self, send: Callable[[bool], Awaitable[None]], changed: Callable[[], None],
        fault: Callable[[str], None], clock: Callable[[], float], *,
        timeout: float = COMMAND_TIMEOUT, retry_delays: tuple[float, ...] = OFF_RETRY_DELAYS,
        settle: float = COMMAND_SETTLE,
        confirmed: Callable[[bool], bool] | None = None,
    ) -> None:
        self._send, self._changed, self._fault, self._clock = send, changed, fault, clock
        self._timeout, self._retry_delays = timeout, retry_delays
        self.observed: bool | None = None
        # Climate commands confirm their target separately from actual heat. See runtime.py.
        self._confirmed = confirmed or (lambda heating: self.observed is heating)
        self.changed_at = clock()
        self.desired = False
        self.pending: bool | None = None
        self.predictive_start_requested = False
        self.off_exhausted = False
        # A slow RS485 source reports our command and then its old value again; keep a
        # window in which that fallback is an echo, not a manual change. See runtime.py.
        self._settle = settle
        self._attempted_at = float("-inf")
        self._attempt_changed_at: float | None = None
        self._settle_state: bool | None = None
        self._settle_until = float("-inf")
        # Set while an ON retry waits for its settle window; runtime.py turns it into a deadline.
        self.next_attempt_at: float | None = None
        self._generation = 0
        self._task: asyncio.Task | None = None
        self._retired: set[asyncio.Task] = set()
        self._event = asyncio.Event()

    def observe(self, state: bool | None, *, initial: bool = False) -> bool:
        """Return whether a known-state transition was unexpected."""
        previous = self.observed
        now = self._clock()
        # A fallback to the value before our own command is the device echoing itself.
        echo = (not initial and previous is not None and state is not None
                and state != previous and self._is_echo(state, now))
        unexpected = (not initial and previous is not None and state is not None
                      and state != previous and state != self.pending and not echo)
        if state != previous or initial:
            self.observed = state
            # An echo is neither a completed run nor a manual change: restore the OFF
            # clock from before the command so the retry is not held by the minimum
            # OFF wait; related: controller.py.
            if echo:
                if self._attempt_changed_at is not None:
                    self.changed_at = self._attempt_changed_at
            else:
                self.changed_at = now
            if state is None:
                # Remember the last known value so a report after the link returns
                # cannot look like an external change; related: runtime.py.
                if previous is not None:
                    self._settle_state = previous
            elif previous is None:
                # The first report after a dropout is a resync; keep the pre-dropout
                # value as the echo baseline for the reports that follow.
                self._settle_until = max(self._settle_until, now + self._settle)
        if state is False and self._confirmed(False):
            self.off_exhausted = False
        self._event.set()
        return unexpected

    def settling_echo(self, state: bool | None) -> bool:
        """Report a fallback to the pre-command value inside the settle window."""
        return self._is_echo(state, self._clock())

    def _is_echo(self, state: bool | None, now: float) -> bool:
        return (state is not None and state == self._settle_state
                and now < self._settle_until)

    def _mark_attempt(self) -> None:
        """Open the settle window that tolerates the device's delayed echo."""
        self._attempted_at = self._clock()
        self._attempt_changed_at = self.changed_at
        if self.observed is not None:
            # Keep the last known value as the echo baseline while the link is down.
            self._settle_state = self.observed
        self._settle_until = max(self._settle_until, self._attempted_at + self._settle)

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    # Retain the ON origin through Climate target confirmation; related: runtime.py.
    def request(self, heating: bool, *, retry: bool = False, force: bool = False,
                predictive_start: bool = False) -> None:
        """Coalesce identical requests; OFF preempts a pending ON immediately."""
        if retry and not self.busy:
            self.off_exhausted = False
        if self.busy and self.desired == heating:
            return
        if self.busy and not self.desired and heating:
            # A new heat demand cannot interrupt an unfinished OFF confirmation.
            return
        if not heating:
            self.next_attempt_at = None
        elif not force and self._clock() - self._attempted_at < self._settle:
            # The last ON command may still be settling; retry when its window closes
            # instead of hammering a slow RS485 source; related: runtime.py.
            self.next_attempt_at = self._attempted_at + self._settle
            return
        else:
            self.next_attempt_at = None
        superseded_on = self.busy and self.desired
        if self.busy:
            self._task.cancel()
            self._retired.add(self._task)
            self._task.add_done_callback(self._retired.discard)
        self.desired = heating
        self._generation += 1
        self.pending = None
        if ((self._confirmed(heating) and not superseded_on and not force)
                or (not heating and self.off_exhausted)):
            if not heating:
                self.predictive_start_requested = False
            self._task = None
            return
        self.predictive_start_requested = heating and predictive_start
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
                        self._mark_attempt()
                        await self._send(heating)
                        while not self._confirmed(heating):
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
        if self._confirmed(False) and self.observed is not False:
            # A Climate target can be confirmed before the heat source stops. See runtime.py.
            try:
                async with asyncio.timeout(self._timeout):
                    while self._confirmed(False) and self.observed is not False:
                        self._event.clear()
                        await self._event.wait()
            except TimeoutError:
                return False
        return self.observed is False and self._confirmed(False)

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
        self.predictive_start_requested = False
