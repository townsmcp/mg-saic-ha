"""Personal details are masked in everything the integration logs.

People attach debug logs to public issues. The log in #401 carried the
account's email address, two full VINs and the car's GPS position; at the SAIC
library's debug level the login reply (access and refresh tokens) is logged
too. log_redaction.py masks all of it, always, for the integration's logger
and the SAIC libraries' loggers.

The lines below are shaped like real ones, with made-up values.
"""

import json
import logging
import subprocess
import sys
import unittest
from pathlib import Path

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

REDACTION = sys.modules["mg_saic.log_redaction"]
BACKENDS = sys.modules["mg_saic.backends"]
CONST = sys.modules["mg_saic.const"]
REPO_ROOT = Path(__file__).resolve().parent.parent

VIN = "LSJA24U61PN000123"
OTHER_VIN = "LSJW74U99PZ000456"
EMAIL = "driver@example.com"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0.s3cr3t"

LOGIN_REPLY = (
    'Response code: 0 {"code":0,"data":{"access_token":"%s","account":"%s",'
    '"expires_in":2591999,"jti":"0f8fad5b-d9cb-469f-a165-70867728950e",'
    '"refresh_token":"%s-refresh","token_type":"bearer","user_id":"1234567",'
    '"user_name":"%s","tenant_id":"459771"},"message":"success"}'
) % (TOKEN, EMAIL, TOKEN, EMAIL)

STATUS_REPLY = (
    'Response code: 0 {"code":0,"data":{"basicVehicleStatus":{"interiorTemperature":19,'
    '"remoteClimateStatus":2,"lockStatus":1},"gpsPosition":{"gpsStatus":3,"timeStamp":1790884969,'
    '"wayPoint":{"heading":21,"position":{"altitude":42,"latitude":51117513,'
    '"longitude":891696},"speed":0}},"statusTime":1790884969}}'
)

STATUS_REPR = (
    "Vehicle Status: VehicleStatusResp(basicVehicleStatus=BasicVehicleStatus("
    "interiorTemperature=19, remoteClimateStatus=2), gpsPosition=GpsPosition("
    "gpsStatus=3, timeStamp=1790884969, wayPoint=WayPoint(heading=21, "
    "position=Position(altitude=42, latitude=51117513, longitude=891696), "
    "speed=0)), statusTime=1790884969)"
)

INFO_REPR = (
    "Vehicle Info: [VinInfo(bindTime=1762553597000, brandName='MG', "
    "modelName='MG5 Electric', modelYear='2022', name=None, series='EP22 UK', "
    f"vin='{VIN}', vehicleModelConfiguration=[VehicleModelConfiguration("
    "itemCode='SA64', itemName='SA64', itemValue="
    "'0111110000000000001000000100101000000010100000000000000000000110')])]"
)


class RedactTests(unittest.TestCase):
    def test_login_reply_loses_its_tokens_and_identity(self):
        out = REDACTION.redact(LOGIN_REPLY)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(EMAIL, out)
        self.assertNotIn("0f8fad5b", out)
        self.assertNotIn("1234567", out)
        self.assertIn('"access_token":"***"', out)
        self.assertIn('"refresh_token":"***"', out)
        # Still valid JSON after the prefix, and the useful parts survive.
        body = json.loads(out.split(" ", 3)[3])
        self.assertEqual(body["data"]["expires_in"], 2591999)
        self.assertEqual(body["data"]["tenant_id"], "459771")
        self.assertEqual(body["message"], "success")

    def test_position_is_masked_in_saics_reply_and_in_our_own_line(self):
        for line in (STATUS_REPLY, STATUS_REPR):
            out = REDACTION.redact(line)
            self.assertNotIn("51117513", out, line)
            self.assertNotIn("891696", out, line)
            self.assertIn("***", out)

    def test_the_rest_of_a_status_line_is_untouched(self):
        out = REDACTION.redact(STATUS_REPR)
        for kept in (
            "interiorTemperature=19",
            "remoteClimateStatus=2",
            "altitude=42",
            "heading=21",
            "timeStamp=1790884969",
            "statusTime=1790884969",
        ):
            self.assertIn(kept, out)

    def test_vin_keeps_only_its_last_four(self):
        out = REDACTION.redact(INFO_REPR)
        self.assertNotIn(VIN, out)
        self.assertIn("vin='…0123'", out)
        # Model details and the 64-character feature bitmask are not VINs.
        self.assertIn("series='EP22 UK'", out)
        self.assertIn(
            "'0111110000000000001000000100101000000010100000000000000000000110'", out
        )

    def test_a_vin_never_registered_is_still_masked(self):
        # #401: a second car on the account, named in an error.
        out = REDACTION.redact(
            f"MG SAIC API did not respond within 30s at startup for VIN {OTHER_VIN} "
            "— HA will retry automatically in the background."
        )
        self.assertNotIn(OTHER_VIN, out)
        self.assertIn("for VIN …0456", out)

    def test_email_in_an_account_key(self):
        out = REDACTION.redact(
            f"AccountPoller ('{EMAIL}', 'EU'): no new messages"
        )
        self.assertEqual(out, "AccountPoller ('***@***', 'EU'): no new messages")

    def test_command_error_lines_are_left_alone(self):
        line = (
            "API call failed: return code: 4, message: The remote control "
            "instruction failed, please try again later., event_id: 1254463881"
        )
        self.assertEqual(REDACTION.redact(line), line)

    def test_climate_and_timing_lines_are_left_alone(self):
        for line in (
            "Climate params - Idx: 11, Fan speed: 2, AC On: True",
            "Detected activity for remoteClimateStatus: previous=0, current=2",
            "Update intervals initialized: Default: 0:30:00, Charging: 0:05:00",
            "Finished fetching MG SAIC data update coordinator data in 7.987 "
            "seconds (success: True)",
            "State updated: Is Powered On: False, Last Powered On Time: "
            "2026-09-30 19:58:50.985608+00:00",
        ):
            self.assertEqual(REDACTION.redact(line), line)

    def test_empty_values_are_kept(self):
        # "No token" is worth seeing and gives nothing away.
        for line in ("access_token=None", '"refresh_token":null', "user_name=''"):
            self.assertEqual(REDACTION.redact(line), line)

    def test_token_in_a_url_query(self):
        out = REDACTION.redact(
            "GET https://api.example.org/1/tlm/send?token=abc123&tlm=%7B%7D failed"
        )
        self.assertNotIn("abc123", out)
        self.assertIn("token=***&tlm=", out)

    def test_masking_twice_changes_nothing_more(self):
        for line in (LOGIN_REPLY, STATUS_REPR, INFO_REPR):
            once = REDACTION.redact(line)
            self.assertEqual(REDACTION.redact(once), once)


class KnownValueTests(unittest.TestCase):
    """This installation's own VIN and account, wherever they turn up."""

    def test_vin_inside_a_unique_id(self):
        REDACTION.register_vin(VIN)
        out = REDACTION.redact(f"Entity 01JABC_{VIN}_climate already exists")
        self.assertEqual(out, "Entity 01JABC_…0123_climate already exists")

    def test_vin_in_lower_case(self):
        REDACTION.register_vin(VIN)
        self.assertNotIn(VIN.lower(), REDACTION.redact(f"topic mg/{VIN.lower()}/soc"))

    def test_phone_number_account(self):
        # A phone number looks like any other number until it's registered.
        REDACTION.register_account("447700900123")
        out = REDACTION.redact("Login successful for account ('447700900123', 'EU')")
        self.assertEqual(out, "Login successful for account ('***', 'EU')")

    def test_short_values_are_not_registered(self):
        REDACTION.register_account("me")
        self.assertEqual(REDACTION.redact("some message"), "some message")

    def test_nothing_to_register(self):
        REDACTION.register_vin(None)
        REDACTION.register_account(None)
        self.assertEqual(REDACTION.redact("plain"), "plain")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []
        self.setFormatter(logging.Formatter("%(name)s %(message)s"))

    def emit(self, record):
        self.lines.append(self.format(record))


class FilterTests(unittest.TestCase):
    """Through real loggers, as Home Assistant's handlers see the lines."""

    def setUp(self):
        self.capture = _Capture()
        logging.getLogger().addHandler(self.capture)
        self.addCleanup(logging.getLogger().removeHandler, self.capture)

    def _logger(self, name):
        logger = logging.getLogger(name)
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, previous)
        return logger

    def test_integration_logger_is_masked(self):
        # Importing the integration installs the filter on its own logger.
        self._logger(CONST.LOGGER.name).debug(
            "Reusing existing shared client for account %s (adding VIN: %s)",
            (EMAIL, "EU"),
            VIN,
        )
        line = self.capture.lines[-1]
        self.assertNotIn(EMAIL, line)
        self.assertNotIn(VIN, line)
        self.assertIn("(adding VIN: …0123)", line)

    def test_library_child_loggers_are_masked(self):
        # A filter on a logger doesn't see its children's records, so each
        # child needs the filter itself.
        child = self._logger("saic_ismart_client_ng.api.base")
        REDACTION.install_log_redaction(CONST.LOGGER.name)
        child.debug("Response code: %s %s", 0, LOGIN_REPLY.split(" ", 3)[3])
        line = self.capture.lines[-1]
        self.assertNotIn(TOKEN, line)
        self.assertIn('"access_token":"***"', line)

    def test_a_logger_created_later_is_picked_up_by_the_next_install(self):
        late = self._logger(f"{CONST.LOGGER.name}.created_later")
        REDACTION.install_log_redaction(CONST.LOGGER.name)
        late.debug("position latitude=51117513")
        self.assertIn("latitude=***", self.capture.lines[-1])

    def test_install_is_idempotent(self):
        logger = logging.getLogger(CONST.LOGGER.name)
        REDACTION.install_log_redaction(CONST.LOGGER.name)
        REDACTION.install_log_redaction(CONST.LOGGER.name)
        filters = [f for f in logger.filters if isinstance(f, REDACTION.RedactingFilter)]
        self.assertEqual(len(filters), 1)

    def test_other_loggers_are_not_touched(self):
        other = self._logger("some.other.integration")
        other.debug("vin=%s", VIN)
        self.assertIn(VIN, self.capture.lines[-1])

    def test_traceback_is_masked(self):
        logger = self._logger(CONST.LOGGER.name)
        try:
            raise RuntimeError(f"no answer for VIN {OTHER_VIN} ({EMAIL})")
        except RuntimeError:
            logger.exception("Setup failed")
        line = self.capture.lines[-1]
        self.assertIn("RuntimeError", line)
        self.assertNotIn(OTHER_VIN, line)
        self.assertNotIn(EMAIL, line)

    def test_a_line_that_cannot_be_rendered_is_logged_as_passed(self):
        # mg-saic-client 0.9.6, on every retry: "%d" with a text event id.
        # Without help, logging prints "--- Logging error ---" and a call
        # stack instead of the line (seen 22 times in 2000 log lines on
        # 2026-10-02).
        self._logger("saic_ismart_client_ng.api.base")
        REDACTION.install_log_redaction(CONST.LOGGER.name)
        record = logging.LogRecord(
            "saic_ismart_client_ng.api.base", logging.DEBUG, __file__, 1,
            "Updating event_id to the newly obtained value %d", ("688443730",), None,
        )
        self.assertTrue(REDACTION.RedactingFilter().filter(record))
        self.assertEqual(
            record.getMessage(),
            "Updating event_id to the newly obtained value %d ('688443730',)",
        )

    def test_an_unrenderable_line_is_still_masked(self):
        record = logging.LogRecord(
            CONST.LOGGER.name, logging.DEBUG, __file__, 1, "%d", (EMAIL,), None
        )
        REDACTION.RedactingFilter().filter(record)
        self.assertNotIn(EMAIL, record.getMessage())


class WiringTests(unittest.TestCase):
    def test_creating_a_backend_registers_the_account_and_car(self):
        phone, vin = "447700900999", "LSJA24U61PN000789"
        BACKENDS.create_backend(
            {
                "username": phone,
                "password": "hunter2",
                "vin": vin,
                "region": "EU",
                "country_code": "44",
            }
        )
        out = REDACTION.redact(f"entry {phone} unique id x_{vin}_lock")
        self.assertEqual(out, "entry *** unique id x_…0789_lock")

    def test_setup_module_installs_the_filter(self):
        logger = logging.getLogger(CONST.LOGGER.name)
        self.assertTrue(
            any(isinstance(f, REDACTION.RedactingFilter) for f in logger.filters)
        )


_REAL_LIBRARY = """
import importlib.util, json, logging, sys
import saic_ismart_client_ng.api.base  # creates the library's real loggers
import saic_ismart_client_ng.net.client
spec = importlib.util.spec_from_file_location("log_redaction", sys.argv[1])
redaction = importlib.util.module_from_spec(spec); spec.loader.exec_module(redaction)
redaction.install_log_redaction("custom_components.mg_saic")
names = [n for n, l in logging.root.manager.loggerDict.items()
         if isinstance(l, logging.Logger) and n.startswith("saic_ismart_client_ng")]
unfiltered = [n for n in names if not any(
    isinstance(f, redaction.RedactingFilter) for f in logging.getLogger(n).filters)]
lines = []
class Capture(logging.Handler):
    def emit(self, record):
        lines.append(record.getMessage())
logging.getLogger().addHandler(Capture())
base = logging.getLogger("saic_ismart_client_ng.api.base")
base.setLevel(logging.DEBUG)
base.debug("Response code: %s %s", 0, sys.argv[2])
print(json.dumps({"loggers": len(names), "unfiltered": unfiltered, "line": lines[-1]}))
"""


class RealLibraryTests(unittest.TestCase):
    """Every logger the real mg-saic-client creates gets the filter."""

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _REAL_LIBRARY,
                str(REPO_ROOT / "custom_components/mg_saic/log_redaction.py"),
                LOGIN_REPLY.split(" ", 3)[3],
            ],
            capture_output=True, text=True, timeout=60,
        )
        cls.out = json.loads(result.stdout) if result.returncode == 0 else None
        cls.err = result.stderr

    def setUp(self):
        if self.out is None:
            if "No module named 'saic_ismart_client_ng'" in self.err:
                self.skipTest("mg-saic-client not installed")
            self.fail(f"library check failed:\n{self.err}")

    def test_all_library_loggers_are_filtered(self):
        self.assertGreater(self.out["loggers"], 3)
        self.assertEqual(self.out["unfiltered"], [])

    def test_login_reply_is_masked_on_the_librarys_own_logger(self):
        self.assertNotIn(TOKEN, self.out["line"])
        self.assertNotIn(EMAIL, self.out["line"])


if __name__ == "__main__":
    unittest.main()
