from __future__ import annotations

import http.server
import threading
import unittest

import research


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self._handle(head_only=True)

    def do_GET(self):
        self._handle(head_only=False)

    def _handle(self, head_only):
        if self.path == "/ok":
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(b"ok")
        elif self.path == "/listing-live":
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(b'<html>state: {\\"status\\":\\"active\\"} "price": 30000 </html>')
        elif self.path == "/listing-dead":
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(b'<html>state: {\\"status\\":\\"removed_by_user\\"} "price": 15000 </html>')
        elif self.path == "/head-405":
            if self.command == "HEAD":
                self.send_response(405)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                if not head_only:
                    self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):
        return


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=2)

    def test_extract_json_plain(self):
        self.assertEqual(research.extract_json('{"findings": []}'), {"findings": []})

    def test_extract_json_fenced(self):
        text = 'Here:\n```json\n{"tasks":[{"query":"x"}]}\n```'
        self.assertEqual(research.extract_json(text), {"tasks": [{"query": "x"}]})

    def test_extract_json_mixed(self):
        text = 'prefix\n{"findings":[{"title":"A"}]}\ntrailing'
        self.assertEqual(research.extract_json(text), {"findings": [{"title": "A"}]})

    def test_dedupe_same_url_ignores_tracking(self):
        items = [
            research.normalize_finding(
                {"title": "MacBook", "price": 100, "url": "https://example.com/item?utm_source=x"},
                "codex",
                "task-1",
                "r1",
            ),
            research.normalize_finding(
                {"title": "MacBook", "price": 90, "url": "https://example.com/item"},
                "gemini",
                "task-1",
                "r2",
            ),
        ]
        deduped = research.dedupe_findings(items)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["price"], 90.0)
        self.assertEqual(deduped[0]["source_models"], ["codex", "gemini"])

    def test_verify_url_ok_and_get_fallback(self):
        ok = research.verify_url(self.base_url + "/ok", timeout=2)
        self.assertTrue(ok["ok"])
        fallback = research.verify_url(self.base_url + "/head-405", timeout=2)
        self.assertTrue(fallback["ok"])
        self.assertEqual(fallback["method"], "GET")

    def test_verify_url_404_and_invalid(self):
        missing = research.verify_url(self.base_url + "/missing", timeout=2)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["reason"], "http_404")
        invalid = research.verify_url("not-a-url", timeout=2)
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["reason"], "invalid_url")

    def test_dedupe_same_olx_listing_across_language_prefixes(self):
        items = [
            research.normalize_finding(
                {"title": "MacBook", "price": 100, "url": "https://www.olx.ua/d/uk/obyavlenie/macbook-air-m2-ID10xQ56.html"},
                "codex",
                "task-1",
                "r1",
            ),
            research.normalize_finding(
                {"title": "MacBook Air", "price": 100, "url": "https://www.olx.ua/d/obyavlenie/macbook-air-m2-ID10xQ56.html?reason=extended"},
                "gemini",
                "task-2",
                "r2",
            ),
        ]
        deduped = research.dedupe_findings(items)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["source_models"], ["codex", "gemini"])

    def test_listing_key_marketplaces(self):
        self.assertEqual(
            research.listing_key("https://www.olx.ua/d/uk/obyavlenie/macbook-ID10xQ56.html"),
            "olx:10xq56",
        )
        self.assertEqual(
            research.listing_key("https://prom.ua/ua/p1788550677-macbook-air-m2.html"),
            "prom:1788550677",
        )
        self.assertIsNone(research.listing_key("https://example.com/item-ID123.html"))

    def test_price_dispute_detection(self):
        items = [
            research.normalize_finding(
                {"title": "MacBook", "price": 100, "url": "https://example.com/item"},
                "codex",
                "task-1",
                "r1",
            ),
            research.normalize_finding(
                {"title": "MacBook", "price": 130, "url": "https://example.com/item"},
                "gemini",
                "task-1",
                "r2",
            ),
        ]
        deduped = research.dedupe_findings(items)
        self.assertEqual(len(deduped), 1)
        self.assertTrue(deduped[0]["disputed"])
        self.assertEqual(len(deduped[0]["price_candidates"]), 2)
        self.assertEqual(deduped[0]["price"], 100.0)

    def test_close_prices_are_not_disputed(self):
        items = [
            research.normalize_finding(
                {"title": "MacBook", "price": 100, "url": "https://example.com/item"},
                "codex",
                "task-1",
                "r1",
            ),
            research.normalize_finding(
                {"title": "MacBook", "price": 104, "url": "https://example.com/item"},
                "gemini",
                "task-1",
                "r2",
            ),
        ]
        deduped = research.dedupe_findings(items)
        self.assertFalse(deduped[0]["disputed"])

    def test_off_site_rejection(self):
        finding = {"title": "A", "price": 1, "url": "https://rozetka.com.ua/item", "availability": "available"}
        reasons = research.rejection_reasons(finding, {"ok": True}, sites=["olx.ua"])
        self.assertIn("off_site", reasons)
        on_site = {"title": "A", "price": 1, "url": "https://www.olx.ua/d/obyavlenie/x-ID1.html", "availability": "available"}
        self.assertEqual(research.rejection_reasons(on_site, {"ok": True}, sites=["olx.ua"]), [])

    def test_shape_query_for_leg(self):
        self.assertEqual(
            research.shape_query_for_leg("macbook air m2 site:olx.ua", "codex", []),
            "macbook air m2",
        )
        self.assertEqual(
            research.shape_query_for_leg("macbook air m2", "gemini", ["olx.ua"]),
            "macbook air m2 site:olx.ua",
        )
        self.assertEqual(
            research.shape_query_for_leg("macbook site:olx.ua", "gemini", ["olx.ua"]),
            "macbook site:olx.ua",
        )

    def test_make_config_effort_and_sites(self):
        config = research.make_config("max", "https://www.OLX.ua/list, prom.ua")
        self.assertEqual(config["effort"], "max")
        self.assertEqual(config["effort_level"], 4)
        self.assertEqual(config["task_count"], 6)
        self.assertEqual(config["sites"], ["olx.ua", "prom.ua"])
        self.assertEqual(config["review_legs"], ["gemini", "claude"])
        self.assertEqual(config["claude_model"], "opus")

        default = research.make_config(None, None)
        self.assertEqual(default["effort"], "standard")
        self.assertEqual(default["sites"], [])
        self.assertEqual(default["claude_model"], "sonnet")

        named = research.make_config("3", None)
        self.assertEqual(named["effort"], "deep")
        self.assertTrue(named["adjudicate_disputes"])
        self.assertEqual(named["claude_model"], "opus")

    def test_parse_effort_clamps(self):
        self.assertEqual(research.parse_effort(0), 1)
        self.assertEqual(research.parse_effort(99), 4)
        self.assertEqual(research.parse_effort("quick"), 1)
        self.assertEqual(research.parse_effort("nonsense"), research.DEFAULT_EFFORT_LEVEL)

    def test_stale_run_detection(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            research.write_json(
                run_dir / "run.json",
                {"run_id": run_dir.name, "status": "running", "updated_at": "2020-01-01T00:00:00Z"},
            )
            meta = research.read_json(run_dir / "run.json", {})
            refreshed = research.refresh_stale_status(run_dir, meta)
            self.assertEqual(refreshed["status"], "failed")
            self.assertEqual(refreshed["phase"], "stale")

            research.write_json(
                run_dir / "run.json",
                {"run_id": run_dir.name, "status": "running", "updated_at": research.utc_now()},
            )
            meta = research.read_json(run_dir / "run.json", {})
            self.assertEqual(research.refresh_stale_status(run_dir, meta)["status"], "running")

    def test_live_listing_check_extracts_price_and_status(self):
        live = research.live_listing_check(self.base_url + "/listing-live")
        self.assertTrue(live["ok"])
        self.assertEqual(live["ad_status"], "active")
        self.assertEqual(live["live_price"], 30000.0)

        dead = research.live_listing_check(self.base_url + "/listing-dead")
        self.assertTrue(dead["ok"])
        self.assertEqual(dead["ad_status"], "removed_by_user")

        gone = research.live_listing_check(self.base_url + "/missing")
        self.assertFalse(gone["ok"])

    def test_apply_live_check_corrects_stale_price(self):
        from unittest import mock

        item = {
            "url": self.base_url + "/listing-live",
            "price": 20500.0,
            "disputed": True,
            "price_candidates": [{"price": 20500.0, "source_model": "codex"}],
        }
        with mock.patch.object(research, "listing_key", return_value="olx:test"):
            research.apply_live_check(item)
        self.assertEqual(item["price"], 30000.0)
        self.assertEqual(item["price_corrected_from"], 20500.0)
        self.assertFalse(item["disputed"])
        self.assertIn({"price": 30000.0, "source_model": "live_page"}, item["price_candidates"])

        inactive = {"url": self.base_url + "/listing-dead", "price": 15000.0}
        with mock.patch.object(research, "listing_key", return_value="olx:test2"):
            research.apply_live_check(inactive)
        self.assertTrue(inactive["listing_inactive"])
        self.assertIn("listing_inactive", research.rejection_reasons(inactive, {"ok": True}))
        self.assertFalse(research.is_rescuable({"reasons": ["listing_inactive"]}))

        # non-marketplace URLs are left untouched
        plain = {"url": self.base_url + "/listing-live", "price": 100.0}
        research.apply_live_check(plain)
        self.assertNotIn("live_check", plain)

    def test_is_rescuable(self):
        self.assertTrue(research.is_rescuable({"reasons": ["http_404", "missing_price"]}))
        self.assertTrue(research.is_rescuable({"reasons": ["timeout"]}))
        self.assertFalse(research.is_rescuable({"reasons": ["out_of_stock"]}))
        self.assertFalse(research.is_rescuable({"reasons": ["off_site", "missing_price"]}))
        self.assertFalse(research.is_rescuable({"reasons": ["parse_failed"]}))
        self.assertFalse(research.is_rescuable({}))

    def test_rescue_legs_rotate_across_rounds(self):
        import tempfile
        from unittest import mock

        item = {
            "title": "A",
            "url": "https://example.com/item",
            "reasons": ["http_404"],
            "source_model": "codex",
        }
        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium"):
            calls.append(leg)
            return {"success": True, "stdout": '{"findings": []}', "record_id": "x", "leg": leg, "task_id": task_id}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            attempts: dict[str, set[str]] = {}
            config = research.make_config("standard", None)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                records, dropped = research.run_rechecks("p", [item], run_dir, config, 1, attempts)
                self.assertEqual(calls, ["gemini"])
                records, dropped = research.run_rechecks("p", [item], run_dir, config, 2, attempts)
                self.assertEqual(calls, ["gemini", "codex"])
                records, dropped = research.run_rechecks("p", [item], run_dir, config, 3, attempts)
                self.assertEqual(calls, ["gemini", "codex"])
                self.assertEqual(records, [])

    def test_rescue_max_effort_uses_both_legs_and_counts_drops(self):
        import tempfile
        from unittest import mock

        items = [
            {"title": f"A{i}", "url": f"https://example.com/item{i}", "reasons": ["http_404"], "source_model": "codex"}
            for i in range(14)
        ]
        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium"):
            calls.append((leg, task_id))
            return {"success": True, "stdout": '{"findings": []}', "record_id": "x", "leg": leg, "task_id": task_id}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            config = research.make_config("max", None)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                records, dropped = research.run_rechecks("p", items, run_dir, config, 1, {})
        self.assertEqual(dropped, 2)  # 14 candidates, max profile caps at 12 per round
        self.assertEqual(len(records), 24)  # both legs per item
        first_item_legs = {leg for leg, task_id in calls if task_id.startswith("recheck-1-1-")}
        self.assertEqual(first_item_legs, {"codex", "gemini"})

    def test_leg_circuit_breaker(self):
        import tempfile

        run_id = "test-breaker-run"
        research.init_leg_health(run_id)
        try:
            for _ in range(research.BREAKER_THRESHOLD - 1):
                self.assertFalse(research.record_leg_result(run_id, "gemini", False))
            self.assertFalse(research.leg_disabled(run_id, "gemini"))
            self.assertTrue(research.record_leg_result(run_id, "gemini", False))
            self.assertTrue(research.leg_disabled(run_id, "gemini"))
            self.assertEqual(research.disabled_legs(run_id), ["gemini"])
            # success on another leg keeps it healthy; failures reset on success
            research.record_leg_result(run_id, "codex", False)
            research.record_leg_result(run_id, "codex", True)
            self.assertFalse(research.leg_disabled(run_id, "codex"))

            # disabled leg short-circuits call_model without spawning the script
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                (run_dir / "raw").mkdir(parents=True)
                record = research.call_model("gemini", "x", run_dir, "search", "t1")
                self.assertFalse(record["success"])
                self.assertTrue(record["skipped_by_breaker"])
        finally:
            research.clear_leg_health(run_id)

    def test_breaker_unknown_run_is_noop(self):
        self.assertFalse(research.record_leg_result("unknown-run", "gemini", False))
        self.assertFalse(research.leg_disabled("unknown-run", "gemini"))

    def test_force_disable_leg_on_quota(self):
        run_id = "test-quota-run"
        research.init_leg_health(run_id)
        try:
            self.assertTrue(research.force_disable_leg(run_id, "gemini", "quota_exhausted"))
            self.assertTrue(research.leg_disabled(run_id, "gemini"))
            self.assertFalse(research.force_disable_leg(run_id, "gemini", "quota_exhausted"))
        finally:
            research.clear_leg_health(run_id)
        self.assertFalse(research.force_disable_leg("unknown-run", "gemini", "x"))

    def test_leg_budget_consumption(self):
        import tempfile

        run_id = "test-budget-run"
        research.init_leg_health(run_id)
        research.init_leg_budget(run_id, {"gemini": 2})
        try:
            self.assertTrue(research.consume_leg_budget(run_id, "gemini"))
            self.assertTrue(research.consume_leg_budget(run_id, "gemini"))
            self.assertFalse(research.consume_leg_budget(run_id, "gemini"))
            # legs without a configured budget are unlimited
            self.assertTrue(research.consume_leg_budget(run_id, "codex"))
            self.assertTrue(research.consume_leg_budget("unknown-run", "gemini"))

            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                (run_dir / "raw").mkdir(parents=True)
                record = research.call_model("gemini", "x", run_dir, "search", "t1")
                self.assertFalse(record["success"])
                self.assertTrue(record["skipped_by_budget"])
        finally:
            research.clear_leg_health(run_id)
            research.clear_leg_budget(run_id)

    def test_straggler_drop_kills_slow_call_after_quorum(self):
        import concurrent.futures
        import subprocess
        import tempfile
        import time

        run_id = "test-straggler-run"

        def fast_job(latency):
            time.sleep(0.05)
            return {"record_id": f"fast-{latency}", "latency_sec": latency, "success": True}

        def slow_job():
            proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
            research.register_proc(run_id, "slow-rec", proc.pid)
            try:
                proc.wait()
            finally:
                research.unregister_proc(run_id, "slow-rec")
            return {"record_id": "slow-rec", "latency_sec": 30.0, "success": False}

        config = dict(research.make_config("quick", None))
        config["straggler_grace_sec"] = 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                run_dir.mkdir()
                started = time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [
                        executor.submit(fast_job, 0.1),
                        executor.submit(fast_job, 0.2),
                        executor.submit(fast_job, 0.3),
                        executor.submit(slow_job),
                    ]
                    records = research.collect_with_straggler_drop(futures, run_dir, config)
                elapsed = time.monotonic() - started
            self.assertEqual(len(records), 4)
            self.assertLess(elapsed, 15.0)  # slow 30s call was killed, not awaited
            self.assertTrue(research.was_dropped_as_straggler(run_id, "slow-rec"))
        finally:
            research.clear_run_registry(run_id)

    def test_rejection_rules(self):
        parsed = {"parse_failed": True}
        self.assertEqual(research.rejection_reasons(parsed), ["parse_failed"])

        missing_url = {"title": "A", "price": 1, "availability": "available"}
        self.assertIn("missing_url", research.rejection_reasons(missing_url))

        missing_price = {"title": "A", "url": "https://example.com", "availability": "available"}
        self.assertIn("missing_price", research.rejection_reasons(missing_price, {"ok": True}))

        sold = {"title": "A", "price": 1, "url": "https://example.com", "availability": "sold"}
        self.assertIn("out_of_stock", research.rejection_reasons(sold, {"ok": True}))


if __name__ == "__main__":
    unittest.main()
