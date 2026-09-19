import json
import os
import sys
import tempfile
import types
import unittest
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
        main.CONFIG_FILE = Path(self.temp_dir.name) / "jobs.json"
        main.STATE_FILE = Path(self.temp_dir.name) / "state" / "scheduler.json"
        main._state = {}

    def tearDown(self):
        main.CONFIG_FILE = self.original_config
        main.STATE_FILE = self.original_state_file
        main._state = self.original_state

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

    def test_timed_job_success_is_persisted(self):
        main.record_timed_job_success("daily-job")

        persisted = json.loads(main.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn("daily-job", persisted)
        self.assertEqual(persisted, main._state)


if __name__ == "__main__":
    unittest.main()
