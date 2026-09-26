from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import job_store
from story_formatter import connect_library


class JobStoreTests(unittest.TestCase):
    def test_running_jobs_are_marked_interrupted_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            conn = connect_library(Path(td) / "story_library.sqlite3")
            try:
                run_id = job_store.start_run(conn, "content", ["python", "story_pipeline.py"])
                changed = job_store.mark_interrupted_runs(conn)
                self.assertEqual(changed, 1)
                row = conn.execute("SELECT state,finished_at,error FROM job_runs WHERE id=?", (run_id,)).fetchone()
                self.assertEqual(row["state"], "interrupted")
                self.assertTrue(row["finished_at"])
                self.assertIn("restarted", row["error"].lower())
                self.assertEqual(job_store.mark_interrupted_runs(conn), 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
