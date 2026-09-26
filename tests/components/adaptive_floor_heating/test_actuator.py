"""Command/confirmation race tests without hardware; related: actuator.py."""

import asyncio
import unittest

from custom_components.adaptive_floor_heating.actuator import SwitchActuator


class ActuatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls, self.faults = [], []
        self.confirm = True
        async def send(value):
            self.calls.append(value)
            if self.confirm:
                self.actor.observe(value)
        self.actor = SwitchActuator(send, lambda: None, self.faults.append,
                                    asyncio.get_running_loop().time, timeout=0.01, retry_delays=(0, 0))
        self.actor.observe(False, initial=True)

    async def asyncTearDown(self):
        await self.actor.close()

    async def test_no_duplicate_commands_and_confirmed_transition(self):
        before = self.actor.changed_at
        self.actor.request(False)
        self.assertEqual(self.calls, [])
        self.actor.request(True)
        self.actor.request(True)
        await self.actor.wait()
        self.assertEqual(self.calls, [True])
        self.assertTrue(self.actor.observed)
        self.assertGreaterEqual(self.actor.changed_at, before)
        self.actor.request(True)
        self.assertEqual(self.calls, [True])

    async def test_service_success_without_confirmation_latches_fault_and_sends_off(self):
        self.confirm = False
        self.actor.request(True)
        await self.actor.wait()
        self.assertEqual(self.faults, ["actuation_fault"])
        self.assertEqual(self.calls, [True, False])
        self.assertFalse(self.actor.observed)

    async def test_off_retry_limit_and_explicit_retry(self):
        self.confirm = False
        self.actor.observe(True, initial=True)
        self.actor.request(False)
        self.assertFalse(await self.actor.wait())
        self.assertEqual(self.calls, [False] * 3)
        self.actor.request(False)
        await asyncio.sleep(0)
        self.assertEqual(len(self.calls), 3)
        self.confirm = True
        self.actor.request(False, retry=True)
        self.assertTrue(await self.actor.wait())
        self.assertEqual(len(self.calls), 4)

    async def test_off_preempts_pending_on_even_if_observed_off(self):
        self.confirm = False
        self.actor.request(True)
        await asyncio.sleep(0)
        self.actor.request(False)
        self.assertTrue(await self.actor.wait())
        self.assertEqual(self.calls, [True, False])
        self.assertFalse(self.actor.desired)
        self.assertTrue(self.actor.observe(True))
        self.confirm = True
        self.actor.request(False)
        self.assertTrue(await self.actor.wait())

    async def test_on_does_not_preempt_pending_off(self):
        self.confirm = False
        self.actor.observe(True, initial=True)
        self.actor.request(False)
        await asyncio.sleep(0)
        self.actor.request(True)
        self.assertFalse(self.actor.desired)
        self.actor.observe(False)
        await self.actor.wait()
        self.assertEqual(self.calls, [False])

    async def test_repeated_report_does_not_reset_transition_clock(self):
        self.actor.observe(True, initial=True)
        timestamp = self.actor.changed_at
        await asyncio.sleep(0)
        self.assertFalse(self.actor.observe(True))
        self.assertEqual(self.actor.changed_at, timestamp)

    async def test_service_exception_uses_same_failure_path(self):
        async def fail(value):
            self.calls.append(value)
            if value:
                raise RuntimeError("simulated service failure")
            self.actor.observe(False)
        self.actor._send = fail
        self.actor.request(True)
        await self.actor.wait()
        self.assertEqual(self.calls, [True, False])
        self.assertEqual(self.faults, ["actuation_fault"])
