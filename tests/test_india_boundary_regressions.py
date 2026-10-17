"""Offline India boundaries; HA is stubbed, published India client is real.

Run each module alone: python -m unittest test_india_boundary_regressions.
Only HTTP/session and HA persistence/lifecycle I/O are substituted.
"""
import asyncio
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow as harness
from mg_ismart_india_client import MgIndiaClient, MgIndiaApiError, MgIndiaCommandIndeterminate

INDIA = sys.modules['mg_saic.backends.india']
COORD = sys.modules['mg_saic.coordinator'].SAICMGDataUpdateCoordinator
LOGIC = sys.modules['mg_saic.logic']
TRIP = sys.modules['mg_saic.trip_stats']
ERRORS = sys.modules['mg_saic.errors']


def _load_india_manager(saved):
    """Reload through real storage handling with the India backend selected."""
    manager = TRIP.TripStatsManager(object(), 'entry', 'VIN', india=True)
    store = NS(async_load=AsyncMock(return_value=saved))
    with patch.dict(sys.modules, {
        'homeassistant.helpers.storage': NS(Store=lambda *_a, **_k: store),
    }):
        asyncio.run(manager.async_load())
    return manager


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


class TelemetryBoundaryTests(unittest.TestCase):
    @staticmethod
    def _snap(hour, soc, odo, label, **kwargs):
        snap = TRIP.ChargeSnapshot(ts=f'2026-10-14T{hour:02d}:00:00+00:00', soc_pct=soc,
                                   odometer_km=odo, range_km=soc * 2, **kwargs)
        snap.india_provenance = {field: {'source':label, 'field':field, 'measurement_time':hour}
                                for field in ('soc_pct','pack_energy_kwh','odometer_km','range_km','record_start','record_end','capacity_kwh')}
        return snap

    def test_stopped_charge_selects_field_origins_and_filters_record(self):
        stopped = self._snap(4, 100, 1000, 'stopped')
        epoch = datetime.fromisoformat(stopped.ts).timestamp()
        for odo, keep in ((1000, True), (None, False)):
            with self.subTest(odometer=odo, keep_record=keep):
                stopped.odometer_km = odo
                current = self._snap(12, 80, 1020, 'current', record_start=epoch-3600,
                                     record_end=epoch+(0 if keep else 3600))
                selected = TRIP.charge_end_from_stopped(stopped, current)
                self.assertEqual(selected.soc_pct, 100)
                self.assertEqual(selected.india_provenance['ts']['value'], stopped.ts)
                self.assertEqual(selected.india_provenance['soc_pct'], stopped.india_provenance['soc_pct'])
                self.assertEqual(selected.india_provenance['pack_energy_kwh'], stopped.india_provenance['pack_energy_kwh'])
                self.assertEqual(selected.india_provenance['range_km'], stopped.india_provenance['range_km'])
                self.assertEqual(selected.india_provenance['odometer_km'], (stopped if odo is not None else current).india_provenance['odometer_km'])
                if keep:
                    self.assertEqual(selected.india_provenance['record_end'], current.india_provenance['record_end'])
                else:
                    self.assertIsNone(selected.record_end)
                    self.assertEqual(selected.india_provenance['record_end']['source'], 'filtered')
                self.assertEqual(current.india_provenance['soc_pct']['source'], 'current')
                self.assertEqual(selected.india_provenance['capacity_kwh'], current.india_provenance['capacity_kwh'])
                global_current = TRIP.ChargeSnapshot.from_dict({k:v for k,v in current.to_dict().items() if k!='india_provenance'})
                global_stop = TRIP.ChargeSnapshot.from_dict({k:v for k,v in stopped.to_dict().items() if k!='india_provenance'})
                self.assertFalse(hasattr(TRIP.charge_end_from_stopped(global_stop, global_current), 'india_provenance'))

    def test_charge_close_uses_current_capacity_argument_with_stopped_fields(self):
        manager = TRIP.TripStatsManager(None, 'entry', 'VIN', india=True)
        start = self._snap(1, 50, 1000, 'start')
        stopped = self._snap(4, 80, 1000, 'stopped')
        current = self._snap(12, 70, 1020, 'current')
        manager.note_charge_state(True, start, capacity_kwh=64, now_iso=start.ts)
        manager.note_charge_stopped(stopped)
        result, _ = manager.note_charge_state(False, current, capacity_kwh=48, now_iso=current.ts)
        self.assertEqual(result['energy_added_kWh'], 14.4)
        self.assertEqual(result['india_provenance']['capacity_kwh'], {'value':48, 'origin':current.india_provenance['capacity_kwh']})
        self.assertEqual(result['india_provenance']['end']['soc_pct'], stopped.india_provenance['soc_pct'])

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

    def test_mixed_origins_replays_and_held_counter_restoration(self):
        # Reuse the repository's real published-model fixture, without running
        # its test class. No function under test is replaced.
        import test_india_client_contract as contract
        from mg_ismart_india_client.models import Status
        status_stamp = int(datetime.now(timezone.utc).timestamp())
        charge_stamp = status_stamp - 100
        backend = INDIA.IndiaBackend('9999999999', 'synthetic', vin='VIN')
        backend._electric_vins.add('VIN')
        basic = backend._map_status(Status(status_time=status_stamp, fuel_level=0, odometer_km=1000)).basicVehicleStatus
        model = contract.TestChargeStatusContract()._charge(
            status_time=charge_stamp, soc=90, range_km=50, odometer_km=900,
            distance_since_last_charge_km=10, power_usage_since_last_charge_kwh=5,
            battery_energy_kwh=45, is_charging=False, is_plugged_in=False)
        backend._charge_status_by_vin['VIN'] = model
        charging = asyncio.run(backend.get_charging_info('VIN'))
        rcs = charging.rvsChargeStatus
        self.assertEqual(rcs.measurement_time, charge_stamp)
        self.assertEqual(rcs.chargingDuration, model.charge_time_elapsed_s)
        self.assertEqual(LOGIC.india_measurement(basic, charging, 'soc', provenance=True),
                         (0, {'source':'ordinary','field':'extendedData1','measurement_time':status_stamp,'factor':1.0}))
        c = COORD.__new__(COORD)
        c.client = NS(_india_backend=True)
        c.trip_stats = TRIP.TripStatsManager(None, 'entry', 'VIN', india=True)
        c.vin = 'VIN'
        c._india_state_status_time, c._india_state_charge_time = status_stamp, charge_stamp
        c.last_charging_data = object()
        retained = c.last_charging_data
        c._update_state({'status':NS(basicVehicleStatus=basic), 'charging':charging})
        self.assertIs(c.last_charging_data, retained)
        self.assertIs(c.last_status_data.basicVehicleStatus, basic)
        self.assertIsNone(c.trip_stats.open_charge)
        c.counter_reset_guard = LOGIC.SinceChargeCounterGuard()
        c._schedule_trip_save = MagicMock()  # HA storage boundary
        c._apply_counter_reset_guard(charging, datetime.fromtimestamp(charge_stamp, timezone.utc))
        rcs.measurement_time = status_stamp + 100
        rcs.mileageSinceLastCharge, rcs.powerUsageSinceLastCharge, rcs.lastChargeEndingPower = 0, 0, 400
        c._apply_counter_reset_guard(charging, datetime.fromtimestamp(status_stamp + 100, timezone.utc))
        self.assertEqual(rcs.lastChargeEndingPower, 500)
        origin = rcs.india_provenance['lastChargeEndingPower']
        self.assertTrue(origin['held'])
        self.assertEqual(origin['measurement_time'], charge_stamp)
        restored = LOGIC.SinceChargeCounterGuard.from_dict(
            c.counter_reset_guard.to_dict(india=True), india=True
        )
        self.assertEqual(restored.india_provenance, c.counter_reset_guard.india_provenance)

    def test_wobble_keeps_processed_counter_origins_through_reset_and_restart(self):
        from test_charge_reload import _saved
        from test_counter_reset_guard import BEFORE, PHANTOM, _payload, _r
        c = COORD.__new__(COORD)
        c.client = NS(_india_backend=True)
        c.vin = 'VIN'
        c.counter_reset_guard = LOGIC.SinceChargeCounterGuard()
        c.trip_stats = TRIP.TripStatsManager(None, 'entry', 'VIN', india=True)
        c._schedule_trip_save = MagicMock()  # HA storage scheduling boundary

        def apply(stamp, reading):
            frame = _payload(reading)
            frame.india = True
            frame.rvsChargeStatus.india = True
            frame.rvsChargeStatus.measurement_time = stamp
            frame.rvsChargeStatus.chargingGunState = reading['plugged']
            c._apply_counter_reset_guard(frame, datetime.fromtimestamp(stamp, timezone.utc))
            return frame.rvsChargeStatus

        apply(100, BEFORE)
        for stamp in (200, 300):
            rcs = apply(stamp, _r(BEFORE, km=6099, kwh=265))
            self.assertEqual((rcs.mileageSinceLastCharge, rcs.powerUsageSinceLastCharge), (6100, 266))
            for field, key in (('mileageSinceLastCharge', 'km'), ('powerUsageSinceLastCharge', 'kwh')):
                self.assertEqual(rcs.india_provenance[field]['measurement_time'], 100)
                self.assertTrue(rcs.india_provenance[field]['held'])
                self.assertEqual(c.counter_reset_guard.india_provenance['last'][key], rcs.india_provenance[field])
                self.assertEqual(c.counter_reset_guard.india_provenance['raw'][key]['measurement_time'], stamp)
            self.assertEqual((c.counter_reset_guard.offset_km, c.counter_reset_guard.offset_kwh), (0, 0))
        restored = _load_india_manager(_saved(c.trip_stats))
        c.counter_reset_guard = LOGIC.SinceChargeCounterGuard.from_dict(
            restored.counter_reset_guard, india=True
        )
        rcs = apply(400, PHANTOM)
        self.assertEqual((rcs.mileageSinceLastCharge, rcs.powerUsageSinceLastCharge, rcs.lastChargeEndingPower), (6100, 266, 724))
        for field, key in (('mileageSinceLastCharge', 'km'), ('powerUsageSinceLastCharge', 'kwh')):
            origin = rcs.india_provenance[field]
            self.assertEqual(origin['inputs']['raw']['measurement_time'], 400)
            self.assertEqual(origin['inputs']['offset']['value'], getattr(c.counter_reset_guard, 'offset_' + key))
            # The guard keeps the latest offset's input; sensor metadata only
            # needs the offset summary, without nesting its whole ancestry.
            offset = c.counter_reset_guard.india_provenance['offset_' + key]
            self.assertEqual(offset['inputs']['previous_raw']['measurement_time'], 100)
            self.assertNotIn('inputs', origin['inputs']['offset'])
        self.assertEqual(rcs.india_provenance['lastChargeEndingPower']['measurement_time'], 300)
        c.counter_reset_guard = LOGIC.SinceChargeCounterGuard.from_dict(
            _load_india_manager(_saved(c.trip_stats)).counter_reset_guard, india=True
        )
        rcs = apply(500, _r(PHANTOM, km=10, kwh=2))
        self.assertEqual((rcs.mileageSinceLastCharge, rcs.powerUsageSinceLastCharge), (6110, 268))
        rcs = apply(600, _r(PHANTOM, km=9, kwh=1))
        self.assertEqual((rcs.mileageSinceLastCharge, rcs.powerUsageSinceLastCharge), (6110, 268))
        for field in ('mileageSinceLastCharge', 'powerUsageSinceLastCharge'):
            self.assertEqual(rcs.india_provenance[field]['measurement_time'], 500)
            self.assertEqual(rcs.india_provenance[field]['inputs']['raw']['measurement_time'], 500)
        rcs = apply(700, _r(PHANTOM, plugged=True, end=PHANTOM['end'] + 1))
        self.assertEqual((rcs.mileageSinceLastCharge, rcs.powerUsageSinceLastCharge), (0, 0))
        self.assertEqual(rcs.india_provenance['powerUsageSinceLastCharge']['measurement_time'], 700)

    def test_setup_repairs_saved_india_offset_without_relabeling_last_input(self):
        from test_counter_reset_guard import BEFORE
        last_origin = {'source': 'charge', 'field': 'powerUsageSinceLastCharge', 'measurement_time': 100}
        saved = {'counter_reset_guard': {
            'last': BEFORE, 'offset_kwh': 2519, 'offset_km': 0,
            'india_provenance': {'last': {'kwh': last_origin}, 'raw': {'kwh': last_origin},
                                 'offset_kwh': {'source': 'derived', 'operation': 'counter reset offset'}}}}
        c = COORD(None, NS(_india_backend=True), NS(entry_id='entry', data={'vin': 'VIN'}, options={}))
        c.hass = None  # the HA coordinator stub does not retain its constructor argument
        c._remove_renamed_quiet_entities = MagicMock()  # HA entity registry boundary
        c._schedule_trip_save = MagicMock()  # HA storage scheduling boundary
        c.async_config_entry_first_refresh = AsyncMock(side_effect=asyncio.CancelledError)
        storage = NS(Store=lambda *_a: NS(async_load=AsyncMock(return_value=saved)))
        with patch.dict(sys.modules, {'homeassistant.helpers.storage': storage}):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(c.async_setup())
        state = c.trip_stats.counter_reset_guard
        self.assertEqual(state['offset_kwh'], 0)
        self.assertEqual(state['india_provenance']['offset_kwh'], {'source': 'constant', 'value': 0})
        self.assertEqual(state['india_provenance']['last']['kwh'], last_origin)
        self.assertEqual(state['india_provenance']['raw']['kwh'], last_origin)
        c._schedule_trip_save.assert_called_once()

    def test_abrp_partial_success_and_replay_keep_independent_watermarks(self):
        ABRP = harness._load('mg_saic.abrp', harness.PKG_DIR/'abrp.py')
        api = ABRP.AbrpApi.__new__(ABRP.AbrpApi)
        status = NS(basicVehicleStatus=NS(india=True, battery_soc_available=True, measurement_time=200, extendedData1=60))
        charge = NS(india=True, chrgMgmtData=NS(measurement_time=100, bmsPackSOCDsp=500, decoded_power=2), rvsChargeStatus=NS(measurement_time=100))
        groups = api._india_payloads(status, charge)
        self.assertEqual(groups['ordinary']['soc'], 60)
        self.assertEqual(groups['charge']['soc'], 50)
        self.assertEqual(groups['charge']['utc'], 100)
        status.gpsPosition = NS(measurement_time=200, gps_status_decoded=2,
                                wayPoint=NS(position=NS(latitude=12000000, longitude=77000000)))
        groups = api._india_payloads(status, charge)
        self.assertEqual(groups['gps']['soc'], 60)
        self.assertEqual(groups['gps']['lat'], 12)
        status.gpsPosition.gps_status_decoded = 0
        self.assertNotIn('gps', api._india_payloads(status, charge))
        self.assertEqual(api._india_payloads(None, NS(
            india=True, chrgMgmtData=NS(measurement_time=100, decoded_power=2))), {})
        async def scenario():
            api._send_payload = AsyncMock(side_effect=[(True,'ok'), (False,'offline')])  # ABRP HTTP boundary
            marks = {}
            self.assertEqual(await api.async_send_india(status, charge, marks), (False,'offline'))
            self.assertEqual(marks, {'ordinary':200})
            api._send_payload = AsyncMock(return_value=(True,'ok'))
            await api.async_send_india(status, charge, marks)
            api._send_payload.assert_awaited_once()
            self.assertEqual(marks, {'ordinary':200, 'charge':100})
            api._send_payload.reset_mock()
            await api.async_send_india(status, charge, marks)
            api._send_payload.assert_not_awaited()
            status.gpsPosition.gps_status_decoded = 2
            api._send_payload = AsyncMock(side_effect=[ABRP.AbrpConnectionError('offline'),
                                                       (True, 'ok'), (True, 'ok')])
            marks = {}
            self.assertEqual(await api.async_send_india(status, charge, marks), (False, 'offline'))
            self.assertEqual(api._send_payload.await_count, 3)
            self.assertEqual(marks, {'charge': 100, 'gps': 200})
        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
