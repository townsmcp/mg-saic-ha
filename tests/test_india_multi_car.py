# File: tests/test_india_multi_car.py
#
# Two cars on one India login share one backend and one real MgIndiaClient.
# Each car's VIN and PIN hash must be bound (via for_vehicle) while its
# command or status runs. Does NOT stub mg_ismart_india_client.

import asyncio
import unittest
from unittest.mock import patch

import test_india_client_contract as C

PIN_A, PIN_B, PIN_DEFAULT = "a" * 32, "b" * 32, "d" * 32


@unittest.skipUnless(C.CLIENT_INSTALLED, "mg-ismart-india-client is not installed")
class TestIndiaMultiCar(unittest.TestCase):
    def _run(self, make):
        from aiohttp import ClientSession
        from mg_ismart_india_client import MgIndiaClient

        async def scenario():
            backend = C.INDIA.IndiaBackend(
                "9999999999", "pw", vin="VINA", pin_hash=PIN_DEFAULT
            )
            backend.set_api_lock(asyncio.Lock())
            backend.set_vehicle_pin_hash("VINA", PIN_A)
            backend.set_vehicle_pin_hash("VINB", PIN_B)
            async with ClientSession() as session:
                client = MgIndiaClient(
                    session, "9999999999", "pw", vin="VINA", pin_hash=PIN_DEFAULT
                )
                backend._client = client
                seen = []

                async def record(*args, **kwargs):
                    seen.append((client.vin, client.pin_hash))

                await make(backend, client, record)
                return seen

        return C._run(scenario())

    def test_command_binds_that_cars_vin_and_pin(self):
        async def go(backend, client, record):
            with patch.object(client, "control_door_lock", record):
                await backend.lock_vehicle("VINB")
                await backend.lock_vehicle("VINA")

        self.assertEqual(self._run(go), [("VINB", PIN_B), ("VINA", PIN_A)])

    def test_status_binds_that_cars_vin_and_pin(self):
        async def go(backend, client, record):
            async def status(**kwargs):
                await record()
                raise RuntimeError("stop after recording")

            with patch.object(client, "status", status):
                with self.assertRaises(RuntimeError):
                    await backend.get_vehicle_status("VINA")

        self.assertEqual(self._run(go), [("VINA", PIN_A)])

    def test_unregistered_vin_falls_back_to_constructor_pin(self):
        async def go(backend, client, record):
            with patch.object(client, "control_door_lock", record):
                await backend.lock_vehicle("VINC")

        self.assertEqual(self._run(go), [("VINC", PIN_DEFAULT)])


if __name__ == "__main__":
    unittest.main()
