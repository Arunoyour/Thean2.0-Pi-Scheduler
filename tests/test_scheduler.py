import json
import os
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    requests_stub = types.ModuleType("requests")
    requests_stub.post = None
    requests_stub.exceptions = types.SimpleNamespace(
        ConnectTimeout=type("ConnectTimeout", (Exception,), {}),
        ReadTimeout=type("ReadTimeout", (Exception,), {}),
        ConnectionError=type("ConnectionError", (Exception,), {}),
        RequestException=type("RequestException", (Exception,), {}),
    )
    sys.modules["requests"] = requests_stub

import main


class SchedulerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.original_config = main.CONFIG_FILE
        self.original_state_file = main.STATE_FILE
        self.original_state = main._state
        self.original_job_status = main._job_status
        self.original_heartbeat_file = main.HEARTBEAT_FILE
        main.CONFIG_FILE = Path(self.temp_dir.name) / "jobs.json"
        main.STATE_FILE = Path(self.temp_dir.name) / "state" / "scheduler.json"
        main._state = {}
        main._job_status = {}
        main.HEARTBEAT_FILE = Path(self.temp_dir.name) / "heartbeat"

    def tearDown(self):
        main.CONFIG_FILE = self.original_config
        main.STATE_FILE = self.original_state_file
        main._state = self.original_state
        main._job_status = self.original_job_status
        main.HEARTBEAT_FILE = self.original_heartbeat_file

    def write_config(self, data):
        main.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")

    def test_load_config_expands_environment_and_merges_project_headers(self):
        self.write_config({
            "project_headers": {
                "THEAN": {
                    "X-Cron-Secret": "${TEST_CRON_SECRET}",
                    "X-Shared": "project",
                }
            },
            "jobs": [{
                "project": "THEAN",
                "name": "daily-job",
                "url": "${TEST_BASE_URL}/cron/jobs/daily-job/run",
                "run_at": "01:00",
                "catch_up": True,
                "headers": {"X-Shared": "job"},
            }],
        })

        with patch.dict(os.environ, {
            "TEST_CRON_SECRET": "secret-value",
            "TEST_BASE_URL": "http://backend:8000/api/v1",
        }):
            jobs = main.load_config()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0]["url"],
            "http://backend:8000/api/v1/cron/jobs/daily-job/run",
        )
        self.assertEqual(jobs[0]["headers"]["X-Cron-Secret"], "secret-value")
        self.assertEqual(jobs[0]["headers"]["X-Shared"], "job")
        self.assertTrue(jobs[0]["catch_up"])

    def test_production_config_loads_all_jobs(self):
        main.CONFIG_FILE = Path(main.__file__).with_name("jobs.json")

        with patch.dict(os.environ, {
            "THEAN_CRON_SECRET": "x" * 64,
            "THEAN_CRON_BASE_URL": "http://backend:8000/api/v1",
        }):
            jobs = main.load_config()

        self.assertEqual(len(jobs), 25)
        self.assertEqual(sum(job["project"] == "THEAN" for job in jobs), 15)
        self.assertEqual(sum(job["project"] == "COCO CABS" for job in jobs), 10)
        self.assertTrue(all(
            job["headers"].get("X-Cron-Secret") == "x" * 64
            for job in jobs if job["project"] == "THEAN"
        ))

    def test_load_config_rejects_unresolved_environment_variables(self):
        self.write_config({
            "jobs": [{
                "name": "job",
                "url": "${MISSING_SCHEDULER_TEST_URL}",
                "interval_seconds": 60,
            }]
        })

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISSING_SCHEDULER_TEST_URL", None)
            with self.assertRaises(SystemExit):
                main.load_config()

    def test_disabled_job_does_not_require_secrets_or_run(self):
        self.write_config({"jobs": [
            {"name": "regular", "url": "https://example.test", "interval_seconds": 60},
            {"name": "cleanup", "enabled": False, "url": "${MISSING_CLEANUP_URL}",
             "headers": {"X-Coco-Promo-Cleanup-Secret": "${MISSING_CLEANUP_SECRET}"}, "interval_seconds": 300},
        ]})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISSING_CLEANUP_URL", None)
            os.environ.pop("MISSING_CLEANUP_SECRET", None)
            self.assertEqual([job["name"] for job in main.load_config()], ["regular"])

    def test_enabling_cleanup_requires_its_configuration(self):
        self.write_config({"jobs": [{"name": "cleanup", "enabled": True,
            "url": "${MISSING_CLEANUP_URL}", "interval_seconds": 300}]})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISSING_CLEANUP_URL", None)
            with self.assertRaises(SystemExit): main.load_config()

    def test_enabled_must_be_boolean(self):
        self.write_config({"jobs": [{"name": "cleanup", "enabled": "false",
            "url": "https://example.test", "interval_seconds": 300}]})
        with self.assertRaises(SystemExit): main.load_config()

    def test_cleanup_requires_https_and_secret_before_activation(self):
        job = {"name": "PromoReservationCleanup", "enabled": True,
               "url": "http://example.test/cleanup", "interval_seconds": 300,
               "headers": {"X-Coco-Promo-Cleanup-Secret": "x" * 32}}
        self.write_config({"jobs": [job]})
        with self.assertRaises(SystemExit): main.load_config()
        job["url"] = "https://example.test/cleanup"
        job["headers"]["X-Coco-Promo-Cleanup-Secret"] = "short"
        self.write_config({"jobs": [job]})
        with self.assertRaises(SystemExit): main.load_config()
        job["headers"]["X-Coco-Promo-Cleanup-Secret"] = "x" * 32
        self.write_config({"jobs": [job]})
        self.assertEqual(main.load_config()[0]["name"], "PromoReservationCleanup")

    def test_business_status_is_required_only_for_opted_in_jobs(self):
        job = {"url": "http://local/assignment", "body": None, "headers": {},
               "connect_timeout": 3, "read_timeout": 8, "require_business_success": True}
        for payload in ({"status_cd": False, "status_Desc": "SQL failed"},
                        {"status_cd": None}, {"status_cd": "true"}, {}, [], {"status_cd": 1}):
            with self.subTest(payload=payload):
                response = unittest.mock.Mock(status_code=200, text="synthetic", reason="OK")
                response.json.return_value = payload
                with patch.object(main.requests, "post", return_value=response):
                    self.assertFalse(main.http_post_once(job)[0])
        for key in ("status_cd", "Status_cd"):
            response.json.return_value = {key: True}
            with patch.object(main.requests, "post", return_value=response):
                self.assertTrue(main.http_post_once(job)[0])
        response.json.side_effect = ValueError("invalid")
        with patch.object(main.requests, "post", return_value=response):
            self.assertFalse(main.http_post_once(job)[0])
            self.assertTrue(main.http_post_once({**job, "require_business_success": False})[0])
        response.status_code = 500
        with patch.object(main.requests, "post", return_value=response):
            self.assertFalse(main.http_post_once(job)[0])

    def test_coco_assignment_and_cancellation_jobs_require_business_success(self):
        config = json.loads((Path(main.__file__).parent / "jobs.json").read_text(encoding="utf-8"))
        jobs = config["jobs"]
        expected = {"AssignScheduledTrips", "AutoCancelNoShowTripGhost", "AutoCancelAfterWaitingTime"}
        enabled_jobs = {job["name"] for job in jobs if job.get("enabled", True)}
        self.assertTrue(expected <= enabled_jobs)
        required = {job["name"] for job in jobs if job.get("require_business_success")}
        self.assertTrue(expected <= required)

    def test_promo_cleanup_requires_business_success_when_enabled(self):
        config = json.loads((Path(main.__file__).parent / "jobs.json").read_text(encoding="utf-8"))
        jobs = config["jobs"]
        cleanup = next(j for j in jobs if j["name"] == "PromoReservationCleanup")
        self.assertIs(cleanup["enabled"], False)
        self.assertEqual(cleanup["interval_seconds"], 300)
        self.assertIs(cleanup["require_business_success"], True)

    def test_http_post_once_passes_headers_and_timeouts(self):
        job = {
            "url": "http://backend:8000/api/v1/cron/jobs/job/run",
            "body": None,
            "headers": {"X-Cron-Secret": "secret-value"},
            "connect_timeout": 3,
            "read_timeout": 8,
        }
        response = unittest.mock.Mock(status_code=200, text="", reason="OK")

        with patch.object(main.requests, "post", return_value=response) as post:
            result = main.http_post_once(job)

        self.assertEqual(result, (True, None, 200, None))
        post.assert_called_once_with(
            job["url"],
            json=None,
            headers={"X-Cron-Secret": "secret-value"},
            timeout=(3, 8),
        )

    def test_cleanup_refuses_redirects_and_checks_business_result(self):
        job = {"name": "PromoReservationCleanup", "url": "https://example.test/cleanup",
               "body": None, "headers": {"X-Coco-Promo-Cleanup-Secret": "x" * 32},
               "connect_timeout": 5, "read_timeout": 120, "require_business_success": True}
        response = unittest.mock.Mock(status_code=302, text="redirect", reason="Found")
        with patch.object(main.requests, "post", return_value=response) as post:
            self.assertFalse(main.http_post_once(job)[0])
            self.assertIs(post.call_args.kwargs["allow_redirects"], False)
        response.status_code = 200
        response.json.return_value = {"status_cd": False}
        with patch.object(main.requests, "post", return_value=response):
            self.assertFalse(main.http_post_once(job)[0])
        response.json.return_value = {"status_cd": True, "released": 1}
        with patch.object(main.requests, "post", return_value=response):
            self.assertTrue(main.http_post_once(job)[0])

    def test_timed_job_success_is_persisted(self):
        main.record_timed_job_success("daily-job")

        persisted = json.loads(main.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn("daily-job", persisted)
        self.assertEqual(persisted, main._state)

    def test_run_job_exposes_success_status(self):
        job = {
            "project": "COCO CABS",
            "name": "poll-job",
            "interval_seconds": 15,
            "run_at": None,
            "retry_count": 1,
            "retry_delay": 0,
        }
        main.register_jobs([job])

        with patch.object(main, "http_post_once", return_value=(True, None, 204, None)):
            main.run_job(job)

        status = main._job_status["poll-job"]
        self.assertEqual(status["status"], "Success")
        self.assertEqual(status["http_status"], 204)
        self.assertEqual(status["success_count"], 1)
        self.assertIsNotNone(status["last_started_at"])
        self.assertIsNotNone(status["last_completed_at"])

    def test_status_payload_filters_no_secrets_from_job_config(self):
        job = {
            "project": "THEAN",
            "name": "private-job",
            "interval_seconds": 60,
            "run_at": None,
            "headers": {"X-Cron-Secret": "must-not-leak"},
            "url": "http://private/api",
        }
        main.register_jobs([job])

        payload = main.build_status_payload()
        serialized = json.dumps(payload)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("http://private/api", serialized)

    def test_status_http_endpoint_returns_live_job_data(self):
        main.HEARTBEAT_FILE.write_text("alive", encoding="ascii")
        main.register_jobs([{
            "project": "COCO CABS",
            "name": "poll-job",
            "interval_seconds": 15,
            "run_at": None,
        }])
        server = main.ThreadingHTTPServer(("127.0.0.1", 0), main.StatusRequestHandler)
        thread = threading.Thread(target=server.handle_request)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/status", timeout=2
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "healthy")
            self.assertEqual(payload["jobs"][0]["name"], "poll-job")
        finally:
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
