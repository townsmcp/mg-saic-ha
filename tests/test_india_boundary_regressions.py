"""Offline India boundaries; HA is stubbed, published India client is real.

Run each module alone: python -m unittest test_india_boundary_regressions.
Only HTTP/session and HA persistence/lifecycle I/O are substituted.
"""
import asyncio
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow as harness
from mg_ismart_india_client import MgIndiaClient, MgIndiaApiError, MgIndiaCommandIndeterminate

INDIA = sys.modules['mg_saic.backends.india']
COORD = sys.modules['mg_saic.coordinator'].SAICMGDataUpdateCoordinator


class _Http:
    """Echo a valid PIN frame, then lose the control reply at the HTTP boundary."""
    def __init__(self):
        self.calls = []
        self.control_entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def post(self, url, *, data, **kwargs):
        session = self
        pin = data[4] == '1'  # v1.1 PIN frame, v2.1 control starts with '1'.
        session.calls.append(('pin' if pin else 'control', data))

        class Response:
            status = 200

            async def __aenter__(self):
                if not pin:
                    session.control_entered.set()
                    await session.release.wait()
                return self

            async def text(self):
                return data if pin else 'lost response'

            async def __aexit__(self, *args):
                pass
        return Response()


def _backend(session):
    backend = INDIA.IndiaBackend('9999999999', 'synthetic', vin='A' * 17, pin_hash='A' * 32)
    backend.set_api_lock(asyncio.Lock())
    client = MgIndiaClient(session, '9999999999', 'synthetic', vin=backend.vin, pin_hash='A' * 32)
    client.uid, client.token = '0' * 50, '0' * 40
    backend._client = client
    return backend


class CoreBoundaryTests(unittest.TestCase):
    def test_complete_operation_lock_cancellation_and_close(self):
        async def scenario():
            http = _Http()
            http.release.clear()
            backend = _backend(http)
            from mg_ismart_india_client.models import Status
            mapped = backend._map_status(Status(power_mode_raw=0, front_left_seat_heat_level=1,
                                                front_right_seat_heat_level=3))
            self.assertEqual(mapped.basicVehicleStatus.powerMode, 0)
            self.assertEqual(backend._seat_levels_by_vin['A' * 17], {'front_left':1, 'front_right':3})
            first = asyncio.create_task(backend.start_ac('A' * 17))
            await asyncio.wait_for(http.control_entered.wait(), 1)
            second = asyncio.create_task(backend.start_ac('B' * 17))
            await asyncio.sleep(0)
            self.assertEqual(backend._client.vin, 'A' * 17)
            self.assertEqual([kind for kind, _ in http.calls], ['pin', 'control'])
            closing = asyncio.create_task(backend.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            with self.assertRaisesRegex(MgIndiaApiError, 'closed'):
                await second
            await closing
            self.assertIsNone(backend._client)
            self.assertEqual(len(http.calls), 2)
        asyncio.run(scenario())

    def test_reauth_cancel_finishes_unload_and_recovers_enabled_sibling(self):
        async def scenario():
            flow = harness.CF.SAICMGConfigFlow()
            flow.username, flow.password = 'account', 'replacement'
            entries = [NS(entry_id='one', data={'username':'account','region':'India','vin':'A','password':'old'}, disabled_by=None),
                       NS(entry_id='disabled', data={'username':'account','region':'India','vin':'B','password':'old'}, disabled_by='user')]
            entered, release = asyncio.Event(), asyncio.Event()
            async def unload(entry_id):
                entered.set()
                await release.wait()
                return True
            ce = NS(async_entries=lambda domain: entries, async_unload=unload,
                    async_reload=AsyncMock(return_value=True), async_update_entry=MagicMock())
            flow.hass = NS(data={'mg_saic': {'one': object()}}, config_entries=ce, async_create_task=asyncio.create_task)
            task = asyncio.create_task(flow._replace_india_account_credentials())
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            ce.async_reload.assert_awaited_once_with('one')
            ce.async_update_entry.assert_not_called()
            self.assertEqual([entry.data['password'] for entry in entries], ['old', 'old'])
            self.assertEqual(flow.hass.data['mg_saic']['india_reauth_in_progress'], set())
        asyncio.run(scenario())

    def test_public_uncertain_command_preserves_failure_and_no_success_refresh(self):
        # A real client performs PIN and control; the session returns malformed I/O.
        from test_controls_after_failed_poll import LOCK
        class HomeAssistantError(Exception):
            pass
        async def scenario():
            http = _Http()
            backend = _backend(http)
            entity = LOCK.SAICMGLockEntity.__new__(LOCK.SAICMGLockEntity)
            entity._client, entity._vin = backend, 'A' * 17
            entity.coordinator = NS(after_action_delay=1, lock_unlock_long_interval=2,
                                    schedule_action_refresh=AsyncMock(), record_command_error=MagicMock())
            with self.assertRaisesRegex(HomeAssistantError, 'completion is unknown') as caught:
                await entity.async_lock()
            self.assertIsInstance(caught.exception.__cause__, MgIndiaCommandIndeterminate)
            entity.coordinator.record_command_error.assert_called_once()
            entity.coordinator.schedule_action_refresh.assert_not_awaited()
            self.assertEqual([kind for kind, _ in http.calls], ['pin', 'control'])
        with patch.object(sys.modules['homeassistant.exceptions'], 'HomeAssistantError', HomeAssistantError):
            asyncio.run(scenario())

    def test_india_startup_does_not_restore_global_replies_without_origins(self):
        from test_saved_vehicle_data import _client_replies
        status, charging = _client_replies()
        saved = sys.modules['mg_saic.saved_data']
        c = COORD.__new__(COORD)
        c.client = NS(_india_backend=True)
        c.vin = 'VIN'
        c.trip_stats = NS(saved_vehicle_data={'status':saved.encode_vehicle_data(status),
                                              'charging':saved.encode_vehicle_data(charging)})
        c.last_status_data = c.last_charging_data = None
        c._last_status_time = None
        c._restore_saved_vehicle_data()
        data = {'status':None, 'charging':None}
        c._show_saved_vehicle_data(data)
        self.assertIsNone(c.last_status_data)
        self.assertIsNone(c.last_charging_data)
        self.assertEqual(data, {'status':None, 'charging':None})
        self.assertIsNone(c._last_status_time)


if __name__ == '__main__':
    unittest.main()
