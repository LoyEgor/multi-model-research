from __future__ import annotations

import http.server
import threading
import time
import unittest

import research


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: dict = {}                # request path -> count of GET requests served
    captured_headers: dict = {}    # request path -> {header: value} snapshot
    _lock = threading.Lock()

    @classmethod
    def reset(cls, *paths):
        with cls._lock:
            for p in paths:
                cls.hits.pop(p, None)
                cls.captured_headers.pop(p, None)

    def _bump_get(self):
        with _Handler._lock:
            _Handler.hits[self.path] = _Handler.hits.get(self.path, 0) + 1
            return _Handler.hits[self.path]

    def do_HEAD(self):
        self._handle(head_only=True)

    def do_GET(self):
        self._handle(head_only=False)

    def _handle(self, head_only):
        if self.path == "/hdr":
            with _Handler._lock:
                _Handler.captured_headers[self.path] = {
                    "User-Agent": self.headers.get("User-Agent"),
                    "Accept": self.headers.get("Accept"),
                    "Accept-Language": self.headers.get("Accept-Language"),
                }
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(b"ok")
            return
        if self.path == "/bot-then-ok":
            # 429 (with Retry-After) on the first GET, then 200 — the retry must recover it.
            if head_only:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            if self._bump_get() < 2:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"recovered")
            return
        if self.path == "/bot-forever":
            if not head_only:
                self._bump_get()
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            return
        if self.path == "/cf-403":
            # A real Cloudflare CHALLENGE: cf-mitigated: challenge header + challenge body markers.
            if not head_only:
                self._bump_get()
            self.send_response(403)
            self.send_header("Server", "cloudflare")
            self.send_header("cf-ray", "abc123-IAD")
            self.send_header("cf-mitigated", "challenge")
            self.end_headers()
            if not head_only:
                self.wfile.write(b"<html><body>Attention Required! | Cloudflare. Please enable "
                                 b"javascript and cookies</body></html>")
            return
        if self.path == "/cf-plain-403":
            # CF-proxied but a genuine origin 403: edge headers present, normal body, NO challenge.
            if not head_only:
                self._bump_get()
            self.send_response(403)
            self.send_header("Server", "cloudflare")
            self.send_header("cf-ray", "def456-IAD")
            self.end_headers()
            if not head_only:
                self.wfile.write(b"x" * 1200)  # normal-size origin 403, no markers -> not a wall
            return
        if self.path == "/plain-403":
            if not head_only:
                self._bump_get()
            self.send_response(403)
            self.end_headers()
            if not head_only:
                self.wfile.write(b"x" * 800)  # large, no markers -> a genuine 403, not a bot wall
            return
        if self.path == "/verify-recover":
            # First verify_url call fails hard (500, not retried); a later call gets 200. Proves the
            # run URL cache does NOT memoize a transient failure (rescue can re-fetch and recover).
            if head_only:
                self.send_response(405)
                self.end_headers()
                return
            if self._bump_get() < 2:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/count-404":
            if not head_only:
                self._bump_get()
            self.send_response(404)
            self.end_headers()
            return
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
        elif self.path == "/ldjson":
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(
                    b'<html><script type="application/ld+json">'
                    b'{"@type":"Product","name":"X","offers":{"@type":"Offer","price":"499","priceCurrency":"USD","availability":"https://schema.org/InStock"}}'
                    b'</script></html>'
                )
        elif self.path == "/repurposed":
            # slug claims a macbook, but the live page is a Dyson straightener (active, priced)
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(b'<html><head><title>Vypryamlyach Dyson Airstrait original</title></head>'
                                 b'<body>state: {\\"status\\":\\"active\\"} "price": 17500 </body></html>')
        elif self.path == "/variants":
            # bundled multi-tier listing: Pro / Max 5x / Max 20x each priced
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(b'<html><head><title>Claude AI Pro Max 5x Max 20x account</title></head>'
                                 b'<body>state: {\\"status\\":\\"active\\"} Tariffs: Pro - 12$  Max 5x - 26$  Max 20x - 40$</body></html>')
        elif self.path == "/macbook-ok":
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(b'<html><head><title>MacBook Air M2 2022 8/256 Space Gray</title></head>'
                                 b'<body>state: {\\"status\\":\\"active\\"} "price": 25000 </body></html>')
        elif self.path == "/og":
            self.send_response(200)
            self.end_headers()
            if not head_only:
                self.wfile.write(
                    b'<html><head><meta property="product:price:amount" content="42.50">'
                    b'<meta property="product:price:currency" content="EUR"></head></html>'
                )
        elif self.path == "/head-405":
            if self.command == "HEAD":
                self.send_response(405)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                if not head_only:
                    self.wfile.write(b"ok")
        elif self.path in ("/count-verify", "/count-slow"):
            # HEAD 405 forces verify_url onto its GET path (the only path that bumps the hit count),
            # so a prefetch that already GET'd this URL means the batch verify must add zero GETs.
            if head_only:
                self.send_response(405)
                self.end_headers()
                return
            if self.path == "/count-slow":
                time.sleep(0.2)  # widen the window so concurrent single-flight callers pile on one fetch
            self._bump_get()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path == "/count-live":
            # live_listing_check is GET-only; count every page fetch so the cache's single fetch shows.
            if head_only:
                self.send_response(405)
                self.end_headers()
                return
            self._bump_get()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<html>state: {\\"status\\":\\"active\\"} "price": 30000 </html>')
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

    def test_verify_fetch_sends_browser_headers(self):
        research.verify_url(self.base_url + "/hdr", timeout=2)
        sent = _Handler.captured_headers.get("/hdr") or {}
        self.assertEqual(sent.get("User-Agent"), research.BROWSER_UA)
        self.assertIn("Chrome", research.BROWSER_UA)
        self.assertEqual(sent.get("User-Agent"), research.BROWSER_HEADERS["User-Agent"])
        self.assertTrue(sent.get("Accept"))
        self.assertTrue(sent.get("Accept-Language"))

    def test_verify_url_retries_bot_block_then_succeeds(self):
        from unittest import mock
        _Handler.reset("/bot-then-ok")
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            res = research.verify_url(self.base_url + "/bot-then-ok", timeout=2)
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(_Handler.hits.get("/bot-then-ok", 0), 2)  # first GET 429, retry 200

    def test_verify_url_persistent_bot_block_is_soft_reason(self):
        from unittest import mock
        _Handler.reset("/bot-forever")
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            res = research.verify_url(self.base_url + "/bot-forever", timeout=2)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "bot_blocked")
        self.assertTrue(res.get("bot_blocked"))
        # bot_blocked is a failure to verify, NOT a disproof: rescuable, never a hard rejection.
        self.assertNotIn("bot_blocked", research.NON_RESCUABLE_REASONS)
        self.assertTrue(research.is_rescuable({"reasons": ["bot_blocked"]}))
        self.assertIn("bot_blocked", research.rejection_reasons({"url": "https://x/y", "price": 1}, res))
        # three GETs = one real attempt + two retries (bounded).
        self.assertEqual(_Handler.hits.get("/bot-forever", 0), 3)

    def test_verify_url_404_not_retried(self):
        from unittest import mock
        _Handler.reset("/count-404")
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            res = research.verify_url(self.base_url + "/count-404", timeout=2)
        self.assertEqual(res["reason"], "http_404")
        self.assertFalse(res.get("bot_blocked"))
        self.assertEqual(_Handler.hits.get("/count-404", 0), 1)  # a real signal, fetched once

    def test_http_fetch_cloudflare_403_is_bot_wall_plain_403_is_not(self):
        from unittest import mock
        _Handler.reset("/cf-403", "/plain-403")
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            cf = research.http_fetch(self.base_url + "/cf-403", method="GET", timeout=2, retry=True)
            plain = research.http_fetch(self.base_url + "/plain-403", method="GET", timeout=2, retry=True)
        self.assertTrue(cf["bot_blocked"])
        self.assertEqual(cf["reason"], "bot_blocked")
        self.assertEqual(_Handler.hits.get("/cf-403", 0), 3)   # bot wall retried
        self.assertFalse(plain["bot_blocked"])
        self.assertEqual(plain["reason"], "http_403")
        self.assertEqual(_Handler.hits.get("/plain-403", 0), 1)  # genuine 403 not retried

    def test_http_fetch_cf_proxied_plain_403_not_a_wall(self):
        # A CF-fronted origin 403 (Server: cloudflare + cf-ray, normal body, no challenge) is a
        # REAL 403: not retried, not bot_blocked, and the host must NOT be marked in the registry.
        from unittest import mock
        _Handler.reset("/cf-plain-403")
        registry = research.HostBlockRegistry()
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            res = research.http_fetch(self.base_url + "/cf-plain-403", method="GET", timeout=2,
                                      retry=True, host_registry=registry)
        self.assertFalse(res["bot_blocked"])
        self.assertEqual(res["reason"], "http_403")
        self.assertEqual(_Handler.hits.get("/cf-plain-403", 0), 1)  # no useless anti-bot retries
        host = research.urllib.parse.urlsplit(self.base_url).netloc.lower()
        self.assertFalse(registry.is_blocked(host))  # whole host not condemned by one origin 403

    def test_http_fetch_stops_retrying_when_host_blocked_midflight(self):
        # The retry loop must re-read the registry each attempt: if a concurrent fetch bot-walls the
        # host between attempts, we stop spending the retry budget instead of running it to the end.
        from unittest import mock
        _Handler.reset("/bot-forever")

        class FlipRegistry(research.HostBlockRegistry):
            def __init__(self):
                super().__init__()
                self.checks = 0

            def is_blocked(self, host):  # not blocked at attempt 1's decision, blocked thereafter
                self.checks += 1
                return self.checks > 1

        reg = FlipRegistry()
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            res = research.http_fetch(self.base_url + "/bot-forever", method="GET", timeout=2,
                                      retry=True, host_registry=reg)
        self.assertTrue(res["bot_blocked"])
        # attempt 1 (429) -> retry allowed; attempt 2 (429) -> re-check sees blocked -> stop.
        # Without the per-attempt re-check this would run the full budget (3 GETs).
        self.assertEqual(_Handler.hits.get("/bot-forever", 0), 2)

    def test_http_fetch_per_host_skip_retries_after_block(self):
        from unittest import mock
        _Handler.reset("/bot-forever")
        registry = research.HostBlockRegistry()
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            first = research.http_fetch(self.base_url + "/bot-forever", method="GET", timeout=2,
                                        retry=True, host_registry=registry)
            second = research.http_fetch(self.base_url + "/bot-forever", method="GET", timeout=2,
                                         retry=True, host_registry=registry)
        self.assertTrue(first["bot_blocked"])
        self.assertTrue(second["bot_blocked"])
        # First call: 1 attempt + 2 retries = 3. Second call on the now-blocked host: 1 attempt only.
        self.assertEqual(_Handler.hits.get("/bot-forever", 0), 4)

    def test_url_cache_single_flight_concurrent(self):
        # Four threads race to warm the SAME url; the per-key single-flight collapses them to one fetch.
        import concurrent.futures
        _Handler.reset("/count-slow")
        cache = research.UrlCheckCache()
        reg = research.HostBlockRegistry()
        url = self.base_url + "/count-slow"
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(research.prefetch_url, url, reg, cache) for _ in range(4)]
            for f in concurrent.futures.as_completed(futs):
                f.result()
        self.assertEqual(_Handler.hits.get("/count-slow", 0), 1)

    def test_verify_url_reads_warm_cache_without_refetch(self):
        _Handler.reset("/count-verify")
        cache = research.UrlCheckCache()
        first = research.verify_url(self.base_url + "/count-verify", timeout=2, cache=cache)
        second = research.verify_url(self.base_url + "/count-verify", timeout=2, cache=cache)
        self.assertTrue(first["ok"])
        self.assertEqual(first, second)
        self.assertEqual(_Handler.hits.get("/count-verify", 0), 1)  # HEAD 405 -> one GET, then cached

    def test_url_cache_does_not_memoize_transient_failure(self):
        # A URL that fails once then recovers must NOT stay cached as a failure: a later round
        # (sharing the run cache) has to be free to re-fetch and verify it — otherwise rescue is dead.
        _Handler.reset("/verify-recover")
        cache = research.UrlCheckCache()
        first = research.verify_url(self.base_url + "/verify-recover", timeout=2, cache=cache)
        self.assertFalse(first["ok"])  # round 1: 500, hard fail
        second = research.verify_url(self.base_url + "/verify-recover", timeout=2, cache=cache)
        self.assertTrue(second["ok"])  # round 2: failure was not retained -> re-fetched -> 200
        self.assertEqual(_Handler.hits.get("/verify-recover", 0), 2)

    def test_live_listing_check_reads_warm_cache_without_refetch(self):
        _Handler.reset("/count-live")
        cache = research.UrlCheckCache()
        a = research.live_listing_check(self.base_url + "/count-live", cache=cache)
        b = research.live_listing_check(self.base_url + "/count-live", cache=cache)
        self.assertTrue(a["ok"])
        self.assertEqual(a["live_price"], b["live_price"])
        self.assertEqual(_Handler.hits.get("/count-live", 0), 1)

    def test_prefetch_via_on_record_then_batch_verify_fetches_once(self):
        # End-to-end: collect_with_straggler_drop fires on_record for a completed search record, which
        # parses its findings and warms the cache; the later batch verify then adds zero fetches.
        import concurrent.futures
        import json
        import tempfile
        _Handler.reset("/count-verify")
        cache = research.UrlCheckCache()
        reg = research.HostBlockRegistry()
        url = self.base_url + "/count-verify"
        record = {"success": True, "leg": "codex", "task_id": "t1", "record_id": "r1", "latency_sec": 0.1,
                  "stdout": json.dumps({"findings": [{"title": "x", "price": 10, "currency": "USD", "url": url}]})}
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        submitted = []

        def on_record(rec):
            for f in research.findings_from_record(rec):
                if f.get("url"):
                    submitted.append(pool.submit(research.prefetch_url, f["url"], reg, cache))

        fut = concurrent.futures.Future()
        fut.set_result(record)
        config = dict(research.make_config("quick", None))
        config["straggler_grace_sec"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "prefetch-run"
            run_dir.mkdir()
            research.collect_with_straggler_drop([fut], run_dir, config, on_record=on_record)
        for s in submitted:
            s.result()  # wait for the prefetch to warm the cache
        pool.shutdown(wait=True)
        verified, _ = research.verify_findings(research.findings_from_record(record),
                                               cache=cache, host_registry=reg)
        self.assertEqual(len(verified), 1)
        self.assertEqual(_Handler.hits.get("/count-verify", 0), 1)

    def test_verify_findings_identical_with_and_without_warm_cache(self):
        # Warming the cache changes only TIMING: the verified/rejected split and reasons must match.
        def mk(url, price):
            return research.normalize_finding(
                {"title": "x", "price": price, "currency": "USD", "url": url}, "codex", "t", "r")
        urls = [self.base_url + "/ok", self.base_url + "/missing", self.base_url + "/listing-live"]
        findings = [mk(urls[0], 10), mk(urls[1], 20), mk(urls[2], 30)]

        cold_v, cold_r = research.verify_findings([dict(f) for f in findings])

        cache = research.UrlCheckCache()
        reg = research.HostBlockRegistry()
        for f in findings:
            research.prefetch_url(f["url"], reg, cache)
        warm_v, warm_r = research.verify_findings([dict(f) for f in findings], cache=cache, host_registry=reg)

        def sig(items):
            return sorted((i.get("url"), tuple(i.get("reasons") or [])) for i in items)
        self.assertEqual(sig(cold_v), sig(warm_v))
        self.assertEqual(sig(cold_r), sig(warm_r))
        self.assertEqual({i.get("url") for i in warm_v}, {urls[0], urls[2]})
        self.assertEqual({i.get("url") for i in warm_r}, {urls[1]})

    def test_prefetch_cache_respects_host_block_registry(self):
        # The SAME registry is shared by prefetch and the batch: once /bot-forever marks the host,
        # a DIFFERENT bot-walling URL on that host gets a single polite GET attempt (no retries).
        from unittest import mock
        _Handler.reset("/bot-forever", "/cf-403")
        cache = research.UrlCheckCache()
        reg = research.HostBlockRegistry()
        with mock.patch.object(research, "BOT_BLOCK_BACKOFF", (0.0, 0.0)):
            research.prefetch_url(self.base_url + "/bot-forever", reg, cache)  # marks host blocked
            finding = research.normalize_finding(
                {"title": "y", "price": 5, "currency": "USD", "url": self.base_url + "/cf-403"},
                "codex", "t", "r")
            research.verify_findings([finding], cache=cache, host_registry=reg)
        self.assertEqual(_Handler.hits.get("/bot-forever", 0), 3)  # 1 attempt + 2 retries before mark
        self.assertEqual(_Handler.hits.get("/cf-403", 0), 1)       # throttled host -> single attempt

    def test_collect_straggler_invokes_on_record_per_record(self):
        import concurrent.futures
        import tempfile
        seen = []
        futs = []
        for i in range(3):
            f = concurrent.futures.Future()
            f.set_result({"record_id": f"r{i}", "latency_sec": 0.1, "success": True})
            futs.append(f)
        config = dict(research.make_config("quick", None))
        config["straggler_grace_sec"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "on-record-run"
            run_dir.mkdir()
            records = research.collect_with_straggler_drop(
                futs, run_dir, config, on_record=lambda r: seen.append(r["record_id"]))
        self.assertEqual(sorted(seen), ["r0", "r1", "r2"])
        self.assertEqual(len(records), 3)

    def test_prefetch_shutdown_cancels_queued_backlog(self):
        # shutdown(cancel_futures=True): a task already running finishes, but tasks still queued
        # behind it must be dropped, not keep fetching after phase end / run cancel.
        import json
        import threading as th
        from unittest import mock
        config = dict(research.make_config("quick", None))
        cache = research.UrlCheckCache()
        reg = research.HostBlockRegistry()
        ran = []
        started = th.Event()
        release = th.Event()

        def fake_prefetch(url, host_registry, cache):
            ran.append(url)
            if url.endswith("/block"):
                started.set()
                release.wait(5)

        with mock.patch.object(research, "MAX_PREFETCH_WORKERS", 1), \
             mock.patch.object(research, "prefetch_url", fake_prefetch):
            on_record, shutdown = research.make_prefetch_collector(config, reg, cache)
            rec = {"success": True, "leg": "codex", "task_id": "t", "record_id": "r",
                   "stdout": json.dumps({"findings": [
                       {"title": "a", "price": 1, "currency": "USD", "url": "http://h.test/block"},
                       {"title": "b", "price": 2, "currency": "USD", "url": "http://h.test/queued1"},
                       {"title": "c", "price": 3, "currency": "USD", "url": "http://h.test/queued2"}]})}
            on_record(rec)
            self.assertTrue(started.wait(5))  # first task holds the single worker
            shutdown()                        # cancel_futures drops the two still-queued tasks
            release.set()
        self.assertIn("http://h.test/block", ran)          # already-running task finished
        self.assertNotIn("http://h.test/queued1", ran)     # queued backlog cancelled
        self.assertNotIn("http://h.test/queued2", ran)

    def test_retry_after_seconds_rejects_nan_and_negative(self):
        class _Exc:
            def __init__(self, val):
                self.headers = {"Retry-After": val}
        self.assertIsNone(research._retry_after_seconds(_Exc("nan")))   # nan must not poison backoff
        self.assertIsNone(research._retry_after_seconds(_Exc("-5")))
        self.assertIsNone(research._retry_after_seconds(_Exc("junk")))
        self.assertEqual(research._retry_after_seconds(_Exc("3")), 3.0)
        self.assertEqual(research._retry_after_seconds(_Exc("99999")),
                         research.BOT_BLOCK_RETRY_AFTER_CAP)

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
        # Per-vendor tiers default to the strongest tier, applied to every role that vendor plays.
        self.assertEqual(default["claude_model"], "opus")
        self.assertEqual(default["claude_search_model"], "opus")
        self.assertEqual(default["search_effort"], "xhigh")
        self.assertEqual(default["judge_effort"], "xhigh")
        self.assertEqual(default["gemini_model"], "Gemini 3.1 Pro (High)")

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

        item = research.normalize_finding(
            {"title": "x", "url": self.base_url + "/listing-live", "price": 20500.0, "currency": "UAH"},
            "codex", "task-1", "r1",
        )
        item["disputed"] = True
        with mock.patch.object(research, "listing_key", return_value="olx:test"):
            research.apply_live_check(item)
        self.assertEqual(item["price"], 30000.0)
        self.assertEqual(item["price_corrected_from"], 20500.0)
        self.assertFalse(item["disputed"])
        self.assertEqual(item["price_usd"], research.to_usd(30000.0, "UAH"))
        self.assertTrue(any(c.get("source_model") == "live_page" and c.get("price") == 30000.0
                            for c in item["price_candidates"]))

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

    def test_apply_live_check_adopts_live_price_within_tolerance(self):
        # live (30000) within 10% of claimed (31000): the live page is authoritative and still
        # becomes the canonical price, but it is NOT flagged as a correction.
        from unittest import mock

        item = research.normalize_finding(
            {"title": "x", "url": self.base_url + "/listing-live", "price": 31000.0, "currency": "UAH"},
            "codex", "task-1", "r1",
        )
        with mock.patch.object(research, "listing_key", return_value="olx:tol"):
            research.apply_live_check(item)
        self.assertEqual(item["price"], 30000.0)
        self.assertEqual(item["price_usd"], research.to_usd(30000.0, "UAH"))
        self.assertNotIn("price_corrected_from", item)
        self.assertTrue(any(c.get("source_model") == "live_page" for c in item["price_candidates"]))

    def test_refund_leg_budget(self):
        run_id = "refund-run"
        research.init_leg_budget(run_id, {"gemini": 1})
        try:
            self.assertTrue(research.consume_leg_budget(run_id, "gemini"))
            self.assertFalse(research.consume_leg_budget(run_id, "gemini"))  # spent
            research.refund_leg_budget(run_id, "gemini")
            self.assertTrue(research.consume_leg_budget(run_id, "gemini"))  # back
        finally:
            research.clear_leg_budget(run_id)

    def test_usd_conversion(self):
        self.assertEqual(research.canon_currency("грн"), "UAH")
        self.assertEqual(research.canon_currency("$"), "USD")
        self.assertEqual(research.canon_currency("usd"), "USD")
        self.assertIsNone(research.canon_currency("zorkons"))
        self.assertEqual(research.to_usd(41.5, "UAH"), 1.0)
        self.assertEqual(research.to_usd(100, "USD"), 100.0)
        self.assertIsNone(research.to_usd(100, "zorkons"))
        self.assertIsNone(research.to_usd(None, "USD"))

    def test_normalize_finding_sets_price_usd(self):
        f = research.normalize_finding({"title": "x", "price": "830 грн", "currency": "грн"}, "codex", "t", "r")
        self.assertEqual(f["currency"], "UAH")
        self.assertEqual(f["price"], 830.0)
        self.assertEqual(f["price_usd"], research.to_usd(830.0, "UAH"))

    def test_coerce_intent(self):
        payload = {"intent": {
            "subject_keywords": ["MacBook", "Air"], "exclude_keywords": ["запчасти"],
            "required_tier": "Max 5x", "official_price": "100", "official_currency": "USD",
            "cheaper_than_official": True,
        }}
        intent = research.coerce_intent(payload)
        self.assertEqual(intent["subject_keywords"], ["macbook", "air"])
        self.assertEqual(intent["required_tier"], "max_5x")
        self.assertEqual(intent["official_price_usd"], 100.0)
        self.assertTrue(intent["cheaper_than_official"])
        self.assertEqual(research.coerce_intent({}), research.default_intent())

    def test_intent_rejection_gates(self):
        intent = {
            "subject_keywords": ["macbook"], "exclude_keywords": ["for parts"],
            "required_tier": "max_5x", "official_price_usd": 100.0, "cheaper_than_official": True,
        }
        # exclude keyword hit -> recoverable (distinct reason), NOT final off_intent
        self.assertEqual(research.intent_rejection({"title": "MacBook Air for parts"}, intent), "excluded_by_keyword")
        # wrong product (no subject keyword) -> final
        self.assertEqual(research.intent_rejection({"title": "Dell XPS laptop"}, intent), "off_intent")
        # wrong tier (pro < max_5x)
        self.assertEqual(research.intent_rejection({"title": "macbook", "tier": "pro", "price_usd": 50}, intent), "wrong_tier")
        # at-or-above official price
        self.assertEqual(research.intent_rejection({"title": "macbook", "tier": "max_5x", "price_usd": 120}, intent), "not_below_official")
        # good: right subject, right tier, below official
        self.assertIsNone(research.intent_rejection({"title": "macbook", "tier": "max_20x", "price_usd": 80}, intent))
        # no intent → never rejects
        self.assertIsNone(research.intent_rejection({"title": "anything"}, None))

    def test_canon_basis_and_class(self):
        self.assertEqual(research.canon_basis("$9.99/mo"), "subscription_monthly")
        self.assertEqual(research.canon_basis("billed annually"), "subscription_yearly")
        self.assertEqual(research.canon_basis("per 1k tokens"), "usage_metered")
        self.assertEqual(research.canon_basis("lifetime license"), "one_time")
        self.assertEqual(research.canon_basis("Free"), "free")
        self.assertEqual(research.canon_basis("garbage"), "unknown")
        self.assertEqual(research.basis_class("subscription_yearly"), "recurring")
        self.assertEqual(research.basis_class("usage_metered"), "metered")
        self.assertEqual(research.basis_class("one_time"), "one_time")

    def test_to_monthly_usd(self):
        self.assertEqual(research.to_monthly_usd(240, "subscription_yearly", None), 20.0)
        self.assertEqual(research.to_monthly_usd(20, "subscription_monthly", None), 20.0)
        self.assertIsNone(research.to_monthly_usd(999, "one_time", None))
        self.assertEqual(research.to_monthly_usd(0.002, "usage_metered", {"monthly_usage_units": 1_000_000}), 2000.0)
        self.assertIsNone(research.to_monthly_usd(0.002, "usage_metered", None))

    def test_coerce_intent_price_basis(self):
        intent = research.coerce_intent({"intent": {
            "subject_keywords": ["claude"], "price_basis": "per month", "official_price": "240",
            "official_currency": "USD", "official_price_basis": "per year", "free_ok": False,
            "cheaper_than_official": True, "monthly_usage_units": "1000000", "usage_unit": "tokens"}})
        self.assertEqual(intent["price_basis"], "subscription_monthly")
        self.assertEqual(intent["official_price_basis"], "subscription_yearly")
        self.assertFalse(intent["free_ok"])
        self.assertEqual(intent["monthly_usage_units"], 1_000_000.0)
        self.assertEqual(intent["official_price_monthly_usd"], 20.0)  # $240/yr -> $20/mo
        # defaults when omitted
        d = research.coerce_intent({"intent": {"subject_keywords": ["x"]}})
        self.assertTrue(d["free_ok"])
        self.assertEqual(d["price_basis"], "unknown")

    def test_intent_rejection_free_excluded(self):
        intent = {"subject_keywords": ["claude"], "free_ok": False}
        self.assertEqual(research.intent_rejection({"title": "claude free tier", "price_basis": "free"}, intent), "free_excluded")
        # free_ok True (default) → a free tier is not excluded on that basis
        self.assertIsNone(research.intent_rejection({"title": "claude free tier", "price_basis": "free"}, {"subject_keywords": ["claude"]}))

    def test_intent_rejection_incomparable_basis_kept(self):
        # Owner's flagship case: official price is per-token, the offer is a monthly subscription.
        intent = {"subject_keywords": ["claude"], "cheaper_than_official": True,
                  "official_price_usd": 0.002, "price_basis": "subscription_monthly",
                  "official_price_basis": "usage_metered"}  # no monthly_usage_units -> not normalizable
        f = {"title": "claude pro", "price_basis": "subscription_monthly", "price_usd": 20}
        self.assertIsNone(research.intent_rejection(f, intent))  # NOT rejected as not_below_official
        self.assertEqual(f["basis_flag"], "incomparable_basis")

    def test_intent_rejection_annual_vs_monthly_normalized(self):
        intent = {"subject_keywords": ["claude"], "cheaper_than_official": True,
                  "official_price_usd": 20, "price_basis": "subscription_monthly",
                  "official_price_basis": "subscription_monthly", "official_price_monthly_usd": 20}
        over = {"title": "claude", "price_basis": "subscription_yearly", "price_usd": 240}  # $20/mo
        self.assertEqual(research.intent_rejection(over, intent), "not_below_official")
        under = {"title": "claude", "price_basis": "subscription_yearly", "price_usd": 120}  # $10/mo
        self.assertIsNone(research.intent_rejection(under, intent))
        self.assertEqual(under["price_usd_monthly"], 10.0)

    def test_intent_rejection_unknown_basis_backcompat(self):
        # No basis on either side → legacy raw-USD comparison, unchanged.
        intent = {"subject_keywords": ["macbook"], "cheaper_than_official": True, "official_price_usd": 100}
        self.assertEqual(research.intent_rejection({"title": "macbook", "price_usd": 120}, intent), "not_below_official")
        self.assertIsNone(research.intent_rejection({"title": "macbook", "price_usd": 80}, intent))

    def test_free_excluded_non_rescuable(self):
        self.assertIn("free_excluded", research.NON_RESCUABLE_REASONS)
        self.assertFalse(research.is_rescuable({"reasons": ["free_excluded"]}))

    def test_keyword_hits_word_boundaries(self):
        # Glued-substring false positives must NOT fire (the root-cause bug: "prompt" nuking
        # every listing that says "prompts").
        self.assertEqual(research.keyword_hits("supports prompts and caching", ["prompt"]), [])
        self.assertEqual(research.keyword_hits("freedom of choice", ["free"]), [])
        self.assertEqual(research.keyword_hits("send us an invoice", ["voice"]), [])
        self.assertEqual(research.keyword_hits("industrial supply", ["trial"]), [])
        # a standalone word inside a phrase DOES match (correct; same shape as Cyrillic below)
        self.assertEqual(research.keyword_hits("supports prompt caching", ["prompt"]), ["prompt"])
        # multi-word phrase: whole-phrase boundary
        self.assertEqual(research.keyword_hits("Claude Pro plan monthly", ["claude pro"]), ["claude pro"])
        self.assertEqual(research.keyword_hits("claude professional edition", ["claude pro"]), [])
        # Cyrillic (Unicode word chars)
        self.assertEqual(research.keyword_hits("общий аккаунт доступ", ["аккаунт"]), ["аккаунт"])
        self.assertEqual(research.keyword_hits("аккаунтище большой", ["аккаунт"]), [])
        # a genuine standalone match still fires
        self.assertEqual(research.keyword_hits("MacBook Air for parts", ["for parts"]), ["for parts"])

    def test_intent_rejection_word_boundary_no_false_reject(self):
        # Root-cause: an OpenRouter Fable 5 listing must NOT be nuked by the bad generic exclude
        # keyword "prompt" merely because its evidence says "prompts" (word-boundary fix).
        intent = {"subject_keywords": ["claude", "fable 5"], "exclude_keywords": ["prompt", "free"]}
        finding = {"title": "OpenRouter - Anthropic Claude Fable 5",
                   "evidence": "supports prompts; pay-as-you-go", "marketplace": "OpenRouter"}
        self.assertIsNone(research.intent_rejection(finding, intent))
        # a genuinely standalone excluded word -> recoverable "excluded_by_keyword", not a silent
        # final delete (change #3): stays rescuable, lands in "Unverified — check manually".
        excluded = {"title": "Claude Fable 5 free giveaway", "evidence": "", "marketplace": ""}
        reason = research.intent_rejection(excluded, intent)
        self.assertEqual(reason, "excluded_by_keyword")
        self.assertNotIn("excluded_by_keyword", research.NON_RESCUABLE_REASONS)
        self.assertTrue(research.is_rescuable({"reasons": ["excluded_by_keyword"]}))
        # missing all subject keywords -> final off_intent
        self.assertEqual(
            research.intent_rejection({"title": "GitLab Premium", "evidence": "", "marketplace": ""}, intent),
            "off_intent")

    def test_merge_finding_upgrades_unknown_basis(self):
        a = research.normalize_finding({"title": "x", "price": 10, "currency": "USD"}, "codex", "t", "r1")
        b = research.normalize_finding({"title": "x", "price": 10, "currency": "USD", "price_basis": "subscription_monthly"}, "gemini", "t", "r2")
        self.assertEqual(a["price_basis"], "unknown")
        merged = research.merge_finding(a, b)
        self.assertEqual(merged["price_basis"], "subscription_monthly")

    def test_credible_floor_skips_incomparable(self):
        verified = [
            {"price_usd": 50, "basis_flag": "incomparable_basis"},
            {"price_usd": 90},
        ]
        self.assertEqual(research.credible_floor_usd(verified), 90)

    def test_parse_count(self):
        # A usage quantity, NOT a price: grouping commas mean thousands, not decimals.
        self.assertEqual(research.parse_count("1,000,000"), 1_000_000.0)
        self.assertEqual(research.parse_count("1000000"), 1_000_000.0)
        self.assertEqual(research.parse_count(500000), 500000.0)
        self.assertIsNone(research.parse_count(0))
        self.assertIsNone(research.parse_count("free"))
        self.assertIsNone(research.parse_count(None))

    def test_coerce_intent_usage_units_grouped(self):
        # Regression: a comma-grouped usage count must parse (parse_price would have dropped it).
        intent = research.coerce_intent({"intent": {"subject_keywords": ["x"], "monthly_usage_units": "1,500,000"}})
        self.assertEqual(intent["monthly_usage_units"], 1_500_000.0)

    def test_normalize_finding_sets_monthly_for_recurring(self):
        # price_usd_monthly is populated for every recurring finding, not only priced-out ones.
        yearly = research.normalize_finding({"title": "x", "price": 240, "currency": "USD", "price_basis": "subscription_yearly"}, "codex", "t", "r")
        self.assertEqual(yearly["price_usd_monthly"], 20.0)
        once = research.normalize_finding({"title": "x", "price": 999, "currency": "USD", "price_basis": "one_time"}, "codex", "t", "r")
        self.assertIsNone(once["price_usd_monthly"])

    def test_tier_ranking(self):
        self.assertLess(research.tier_rank("pro"), research.tier_rank("max_5x"))
        self.assertLess(research.tier_rank("max_5x"), research.tier_rank("max_20x"))
        self.assertEqual(research.canon_tier("Max 20x"), "max_20x")
        self.assertEqual(research.canon_tier("6.25x"), "max_5x")
        self.assertIsNone(research.tier_rank("nonsense"))

    def test_listing_key_grey_markets(self):
        self.assertEqual(research.listing_key("https://plati.market/itm/claude-max/5284146"), "plati:5284146")
        self.assertEqual(research.listing_key("https://www.funpay.com/en/lots/offer?id=12345"), "funpay:12345")
        self.assertTrue((research.listing_key("https://ggsel.net/catalog/product/4033189") or "").startswith("digiseller:"))

    def test_diversify_anti_monoculture(self):
        items = [{"url": f"https://plati.market/itm/x/{i}"} for i in range(8)]
        items += [{"url": "https://olx.ua/d/obyavlenie/y-ID1.html"}, {"url": "https://prom.ua/p2-z"}]
        out = research.diversify(items, cap_fraction=0.5, min_per_host=2)
        # plati (8) is capped at 5 (0.5*10) in the lead; the rest pushed below the two other hosts
        lead_hosts = [research.host_of(x["url"]) for x in out[:7]]
        self.assertLessEqual(lead_hosts.count("plati.market"), 5)
        self.assertIn("olx.ua", lead_hosts)
        self.assertEqual(len(out), len(items))

    def test_live_check_jsonld_and_og(self):
        ld = research.live_listing_check(self.base_url + "/ldjson")
        self.assertEqual(ld["live_price"], 499.0)
        self.assertEqual(ld["live_currency"], "USD")
        self.assertEqual(ld["ad_status"], "active")
        og = research.live_listing_check(self.base_url + "/og")
        self.assertEqual(og["live_price"], 42.5)
        self.assertEqual(research.canon_currency(og["live_currency"]), "EUR")

    def test_vendor_disable_config(self):
        # all on by default
        c = research.make_config("max", None, None)
        self.assertEqual(c["enabled_legs"], ["codex", "gemini", "claude"])
        self.assertEqual(c["disabled_legs"], [])
        # disable claude (alias-insensitive) → roles fall to codex/gemini
        c = research.make_config("max", None, "claude")
        self.assertEqual(c["enabled_legs"], ["codex", "gemini"])
        self.assertNotIn("claude", c["search_legs"])
        self.assertEqual(research.judge_vendor(c), "codex")
        self.assertEqual(research.arbiter_vendor(c), "codex")  # claude gone → codex arbitrates
        # disable gpt (alias for codex) → gemini leads, claude arbitrates
        c = research.make_config("deep", None, "gpt")
        self.assertEqual(c["enabled_legs"], ["gemini", "claude"])
        self.assertEqual(research.judge_vendor(c), "claude")
        self.assertEqual(research.judge_chain(c), ["claude", "gemini"])
        # disable two → the single remaining vendor does everything
        c = research.make_config("max", None, ["gpt", "gemini"])
        self.assertEqual(c["enabled_legs"], ["claude"])
        self.assertEqual(c["search_legs"], ["claude"])
        self.assertEqual(research.judge_vendor(c), "claude")
        # disabling all is ignored (research must not break)
        c = research.make_config("max", None, "gpt,gemini,claude")
        self.assertEqual(c["enabled_legs"], ["codex", "gemini", "claude"])
        self.assertEqual(c["disabled_legs"], [])

    def test_user_disabled_blocks_call_model(self):
        import tempfile
        run_id = "vendoff-run"
        research.set_user_disabled(run_id, {"claude"})
        try:
            self.assertTrue(research.user_disabled(run_id, "claude"))
            self.assertFalse(research.user_disabled(run_id, "codex"))
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                (run_dir / "raw").mkdir(parents=True)
                rec = research.call_model("claude", "x", run_dir, "adjudicate", "t")
                self.assertFalse(rec["success"])
                self.assertTrue(rec["skipped_by_user"])
        finally:
            research.clear_user_disabled(run_id)

    def test_calibrate_confidence(self):
        # strong: 3 models agree, live-verified, high trust, high model conf → high band
        strong = {"source_models": ["codex", "gemini", "claude"], "confidence": 0.9,
                  "trust": {"score": 0.9}, "live_check": {"ok": True, "live_price": 100}}
        c = research.calibrate_confidence(strong)
        self.assertEqual(c["band"], "high")
        self.assertGreaterEqual(c["score"], 0.7)
        self.assertTrue(any("3 models" in f for f in c["factors"]))

        # weak: single source, not live-verified, low trust, disputed → low band
        weak = {"source_models": ["codex"], "confidence": 0.3, "disputed": True,
                "trust": {"score": 0.2}, "live_check": {"ok": False}}
        c2 = research.calibrate_confidence(weak)
        self.assertEqual(c2["band"], "low")
        self.assertLess(c2["score"], 0.45)
        self.assertTrue(any("dispute" in f for f in c2["factors"]))

        # inactive listing tanks the live component
        inactive = {"source_models": ["codex", "gemini"], "confidence": 0.8,
                    "trust": {"score": 0.8}, "listing_inactive": True, "live_check": {"ok": True}}
        self.assertTrue(any("inactive" in f for f in research.calibrate_confidence(inactive)["factors"]))

    def test_apply_confidence_attaches(self):
        verified = [research.normalize_finding({"title": "x", "url": "https://example.com/i", "price": 100, "currency": "USD"}, "codex", "t", "r")]
        research.apply_trust(verified)
        research.apply_confidence(verified)
        self.assertIn("confidence_calibrated", verified[0])
        self.assertIn(verified[0]["confidence_calibrated"]["band"], ("high", "medium", "low"))

    def test_final_factcheck_gated(self):
        self.assertFalse(research.make_config("quick", None)["final_factcheck"])
        self.assertFalse(research.make_config("standard", None)["final_factcheck"])
        self.assertTrue(research.make_config("deep", None)["final_factcheck"])
        self.assertTrue(research.make_config("max", None)["final_factcheck"])

    def test_factcheck_top_pick_code_verdicts(self):
        import tempfile
        from unittest import mock

        cfg = research.make_config("deep", None)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp); (run_dir / "raw").mkdir()
            research.init_leg_health(run_dir.name)
            try:
                # no verified → None
                self.assertIsNone(research.factcheck_top_pick("p", [], run_dir, cfg, None))

                # non-marketplace URL → cannot re-verify (ok None)
                v = research.factcheck_top_pick("p", [research.normalize_finding(
                    {"title": "x", "url": "https://example.com/item", "price": 100, "currency": "USD"}, "codex", "t", "r")],
                    run_dir, cfg, None)
                self.assertIsNone(v["ok"])
                self.assertEqual(v["reason"], "no_listing_adapter")

                # live listing inactive → FAIL (mock listing_key + the live fetch)
                top = research.normalize_finding({"title": "x", "url": "https://plati.market/itm/x/55555", "price": 100, "currency": "USD"}, "codex", "t", "r")
                with mock.patch.object(research, "live_listing_check", return_value={"ok": True, "ad_status": "removed_by_user", "live_price": None, "live_currency": None, "page_title": "x", "live_variants": {}}):
                    v = research.factcheck_top_pick("p", [top], run_dir, cfg, None)
                self.assertFalse(v["ok"]); self.assertEqual(v["reason"], "listing_inactive")

                # price drift > tolerance → FAIL (claimed 100, live 200; legs disabled so no model call)
                top2 = research.normalize_finding({"title": "x", "url": "https://plati.market/itm/x/55556", "price": 100, "currency": "USD"}, "codex", "t", "r")
                research.force_disable_leg(run_dir.name, "codex", "x")
                research.force_disable_leg(run_dir.name, "gemini", "x")
                with mock.patch.object(research, "live_listing_check", return_value={"ok": True, "ad_status": "active", "live_price": 200, "live_currency": "USD", "page_title": "x", "live_variants": {}}):
                    v = research.factcheck_top_pick("p", [top2], run_dir, cfg, None)
                self.assertFalse(v["ok"]); self.assertIn("price_drift", v["reason"])
            finally:
                research.clear_leg_health(run_dir.name)

    def test_seller_trust_scoring(self):
        # established account + reviews + business → high trust
        good = research.seller_trust(
            {"seller": "business seller, account since 2018, 340 reviews", "price_usd": 100, "condition": "used-good"},
            floor_usd=90)
        self.assertGreaterEqual(good["score"], 0.8)
        # far below market + scam wording → low trust
        bad = research.seller_trust(
            {"seller": "no reviews, prepayment only", "price_usd": 30, "condition": "unknown"}, floor_usd=90)
        self.assertLess(bad["score"], 0.4)
        self.assertTrue(any("below market" in s for s in bad["signals"]))
        # damaged → penalized
        dmg = research.seller_trust({"seller": "private", "price_usd": 80, "condition": "for parts"}, floor_usd=90)
        self.assertTrue(any("parts" in s for s in dmg["signals"]))
        # no seller info → uncertain, mild penalty, stays mid
        none = research.seller_trust({"price_usd": 100}, floor_usd=90)
        self.assertLessEqual(none["score"], 0.5)

    def test_apply_trust_and_rank_key(self):
        verified = [
            {"price_usd": 30, "seller": "no reviews, prepay only", "condition": "unknown"},   # cheap, low trust
            {"price_usd": 100, "seller": "business, account since 2017, 500 reviews", "condition": "used-good"},  # credible
        ]
        research.apply_trust(verified)
        self.assertIn("trust", verified[0])
        ordered = sorted(verified, key=research.trust_rank_key)
        # the credible (higher-trust) item leads despite being pricier
        self.assertEqual(ordered[0]["price_usd"], 100)

    def test_frontier_rounds_gated_and_floor(self):
        self.assertEqual(research.make_config("quick", None)["frontier_rounds"], 0)
        self.assertEqual(research.make_config("standard", None)["frontier_rounds"], 0)
        self.assertEqual(research.make_config("deep", None)["frontier_rounds"], 1)
        self.assertEqual(research.make_config("max", None)["frontier_rounds"], 2)
        # credible floor = cheapest non-disputed USD price; disputed/None ignored
        self.assertEqual(research.credible_floor_usd(
            [{"price_usd": 100, "disputed": False}, {"price_usd": 70, "disputed": True}, {"price_usd": 85, "disputed": False}]), 85)
        self.assertIsNone(research.credible_floor_usd([{"price_usd": None}, {"price_usd": 50, "disputed": True}]))
        self.assertIsNone(research.credible_floor_usd([]))

    def test_build_frontier_prompt_ceiling(self):
        p = research.build_frontier_prompt("find cheapest macbook", 250.0, ["olx.ua"],
                                           {"subject_keywords": ["macbook air m2"]})
        self.assertIn("250.00", p)
        self.assertIn("STRICTLY CHEAPER", p)
        self.assertIn("olx.ua", p)
        self.assertIn("macbook air m2", p)

    def test_build_synthesis_prompt_renders_literal_braces(self):
        # Regression: a literal "{score, band: ...}" in the f-string was evaluated as an
        # expression (NameError: name 'score' is not defined), crashing every run at synthesis.
        finding = {"title": "x", "url": "https://e", "price_usd": 10, "trust": {"score": 0.9}}
        p = research.build_synthesis_prompt(
            "find cheapest x", [], [finding], [],
            {"sites": [], "judge_effort": "xhigh"}, None, {"subject_keywords": ["x"]},
        )
        self.assertIn("{score, band: high/medium/low, factors}", p)

    def test_no_undefined_names_in_fstrings(self):
        # Guards EVERY prompt builder against the unescaped-brace bug class: a literal "{...}"
        # left in an f-string is silently parsed as an expression and blows up at runtime only
        # when that branch executes. Catch it statically across the whole module instead.
        import ast
        import builtins as _builtins
        from pathlib import Path

        src = (Path(research.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        scope_builtins = set(dir(_builtins))

        module_names = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_names.update((a.asname or a.name).split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                module_names.update(a.asname or a.name for a in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.If, ast.Try, ast.With)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                        module_names.add(sub.id)
                    elif isinstance(sub, ast.Import):
                        module_names.update((a.asname or a.name).split(".")[0] for a in sub.names)
                    elif isinstance(sub, ast.ImportFrom):
                        module_names.update(a.asname or a.name for a in sub.names)

        def bound_names(fn):
            names = set()
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    names.add(n.id)
                elif isinstance(n, ast.arg):
                    names.add(n.arg)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names.add(n.name)
                elif isinstance(n, ast.ExceptHandler) and n.name:
                    names.add(n.name)
                elif isinstance(n, ast.Import):
                    names.update((a.asname or a.name).split(".")[0] for a in n.names)
                elif isinstance(n, ast.ImportFrom):
                    names.update(a.asname or a.name for a in n.names)
            return names

        # Parent map so a name in an f-string can see ALL enclosing function scopes (closures),
        # not just its innermost function — otherwise a closure variable reads as "undefined".
        parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}

        def enclosing_funcs(node):
            out, p = [], parents.get(node)
            while p is not None:
                if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(p)
                p = parents.get(p)
            return out

        fn_locals, offenders = {}, []
        for js in ast.walk(tree):
            if not isinstance(js, ast.JoinedStr):
                continue
            for fv in js.values:
                if not isinstance(fv, ast.FormattedValue):
                    continue
                for nm in ast.walk(fv.value):
                    if not (isinstance(nm, ast.Name) and isinstance(nm.ctx, ast.Load)):
                        continue
                    funcs = enclosing_funcs(nm)
                    scope = set(module_names) | scope_builtins
                    for fn in funcs:
                        if fn not in fn_locals:
                            fn_locals[fn] = bound_names(fn)
                        scope |= fn_locals[fn]
                    if nm.id not in scope:
                        where = funcs[0].name if funcs else "<module>"
                        offenders.append(f"line {nm.lineno}: undefined name '{nm.id}' in f-string (in {where})")
        self.assertEqual(offenders, [], "unescaped-brace bug(s) in f-string(s):\n" + "\n".join(offenders))

    def test_run_frontier_round_fans_out_legs(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_type, task_id))
            return {"success": True, "stdout": '{"findings": []}', "leg": leg, "task_id": task_id, "record_id": "x"}

        cfg = research.make_config("max", None)  # search_legs codex+gemini+claude
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_frontier_round("p", 99.0, research.Path(tmp), cfg, {"subject_keywords": ["x"]}, 1)
        self.assertEqual(len(calls), 3)  # one per search leg
        self.assertTrue(all(tt == "frontier" for _, tt, _ in calls))
        self.assertEqual({leg for leg, _, _ in calls}, {"codex", "gemini", "claude"})

    def test_query_variants_parsed_and_gated(self):
        cfg = research.make_config("deep", None)
        self.assertEqual(cfg["query_variants_per_task"], 1)
        self.assertEqual(research.make_config("quick", None)["query_variants_per_task"], 1)
        self.assertEqual(research.make_config("max", None)["query_variants_per_task"], 2)
        payload = {"tasks": [
            {"id": "task-1", "query": "macbook air m2",
             "query_variants": ["Apple MacBook Air 2022 M2", "макбук аір м2", "macbook air m2"]},
            {"id": "task-2", "query": "macbook air m2 olx"},
            {"id": "task-3", "query": "macbook air m2 prom"},
        ]}
        tasks = research.coerce_tasks(payload, "p", cfg)
        # base query deduped out of variants; order preserved
        self.assertEqual(tasks[0]["query_variants"], ["Apple MacBook Air 2022 M2", "макбук аір м2"])
        self.assertEqual(tasks[1]["query_variants"], [])  # task without variants → empty list

    def test_task_query_set_expansion(self):
        task = {"id": "task-1", "query": "base q", "query_variants": ["alt one", "alt two", "alt three"]}
        self.assertEqual([t["query"] for t in research.task_query_set(task, 1)], ["base q"])
        three = research.task_query_set(task, 3)
        self.assertEqual([t["query"] for t in three], ["base q", "alt one", "alt two"])
        self.assertEqual([t["id"] for t in three], ["task-1", "task-1#v2", "task-1#v3"])
        # no variants → always just the base regardless of n
        self.assertEqual(research.task_query_set({"id": "t", "query": "x"}, 3), [{"id": "t", "query": "x"}])

    def test_run_primary_search_fans_out_variants(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "stdout": '{"findings": []}', "leg": leg, "task_id": task_id, "record_id": "x"}

        tasks = [{"id": "task-1", "query": "q1", "query_variants": ["q1b", "q1c"], "preferred_sites": []}]
        cfg = research.make_config("max", None)  # 2 query variants, search_legs codex+gemini+claude
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_primary_search("p", tasks, run_dir, cfg)
        # Fast legs (gemini, claude) cover every query variant; codex — the slow long-pole leg —
        # covers only the BASE query. 1 task × 2 variants × 2 fast legs + 1 codex base = 5 calls.
        self.assertEqual(len(calls), 5)
        self.assertEqual({tid for _, tid in calls}, {"task-1", "task-1#v2"})
        self.assertEqual([tid for leg, tid in calls if leg == "codex"], ["task-1"])

    def test_run_primary_search_includes_audit_extra_tasks(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "stdout": '{"findings": []}', "leg": leg, "task_id": task_id, "record_id": "x"}

        class _Supplier:  # Future-like: yields the audit's extra tasks when waited on
            def result(self, timeout=None):
                return [{"id": "audit-1", "query": "extra q", "focus": "f", "preferred_sites": [], "query_variants": []}]

        tasks = [{"id": "task-1", "query": "q1", "query_variants": [], "preferred_sites": []}]
        cfg = research.make_config("standard")  # level 2: legs codex+gemini+claude, 1 variant, plan_audit on
        sink = []
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_primary_search("p", tasks, run_dir, cfg,
                                            extra_tasks_supplier=_Supplier(), extra_tasks_sink=sink)
        task_ids = {tid for _, tid in calls}
        self.assertIn("task-1", task_ids)
        self.assertIn("audit-1", task_ids)          # the audit-added task joined the same fan-out
        self.assertEqual(len(calls), 6)             # (1 base + 1 extra) tasks × 3 legs
        self.assertEqual(len(sink), 1)              # only actually-searched extra tasks reported back

    def test_run_primary_search_timed_out_audit_adds_nothing(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "stdout": '{"findings": []}', "leg": leg, "task_id": task_id, "record_id": "x"}

        class _SlowSupplier:  # never ready within the bounded wait
            def result(self, timeout=None):
                raise research.concurrent.futures.TimeoutError()

        tasks = [{"id": "task-1", "query": "q1", "query_variants": [], "preferred_sites": []}]
        cfg = research.make_config("standard")
        sink = []
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_primary_search("p", tasks, run_dir, cfg,
                                            extra_tasks_supplier=_SlowSupplier(), extra_tasks_sink=sink)
        self.assertEqual(len(calls), 3)  # only the base task × 3 legs; a late audit adds nothing
        self.assertEqual(sink, [])

    def test_codex_task_cap_limits_slow_leg_fanout(self):
        # The slow leg (codex) covers only the BASE query of the first codex_task_cap tasks; the
        # fast legs (gemini/claude) cover every task for full breadth. deep -> codex_task_cap 3.
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "stdout": '{"findings": []}', "leg": leg, "task_id": task_id, "record_id": "x"}

        tasks = [{"id": f"task-{i}", "query": f"q{i}", "query_variants": [], "preferred_sites": []}
                 for i in range(1, 6)]  # 5 tasks
        cfg = research.make_config("deep", None)
        self.assertEqual(cfg["codex_task_cap"], 3)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_primary_search("p", tasks, research.Path(tmp), cfg)
        self.assertEqual(sorted(t for leg, t in calls if leg == "codex"), ["task-1", "task-2", "task-3"])
        for leg in ("gemini", "claude"):  # fast legs still cover all five tasks
            self.assertEqual(sorted(t for l, t in calls if l == leg), [f"task-{i}" for i in range(1, 6)])

    def test_collect_straggler_quorum_ignores_slow_leg(self):
        # With fast_quorum_total set, the grace window opens once the FAST legs reach quorum — the
        # slow codex calls are NOT in the denominator, so the phase never blocks on codex. Here 2
        # fast + 2 codex: fast quorum (ceil(2*0.75)=2) is met by the fast pair alone, so stragglers
        # are reaped without any codex having returned. (The old all-calls quorum needed 3 of 4 and
        # would hang on the still-pending codex.)
        import concurrent.futures
        import tempfile
        import threading as th
        from unittest import mock

        release = th.Event()
        # A record carries a finding so the zero-findings reaper guard (S2) does not extend the grace:
        # this test exercises the normal "quorum met + findings present -> reap the straggler" path.
        _stdout = '{"findings":[{"title":"x","url":"https://e.com/1","price":10,"currency":"USD"}]}'

        def fast():
            return {"leg": "gemini", "record_id": "f", "task_id": "t", "latency_sec": 0.01,
                    "success": True, "stdout": _stdout}

        def slow():
            release.wait(5)
            return {"leg": "codex", "record_id": "s", "task_id": "t", "latency_sec": 0.01,
                    "success": True, "stdout": _stdout}

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        futs = [pool.submit(fast), pool.submit(fast), pool.submit(slow), pool.submit(slow)]
        for f in futs[:2]:
            f.result()  # both fast calls are in before collect runs
        config = dict(research.make_config("standard", None))
        config["straggler_grace_sec"] = 0.2
        killed_called = th.Event()

        def fake_kill(run_id, include_protected=False):
            killed_called.set()
            release.set()  # the kill unblocks the (would-be SIGKILLed) codex futures
            return []

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "q"
            run_dir.mkdir()
            with mock.patch.object(research, "kill_stragglers", fake_kill):
                records = research.collect_with_straggler_drop(futs, run_dir, config, fast_quorum_total=2)
        pool.shutdown(wait=True)
        self.assertTrue(killed_called.is_set())  # grace opened + stragglers reaped on fast quorum alone
        self.assertEqual(len(records), 4)

    def test_straggler_kill_does_not_trip_breaker(self):
        # A straggler-killed call is a scheduling drop of a healthy-but-slow leg, not a leg failure:
        # it must never count toward disabling the leg, while genuine failures still trip the breaker.
        run_id = "breaker-guard-test"
        research.init_leg_health(run_id)
        try:
            for _ in range(research.BREAKER_THRESHOLD + 2):
                self.assertFalse(research.apply_call_to_breaker(run_id, "codex", False, True))
            self.assertFalse(research.leg_disabled(run_id, "codex"))
            tripped = [research.apply_call_to_breaker(run_id, "gemini", False, False)
                       for _ in range(research.BREAKER_THRESHOLD)]
            self.assertTrue(any(tripped))
            self.assertTrue(research.leg_disabled(run_id, "gemini"))
        finally:
            research.clear_leg_health(run_id)

    def test_effort_profiles_codex_cap_and_grace_scale(self):
        # Speed/quality gradient stays monotonic across effort levels: codex fan-out and the slow-leg
        # grace both grow with effort, so the modes stay distinguishable.
        caps = [research.make_config(l)["codex_task_cap"] for l in (1, 2, 3, 4)]
        graces = [research.make_config(l)["straggler_grace_sec"] for l in (1, 2, 3, 4)]
        self.assertEqual(caps, sorted(caps))
        self.assertTrue(all(c >= 1 for c in caps))
        self.assertEqual(graces, sorted(graces))
        self.assertLess(graces[0], graces[-1])

    def test_make_config_plan_audit_gated(self):
        self.assertFalse(research.make_config(1)["plan_audit"])
        for lvl in (2, 3, 4):
            self.assertTrue(research.make_config(lvl)["plan_audit"])

    def test_plan_audit_prompt_content(self):
        tasks = [{"id": "task-1", "query": "macbook air m2", "focus": "official store",
                  "source_class": "official_store", "angle": "exact SKU", "preferred_sites": ["apple.com"]}]
        p = research.build_plan_audit_prompt("find cheap macbook", tasks, research.default_intent(), research.make_config(3))
        self.assertIn("macbook air m2", p)   # the planned task list is embedded
        self.assertIn("official_store", p)
        self.assertIn('"verdict"', p)        # strict JSON schema fields
        self.assertIn('"extra_tasks"', p)
        self.assertIn("up to 2", p)          # the additive-task cap is stated
        self.assertIn("ONLY valid JSON", p)

    def test_coerce_audit_tasks_cap_dedupe_and_tag(self):
        cfg = research.make_config(3)
        existing = [{"id": "task-1", "query": "macbook air m2", "query_variants": ["apple macbook air 2022"]}]
        raw_extra = [
            {"query": "macbook air m2", "source_class": "official_store"},            # dup of base query -> dropped
            {"query": "Apple MacBook Air 2022", "source_class": "big_marketplace"},    # dup of a query_variant -> dropped
            {"query": "refurbished macbook air m2", "query_variants": ["v1", "v2", "v3"], "source_class": "refurb_used"},
            {"query": "macbook air m2 telegram resale", "source_class": "forums_telegram"},
            {"query": "macbook air m2 local uk", "source_class": "regional"},          # 3rd valid -> capped out
        ]
        out = research.coerce_audit_tasks(raw_extra, existing, cfg)
        self.assertEqual(len(out), 2)                                   # capped at 2, both dups dropped
        self.assertEqual([t["origin"] for t in out], ["plan_audit", "plan_audit"])
        self.assertEqual(out[0]["query_variants"], ["v1"])             # 1-variant cap (surgical)
        self.assertEqual(len({t["id"] for t in out}), 2)               # distinct audit id space
        self.assertTrue(all(t["id"].startswith("audit-") for t in out))
        kept = {t["query"] for t in out}
        self.assertNotIn("macbook air m2", kept)
        self.assertNotIn("Apple MacBook Air 2022", kept)
        # a non-list payload never raises
        self.assertEqual(research.coerce_audit_tasks(None, existing, cfg), [])

    def test_audit_plan_unparseable_returns_empty(self):
        import tempfile
        from unittest import mock

        tasks = [{"id": "task-1", "query": "q", "preferred_sites": []}]
        for record in ({"success": True, "stdout": "not json at all", "leg": "codex"},
                       {"success": False, "rc": 1, "leg": "codex"}):
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp)
                with mock.patch.object(research, "call_model", return_value=record):
                    out = research.audit_plan("p", tasks, research.default_intent(), run_dir, research.make_config(3))
                self.assertEqual(out, [])  # advisory: any failure -> [] with no exception
                rows = [research.json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
                self.assertTrue(any(r["event"] == "plan_audit_finished" for r in rows))  # still observable

    def test_audit_plan_success_yields_extra_tasks(self):
        import tempfile
        from unittest import mock

        tasks = [{"id": "task-1", "query": "macbook air m2", "preferred_sites": []}]
        payload = {"verdict": "gaps", "notes": "no refurb channel planned",
                   "extra_tasks": [
                       {"query": "macbook air m2", "source_class": "official_store"},  # dup -> dropped
                       {"query": "refurbished macbook air m2", "query_variants": ["a", "b"], "source_class": "refurb_used"},
                   ]}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            record = {"success": True, "stdout": research.json.dumps(payload), "leg": "codex"}
            with mock.patch.object(research, "call_model", return_value=record):
                out = research.audit_plan("p", tasks, research.default_intent(), run_dir, research.make_config(3))
            meta = research.read_json(run_dir / "run.json", {})
        self.assertEqual(len(out), 1)                       # dup dropped, one genuinely-additive task kept
        self.assertEqual(out[0]["origin"], "plan_audit")
        self.assertEqual(out[0]["query_variants"], ["a"])   # 1-variant cap
        self.assertEqual(meta["plan_audit"], {"verdict": "gaps", "added": 1})

    def test_make_config_gap_audit_gated(self):
        self.assertFalse(research.make_config(1)["gap_audit"])
        for lvl in (2, 3, 4):
            self.assertTrue(research.make_config(lvl)["gap_audit"])

    def test_gap_audit_prompt_content(self):
        verified = [
            {"title": "MacBook Air M2 2022", "price": 899, "currency": "USD", "url": "https://amazon.com/a"},
            {"title": "MacBook Air M2 refurb", "price_usd": 780, "url": "https://backmarket.com/b"},
        ]
        host_dist = {"amazon.com": 1, "backmarket.com": 1, "olx.ua": 0}
        p = research.build_gap_audit_prompt("find cheap macbook air m2",
                                            research.default_intent(), verified, host_dist)
        self.assertIn("MacBook Air M2 2022", p)     # finding one-liners embedded
        self.assertIn("899 USD", p)
        self.assertIn("amazon.com", p)              # host distribution embedded
        self.assertIn("olx.ua", p)                  # incl. a zero-result host
        self.assertIn("returned NOTHING", p)
        self.assertIn("up to 3", p)                 # material-gap cap stated
        self.assertIn("ONLY valid JSON", p)         # strict JSON contract
        self.assertIn('"gaps"', p)                  # schema key
        self.assertIn("Do NOT invent gaps", p)      # do-not-invent instruction

    def test_coerce_gap_queries_cap_and_dedupe(self):
        cfg = research.make_config(2)
        tasks = [{"id": "task-1", "query": "macbook air m2", "query_variants": ["apple macbook air 2022"]}]
        raw = [
            {"query": "macbook air m2", "reason": "x"},                 # dup of base task query -> dropped
            {"query": "Apple MacBook Air 2022", "reason": "y"},         # dup of a query_variant -> dropped
            {"query": "refurbished macbook air m2", "reason": "refurb channel missing"},
            {"query": "macbook air m2 telegram resale", "reason": "no forum channel"},
            {"query": "refurbished macbook air m2", "reason": "dup of an earlier gap"},  # dup of a kept gap
            {"query": "macbook air m2 local uk", "reason": "regional"},  # 4th unique -> capped out
        ]
        out = research.coerce_gap_queries(raw, tasks, cfg)
        self.assertEqual(len(out), 3)                                   # capped at 3, all dups dropped
        queries = [g["query"] for g in out]
        self.assertNotIn("macbook air m2", queries)
        self.assertNotIn("Apple MacBook Air 2022", queries)
        self.assertEqual(len(queries), len(set(q.lower() for q in queries)))  # no dup between gaps
        self.assertTrue(all("reason" in g for g in out))
        self.assertEqual(research.coerce_gap_queries("garbage", tasks, cfg), [])  # non-list -> []
        self.assertEqual(research.coerce_gap_queries([{"reason": "no query"}], tasks, cfg), [])  # empty query

    def test_gap_audit_failure_returns_empty(self):
        import tempfile
        from unittest import mock

        verified = [{"title": "t", "price": 1, "currency": "USD", "url": "https://ex.com/a"}]
        for record in ({"success": True, "stdout": "not json", "leg": "codex"},
                       {"success": False, "rc": 1, "leg": "codex"}):
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp)
                with mock.patch.object(research, "call_model", return_value=record):
                    out = research.gap_audit("p", research.default_intent(), verified,
                                             {"ex.com": 1}, run_dir, research.make_config(2))
                self.assertEqual(out, [])  # advisory: any failure -> [] with no exception
                rows = [research.json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
                self.assertTrue(any(r["event"] == "gap_audit_finished" for r in rows))  # still observable

    def test_gap_audit_success_records_run(self):
        import tempfile
        from unittest import mock

        tasks = [{"id": "task-1", "query": "macbook air m2", "preferred_sites": []}]
        verified = [{"title": "t", "price": 1, "currency": "USD", "url": "https://ex.com/a"}]
        payload = {"gaps": [
            {"query": "macbook air m2", "reason": "dup of task -> dropped"},
            {"query": "refurbished macbook air m2", "reason": "no refurb channel covered"},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            record = {"success": True, "stdout": research.json.dumps(payload), "leg": "codex"}
            with mock.patch.object(research, "call_model", return_value=record):
                out = research.gap_audit("p", research.default_intent(), verified,
                                         {"ex.com": 1}, run_dir, research.make_config(2), tasks)
            meta = research.read_json(run_dir / "run.json", {})
        self.assertEqual(len(out), 1)                    # dup-of-task gap dropped, one kept
        self.assertEqual(out[0]["query"], "refurbished macbook air m2")
        self.assertEqual(meta["gap_audit"], {"gaps": 1})

    def test_run_coverage_round_includes_gap_queries(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_type, task_id))
            return {"success": True, "stdout": '{"findings": []}', "leg": leg, "task_id": task_id, "record_id": "x"}

        cfg = research.make_config("deep", None)  # search_legs codex+gemini+claude
        gaps = [{"query": "refurbished macbook air m2", "reason": "r1"},
                {"query": "macbook air m2 regional uk", "reason": "r2"}]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_coverage_round("p", ["classifieds"], [], research.Path(tmp), cfg,
                                            {"subject_keywords": ["macbook"]}, gap_queries=gaps)
        ids = [tid for _, _, tid in calls]
        self.assertEqual(len(calls), 3)                             # 1 missing-class + 2 gap jobs, same batch
        self.assertTrue(all(tt == "coverage" for _, tt, _ in calls))
        self.assertTrue(any(i.startswith("class-classifieds") for i in ids))
        self.assertEqual(sum(1 for i in ids if i.startswith("gap-")), 2)  # both gap queries fanned out

    def test_run_coverage_round_no_jobs_without_gaps_or_classes(self):
        import tempfile
        from unittest import mock

        cfg = research.make_config("standard")  # coverage_rounds == 0 -> effort-2 mini-wave path
        # Effort-2 mini-wave machinery: no gaps -> no jobs -> no wall-clock; gaps -> jobs fire.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(research, "call_model") as m:
                self.assertEqual(research.run_coverage_round("p", [], [], research.Path(tmp), cfg, None), [])
                m.assert_not_called()
        # config gating: effort 2 has no coverage round (uses mini-wave), efforts 3-4 merge into it.
        self.assertEqual(research.make_config(2)["coverage_rounds"], 0)
        self.assertEqual(research.make_config(3)["coverage_rounds"], 1)
        self.assertEqual(research.make_config(4)["coverage_rounds"], 1)

    def test_extract_variants(self):
        v = research.extract_variants("Pro - 12$  Max 5x - 26$  Max 20x - 40 USD")
        self.assertEqual(v["pro"]["price"], 12.0)
        self.assertEqual(v["max_5x"]["price"], 26.0)
        self.assertEqual(v["max_20x"]["price"], 40.0)
        self.assertEqual(research.extract_variants("no tiers here, just text"), {})

    def test_apply_live_check_picks_requested_tier_price(self):
        from unittest import mock

        # finding claims the cheap base price (12, looks like Pro); user wants Max 5x.
        item = research.normalize_finding(
            {"title": "Claude account bundle", "url": self.base_url + "/variants", "price": 12, "currency": "USD"},
            "codex", "t", "r1",
        )
        intent = {"required_tier": "max_5x"}
        with mock.patch.object(research, "listing_key", return_value="plati:1"):
            research.apply_live_check(item, intent)
        self.assertTrue(item.get("variant_corrected"))
        self.assertEqual(item["tier"], "max_5x")
        self.assertEqual(item["price"], 26.0)          # requested tier's price, not the base 12
        self.assertEqual(item["price_usd"], 26.0)
        self.assertEqual(item["price_corrected_from"], 12)

        # no required tier and no finding tier → general live price path (no variant override)
        plain = research.normalize_finding(
            {"title": "Claude account bundle", "url": self.base_url + "/variants", "price": 12, "currency": "USD"},
            "codex", "t", "r2",
        )
        with mock.patch.object(research, "listing_key", return_value="plati:2"):
            research.apply_live_check(plain, None)
        self.assertNotIn("variant_corrected", plain)

    def test_content_mismatch_repurposed_listing(self):
        from unittest import mock

        intent = {"subject_keywords": ["macbook"], "exclude_keywords": ["dyson"]}
        # repurposed: slug/finding claims macbook, live page title is a Dyson straightener
        item = research.normalize_finding(
            {"title": "MacBook Air M2 space gray", "url": self.base_url + "/repurposed", "price": 17500, "currency": "UAH"},
            "codex", "t", "r1",
        )
        with mock.patch.object(research, "listing_key", return_value="olx:rep"):
            research.apply_live_check(item)
        self.assertTrue(research.content_mismatch(item, intent))
        self.assertIn("content_mismatch", research.rejection_reasons(item, {"ok": True}, None, intent))
        self.assertFalse(research.is_rescuable({"reasons": ["content_mismatch"]}))

        # genuine macbook page: live title carries the subject → no mismatch
        ok = research.normalize_finding(
            {"title": "MacBook Air M2", "url": self.base_url + "/macbook-ok", "price": 25000, "currency": "UAH"},
            "codex", "t", "r2",
        )
        with mock.patch.object(research, "listing_key", return_value="olx:ok"):
            research.apply_live_check(ok)
        self.assertFalse(research.content_mismatch(ok, intent))

    def test_content_mismatch_conservative_without_signal(self):
        # no live check / no title / no intent subject → never a mismatch (avoid false positives)
        self.assertFalse(research.content_mismatch({"title": "x"}, {"subject_keywords": ["macbook"]}))
        self.assertFalse(research.content_mismatch({"title": "x", "live_check": {"ok": True, "page_title": ""}}, {"subject_keywords": ["macbook"]}))
        self.assertFalse(research.content_mismatch({"title": "x", "live_check": {"ok": True, "page_title": "anything"}}, None))
        # finding NOT indexed on subject (its own title lacks the subject) → don't judge by live title
        self.assertFalse(research.content_mismatch(
            {"title": "random thing", "live_check": {"ok": True, "page_title": "other product"}},
            {"subject_keywords": ["macbook"]}))

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

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append(leg)
            return {"success": True, "stdout": '{"findings": []}', "record_id": "x", "leg": leg, "task_id": task_id}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            attempts: dict[str, set[str]] = {}
            config = research.make_config("standard", None)  # search_legs codex+gemini+claude
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                # A codex-sourced item is rescued by the OTHER families first, then codex, one leg/round.
                records, dropped = research.run_rechecks("p", [item], run_dir, config, 1, attempts)
                self.assertEqual(calls, ["gemini"])
                records, dropped = research.run_rechecks("p", [item], run_dir, config, 2, attempts)
                self.assertEqual(calls, ["gemini", "claude"])
                records, dropped = research.run_rechecks("p", [item], run_dir, config, 3, attempts)
                self.assertEqual(calls, ["gemini", "claude", "codex"])
                records, dropped = research.run_rechecks("p", [item], run_dir, config, 4, attempts)
                self.assertEqual(calls, ["gemini", "claude", "codex"])  # all legs exhausted
                self.assertEqual(records, [])

    def test_rescue_max_effort_uses_both_legs_and_counts_drops(self):
        import tempfile
        from unittest import mock

        items = [
            {"title": f"A{i}", "url": f"https://example.com/item{i}", "reasons": ["http_404"], "source_model": "codex"}
            for i in range(14)
        ]
        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "stdout": '{"findings": []}', "record_id": "x", "leg": leg, "task_id": task_id}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            config = research.make_config("max", None)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                records, dropped = research.run_rechecks("p", items, run_dir, config, 1, {})
        self.assertEqual(dropped, 6)  # 14 candidates, max profile caps at 8 per round
        self.assertEqual(len(records), 16)  # 8 items × recheck_legs=2 per item
        # max effort searches with 3 legs; a codex-sourced item is rescued by the 2 OTHER families.
        first_item_legs = {leg for leg, task_id in calls if task_id.startswith("recheck-1-1-")}
        self.assertEqual(first_item_legs, {"gemini", "claude"})

    def test_all_frontier_legs_search_at_every_level(self):
        # All three frontier families search at every level (they run in parallel), with a Claude
        # search budget that grows toward the deeper tiers.
        budgets = []
        for name in ("quick", "standard", "deep", "max"):
            cfg = research.make_config(name, None)
            self.assertEqual(cfg["search_legs"], ["codex", "gemini", "claude"])
            self.assertGreater(cfg["claude_search_budget"], 0)
            budgets.append(cfg["claude_search_budget"])
        self.assertEqual(budgets, sorted(budgets))  # non-decreasing with effort

    def test_vendor_tiers_default_max(self):
        cfg = research.make_config("deep", None)
        self.assertEqual(cfg["vendor_tiers"], {"codex": "xhigh", "gemini": "high", "claude": "opus"})
        self.assertEqual(cfg["search_effort"], "xhigh")
        self.assertEqual(cfg["judge_effort"], "xhigh")
        self.assertEqual(cfg["claude_model"], "opus")
        self.assertEqual(cfg["claude_search_model"], "opus")
        self.assertEqual(cfg["gemini_model"], "Gemini 3.1 Pro (High)")

    def test_vendor_tiers_override_applies_to_every_role(self):
        cfg = research.make_config("deep", None,
                                   vendor_tiers={"codex": "medium", "claude": "sonnet", "gemini": "low"})
        # One tier per vendor, applied to search AND judge seats alike.
        self.assertEqual(cfg["search_effort"], "medium")
        self.assertEqual(cfg["judge_effort"], "medium")
        self.assertEqual(cfg["claude_model"], "sonnet")
        self.assertEqual(cfg["claude_search_model"], "sonnet")
        self.assertEqual(cfg["gemini_model"], "Gemini 3.1 Pro (Low)")

    def test_vendor_tiers_invalid_values_fall_back_to_default(self):
        cfg = research.make_config("quick", None,
                                   vendor_tiers={"codex": "ultra", "claude": "fable", "gemini": "turbo"})
        # Unknown/over-ceiling tiers are ignored, not errored — fall back to the max default.
        self.assertEqual(cfg["vendor_tiers"], {"codex": "xhigh", "gemini": "high", "claude": "opus"})

    def test_make_config_excluded_sites(self):
        cfg = research.make_config("max", "https://www.OLX.ua/list, prom.ua", excluded_sites="olx.ua, rozetka.com.ua")
        self.assertEqual(cfg["sites"], ["olx.ua", "prom.ua"])
        # olx.ua is in scope -> dropped from the blocklist (scope wins); rozetka stays, normalized.
        self.assertEqual(cfg["excluded_sites"], ["rozetka.com.ua"])
        # blocklist alone, no scope
        self.assertEqual(research.make_config("quick", None, excluded_sites="ebay.com")["excluded_sites"], ["ebay.com"])

    def test_excluded_site_rejection(self):
        ex = ["rozetka.com.ua"]
        r = research.rejection_reasons({"title": "x", "price": 1, "url": "https://rozetka.com.ua/p1/", "availability": "available"},
                                       {"ok": True}, None, None, ex)
        self.assertIn("excluded_site", r)
        self.assertEqual(r.count("excluded_site"), 1)  # no duplicate
        # subdomain is caught too
        sub = research.rejection_reasons({"title": "x", "price": 1, "url": "https://m.rozetka.com.ua/p1/", "availability": "available"},
                                         {"ok": True}, None, None, ex)
        self.assertIn("excluded_site", sub)
        # an allowed domain is not flagged
        ok = research.rejection_reasons({"title": "x", "price": 1, "url": "https://olx.ua/d/1", "availability": "available"},
                                        {"ok": True}, None, None, ex)
        self.assertNotIn("excluded_site", ok)

    def test_excluded_site_non_rescuable(self):
        self.assertIn("excluded_site", research.NON_RESCUABLE_REASONS)
        self.assertFalse(research.is_rescuable({"reasons": ["excluded_site"]}))

    def test_search_and_decompose_prompts_carry_blocklist(self):
        cfg = research.make_config(3, excluded_sites="rozetka.com.ua")
        task = {"id": "t1", "query": "q", "focus": "f", "preferred_sites": []}
        sp = research.build_search_prompt("p", task, "codex", cfg)
        self.assertIn("BLOCKED", sp)
        self.assertIn("rozetka.com.ua", sp)
        # no blocklist -> no BLOCKED line (zero added tokens for the common case)
        self.assertNotIn("BLOCKED", research.build_search_prompt("p", task, "codex", research.make_config(3)))
        dp = research.build_decompose_prompt("find x", cfg)
        self.assertIn("rozetka.com.ua", dp)
        self.assertNotIn("rozetka.com.ua", research.build_decompose_prompt("find x", research.make_config(3)))

    def test_call_model_records_queue_wait(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "qw-run"
            (run_dir / "raw").mkdir(parents=True)
            research.init_leg_health("qw-run")
            try:
                # codex --probe is a real, fast subscription call; we only assert the meta shape.
                rec = research.call_model("codex", "--probe", run_dir, "decompose", "t", timeout=120, effort="medium")
                self.assertIn("queue_wait_sec", rec)
                self.assertIsInstance(rec["queue_wait_sec"], (int, float))
                self.assertGreaterEqual(rec["latency_sec"], 0)
            finally:
                research.clear_leg_health("qw-run")

    def test_kill_one_call(self):
        import subprocess

        run_id = "kill-one-run"
        try:
            proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
            research.register_proc(run_id, "rec-x", proc.pid)
            self.assertTrue(research.kill_one_call(run_id, "rec-x"))
            self.assertTrue(research.was_dropped_as_straggler(run_id, "rec-x"))
            proc.wait(timeout=10)
            self.assertFalse(research.kill_one_call(run_id, "nonexistent"))
        finally:
            research.clear_run_registry(run_id)

    def test_daily_call_counts_and_pacing(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            served = research.Path(tmp) / "served-models.jsonl"
            today = research.dt.datetime.now(research.dt.timezone.utc).strftime("%Y-%m-%d")
            rows = [
                {"ts": f"{today}T01:00:00Z", "leg": "gemini"},
                {"ts": f"{today}T02:00:00Z", "leg": "gemini"},
                {"ts": f"{today}T03:00:00Z", "leg": "codex"},
                {"ts": "2000-01-01T00:00:00Z", "leg": "gemini"},  # old, ignored
            ]
            served.write_text("\n".join(research.json.dumps(r) for r in rows) + "\n")
            with mock.patch.object(research, "SERVED_MODELS", served):
                counts = research.daily_call_counts()
                self.assertEqual(counts.get("gemini"), 2)
                self.assertEqual(counts.get("codex"), 1)
                with mock.patch.dict(research.DAILY_CAPS, {"gemini": 5}, clear=False):
                    budget, remaining = research.paced_budget("gemini", 9)
                    self.assertEqual(remaining, 3)   # cap 5 - used 2
                    self.assertEqual(budget, 3)      # clamped from 9 to remaining
                    budget2, _ = research.paced_budget("gemini", 2)
                    self.assertEqual(budget2, 2)     # request below remaining is untouched

    def test_build_scoreboard_shape(self):
        sb = research.build_scoreboard()
        self.assertIn("legs", sb)
        self.assertIn("generated_at", sb)
        legs = {lg["leg"]: lg for lg in sb["legs"]}
        # the capped legs always appear (even with zero history)
        for leg in ("gemini", "codex", "claude"):
            self.assertIn(leg, legs)
            self.assertIn("success_rate", legs[leg])
            self.assertIn("daily_cap", legs[leg])

    def test_scoreboard_history_bucketing(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            stats = research.Path(tmp) / "model-stats.jsonl"
            served = research.Path(tmp) / "served-models.jsonl"
            stats_rows = [
                {"ts": "2026-03-15T00:00:00Z", "leg": "codex", "success": True, "latency_sec": 10.0},
                {"ts": "2026-03-15T09:00:00Z", "leg": "codex", "success": False, "latency_sec": 20.0},
                {"ts": "2026-03-15T09:30:00Z", "leg": "gemini", "success": True, "latency_sec": 30.0},
                {"ts": "2026-03-14T23:59:59Z", "leg": "codex", "success": True, "latency_sec": 40.0},
                {"ts": "2026-03-01T12:00:00Z", "leg": "codex", "success": True, "latency_sec": 99.0},  # outside window
                {"ts": "2026-03-15T10:00:00Z", "success": True},  # no leg -> ignored
            ]
            served_rows = [
                {"ts": "2026-03-15T00:10:00Z", "leg": "codex", "weak_tier": 1},
                {"ts": "2026-03-15T00:20:00Z", "leg": "codex", "weak_tier": 0},
                {"ts": "2026-03-15T00:30:00Z", "leg": "gemini", "served": "QUOTA_EXHAUSTED"},
            ]
            stats.write_text("\n".join(research.json.dumps(r) for r in stats_rows) + "\n")
            served.write_text("\n".join(research.json.dumps(r) for r in served_rows) + "\n")
            with mock.patch.object(research, "MODEL_STATS", stats), mock.patch.object(research, "SERVED_MODELS", served):
                h = research.scoreboard_history(days=7, today="2026-03-15")

            self.assertEqual(len(h["days"]), 7)
            self.assertEqual(h["days"][0], "2026-03-09")
            self.assertEqual(h["days"][-1], "2026-03-15")
            legs = {s["leg"]: s for s in h["legs"]}
            self.assertEqual(set(legs), {"codex", "gemini"})
            for s in h["legs"]:                       # dense: one point per day per leg
                self.assertEqual(len(s["points"]), 7)
            codex = {p["day"]: p for p in legs["codex"]["points"]}
            self.assertEqual(codex["2026-03-15"]["calls"], 2)
            self.assertEqual(codex["2026-03-15"]["success_rate"], 0.5)
            self.assertEqual(codex["2026-03-15"]["avg_latency_sec"], 15.0)
            self.assertEqual(codex["2026-03-15"]["served_calls"], 2)
            self.assertEqual(codex["2026-03-15"]["weak_or_quota"], 1)
            # UTC day boundary: 23:59:59Z lands on 03-14, not 03-15
            self.assertEqual(codex["2026-03-14"]["calls"], 1)
            self.assertEqual(codex["2026-03-14"]["success_rate"], 1.0)
            self.assertNotIn("2026-03-01", codex)     # older than the 7-day window
            self.assertEqual(codex["2026-03-10"]["calls"], 0)   # zero-filled gap day
            self.assertIsNone(codex["2026-03-10"]["success_rate"])
            gemini = {p["day"]: p for p in legs["gemini"]["points"]}
            self.assertEqual(gemini["2026-03-15"]["weak_or_quota"], 1)   # QUOTA_EXHAUSTED served
            self.assertEqual(gemini["2026-03-15"]["success_rate"], 1.0)

    def test_scoreboard_history_empty(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            stats = research.Path(tmp) / "model-stats.jsonl"
            served = research.Path(tmp) / "served-models.jsonl"
            stats.write_text("")
            served.write_text("")
            with mock.patch.object(research, "MODEL_STATS", stats), mock.patch.object(research, "SERVED_MODELS", served):
                h = research.scoreboard_history(days=2, today="2026-03-15")
            self.assertEqual(h["legs"], [])
            self.assertEqual(h["days"], ["2026-03-14", "2026-03-15"])
            self.assertIn("generated_at", h)
            # missing files entirely must also degrade to an empty (but well-formed) history
            with mock.patch.object(research, "MODEL_STATS", research.Path(tmp) / "absent.jsonl"), \
                 mock.patch.object(research, "SERVED_MODELS", research.Path(tmp) / "absent2.jsonl"):
                h2 = research.scoreboard_history(days=1, today="2026-03-15")
            self.assertEqual(h2["legs"], [])
            self.assertEqual(len(h2["days"]), 1)

    def test_scoreboard_history_endpoint(self):
        import tempfile
        import threading
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            stats = research.Path(tmp) / "model-stats.jsonl"
            served = research.Path(tmp) / "served-models.jsonl"
            today = research.dt.datetime.now(research.dt.timezone.utc).strftime("%Y-%m-%d")
            stats.write_text(research.json.dumps({"ts": f"{today}T01:00:00Z", "leg": "codex", "success": True, "latency_sec": 5.0}) + "\n")
            served.write_text(research.json.dumps({"ts": f"{today}T01:05:00Z", "leg": "codex", "weak_tier": 0}) + "\n")
            with mock.patch.object(research, "MODEL_STATS", stats), mock.patch.object(research, "SERVED_MODELS", served):
                server = research.http.server.ThreadingHTTPServer(("127.0.0.1", 0), research.ResearchHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                    conn.request("GET", "/api/scoreboard/history?days=5")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn("application/json", response.getheader("Content-Type", ""))
                    payload = research.json.loads(response.read().decode("utf-8"))
                    conn.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
        self.assertEqual(len(payload["days"]), 5)
        legs = {s["leg"]: s for s in payload["legs"]}
        self.assertIn("codex", legs)
        # Midnight-safe: locate the point by the day we actually stamped the row with, rather than
        # asserting on points[-1] (the server's own "today" can differ if the clock rolls over UTC
        # midnight between writing the fixture and the endpoint computing the window).
        by_day = {p["day"]: p for p in legs["codex"]["points"]}
        self.assertIn(today, by_day)
        self.assertEqual(by_day[today]["calls"], 1)

    def test_agy_claude_reserve_absent_cli(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "agy-run"
            (run_dir / "raw").mkdir(parents=True)
            with mock.patch.object(research.shutil, "which", return_value=None):
                rec = research.call_agy_claude("hi", run_dir, "synthesize", "final-agy")
            self.assertFalse(rec["success"])
            self.assertEqual(rec["leg"], "claude-agy")

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
            # Carry a finding so the zero-findings guard (S2) does not extend the grace instead of
            # reaping — this test asserts the slow call IS killed once the quorum + findings are in.
            return {"record_id": f"fast-{latency}", "leg": "gemini", "task_id": "t",
                    "latency_sec": latency, "success": True,
                    "stdout": '{"findings":[{"title":"x","url":"https://e.com/1","price":10,"currency":"USD"}]}'}

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

    def test_emit_event_and_update_run_events(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            research.emit_event(run_dir, "run_created", prompt="p")
            research.update_run(run_dir, status="running", phase="decomposing")
            research.update_run(run_dir, phase="decomposing")  # no change -> no event
            research.update_run(run_dir, progress={"done": 1, "total": 4})
            lines = [
                research.json.loads(line)
                for line in (run_dir / "events.jsonl").read_text().splitlines()
            ]
        events = [row["event"] for row in lines]
        self.assertEqual(events, ["run_created", "status", "progress"])
        self.assertEqual(lines[1]["phase"], "decomposing")
        self.assertEqual(lines[2]["done"], 1)

    def test_verify_findings_streams_finding_settled_events(self):
        import tempfile
        from unittest import mock

        findings = [
            {"title": "Good", "url": "https://ok.example/1", "price": 10, "currency": "USD",
             "availability": "available", "source_model": "gemini"},
            {"title": "Dead", "url": "https://dead.example/2", "price": 20, "currency": "USD",
             "availability": "available", "source_model": "codex"},
        ]

        def fake_verify_url(url, **kwargs):
            return {"ok": True} if "ok.example" in (url or "") else {"ok": False, "reason": "http_404"}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            with mock.patch.object(research, "verify_url", side_effect=fake_verify_url), \
                 mock.patch.object(research, "apply_live_check", lambda item, intent=None, **kwargs: None):
                verified, rejected = research.verify_findings(
                    findings, [], None, None, run_dir=run_dir, stage="primary")
            rows = [research.json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]

        self.assertEqual(len(verified), 1)
        self.assertEqual(len(rejected), 1)
        settled = [r for r in rows if r["event"] == "finding_settled"]
        self.assertEqual(len(settled), 2)
        by_verdict = {r["verdict"]: r for r in settled}
        self.assertEqual(by_verdict["verified"]["url"], "https://ok.example/1")
        self.assertEqual(by_verdict["verified"]["stage"], "primary")
        self.assertEqual(by_verdict["rejected"]["url"], "https://dead.example/2")
        self.assertIn("http_404", by_verdict["rejected"]["reasons"])

    def test_verify_findings_silent_without_run_dir(self):
        # Back-compat: callers without a run_dir (and the unit tests) must not require an events file.
        from unittest import mock
        with mock.patch.object(research, "verify_url", lambda url, **kwargs: {"ok": False, "reason": "x"}):
            verified, rejected = research.verify_findings(
                [{"title": "A", "url": "https://e/1", "price": 1, "availability": "available"}])
        self.assertEqual(verified, [])
        self.assertEqual(len(rejected), 1)

    def test_record_stage_results_persists_and_summarizes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            verified = [{"title": "v", "url": "https://e/v"}]
            rejected = [{"title": "r", "url": "https://e/r"}, {"parse_failed": True, "reasons": ["parse_failed"]}]
            research.record_stage_results(run_dir, "rescue 1", verified, rejected)
            saved = research.json.loads((run_dir / "verification.json").read_text())
            rows = [research.json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]

        self.assertEqual(saved["stage"], "rescue 1")
        self.assertEqual(len(saved["verified"]), 1)
        summary = [r for r in rows if r["event"] == "stage_summary"][-1]
        self.assertEqual(summary["stage"], "rescue 1")
        self.assertEqual(summary["verified_total"], 1)
        # Counts match the persisted lists (and the live stream, which carries parse failures) so the
        # live view never disagrees with the poll fallback.
        self.assertEqual(summary["rejected_total"], 2)

    def test_verify_findings_streams_parse_failures_and_dedupes(self):
        import tempfile
        from unittest import mock

        good = {"title": "Good", "url": "https://ok.example/1", "price": 10, "currency": "USD",
                "availability": "available", "source_model": "gemini"}
        parse_rej = [{"parse_failed": True, "source_model": "codex", "task_id": "t1",
                      "record_id": "rec-1", "reasons": ["parse_failed"]}]

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            emitted = {}
            with mock.patch.object(research, "verify_url", lambda url, **kwargs: {"ok": True}), \
                 mock.patch.object(research, "apply_live_check", lambda item, intent=None, **kwargs: None):
                # Round 1: 1 finding + 1 parse failure -> 2 finding_settled events.
                research.verify_findings([good], parse_rej, None, None,
                                         run_dir=run_dir, stage="primary", emitted=emitted)
                # Round 2: re-verify the SAME cumulative set -> nothing changed -> no new events.
                research.verify_findings([good], parse_rej, None, None,
                                         run_dir=run_dir, stage="rescue 1", emitted=emitted)
            rows = [research.json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]

        settled = [r for r in rows if r["event"] == "finding_settled"]
        self.assertEqual(len(settled), 2, "re-verifying the same findings must not re-emit")
        self.assertTrue(any(s.get("parse_failed") and s["verdict"] == "rejected" for s in settled),
                        "parse failures must be streamed so the live view matches the poll")
        # First-seen stage wins (no re-emit in round 2), so both carry the primary stage.
        self.assertTrue(all(s["stage"] == "primary" for s in settled))

    def test_cancel_flow(self):
        import subprocess
        import tempfile

        run_id = "test-cancel-run"
        research.ACTIVE_RUNS.add(run_id)
        research.init_leg_health(run_id)
        try:
            proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
            research.register_proc(run_id, "rec-1", proc.pid)
            self.assertTrue(research.request_cancel(run_id))
            self.assertTrue(research.run_cancelled(run_id))
            proc.wait(timeout=10)  # killed by request_cancel

            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                (run_dir / "raw").mkdir(parents=True)
                record = research.call_model("gemini", "x", run_dir, "search", "t1")
                self.assertTrue(record["skipped_by_cancel"])
        finally:
            research.ACTIVE_RUNS.discard(run_id)
            research.clear_cancel(run_id)
            research.clear_leg_health(run_id)
            research.clear_run_registry(run_id)
        self.assertFalse(research.request_cancel("not-active-run"))

    def test_sse_stream_replays_events_and_closes(self):
        import http.client
        import tempfile
        import threading
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = research.Path(tmp)
            run_dir = runs_dir / "sse-run"
            run_dir.mkdir()
            research.write_json(run_dir / "run.json", {"run_id": "sse-run", "status": "completed"})
            research.emit_event(run_dir, "run_created", prompt="p")
            research.emit_event(run_dir, "status", status="completed", phase="completed")

            with mock.patch.object(research, "RUNS_DIR", runs_dir):
                server = research.http.server.ThreadingHTTPServer(("127.0.0.1", 0), research.ResearchHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                    conn.request("GET", "/api/runs/sse-run/events")
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn("text/event-stream", response.getheader("Content-Type", ""))
                    body = response.read().decode("utf-8")
                    conn.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
        self.assertIn('"event": "run_created"', body)
        self.assertIn("event: done", body)

    def test_rejection_rules(self):
        parsed = {"parse_failed": True}
        self.assertEqual(research.rejection_reasons(parsed), ["parse_failed"])

        missing_url = {"title": "A", "price": 1, "availability": "available"}
        self.assertIn("missing_url", research.rejection_reasons(missing_url))

        missing_price = {"title": "A", "url": "https://example.com", "availability": "available"}
        self.assertIn("missing_price", research.rejection_reasons(missing_price, {"ok": True}))

        sold = {"title": "A", "price": 1, "url": "https://example.com", "availability": "sold"}
        self.assertIn("out_of_stock", research.rejection_reasons(sold, {"ok": True}))

    # ---- researched best-practice adoption (all effort-gated; effort 1-2 must stay unchanged) ----

    def test_effort_1_2_run_fewer_passes(self):
        # Model TIER is max everywhere now (all frontier families search at every level); the levels
        # differ by NUMBER OF PASSES/breadth, not by weaker models. The light tiers keep the extra
        # passes OFF so quick/standard stay fast.
        for lvl in (1, 2):
            p = research.EFFORT_PROFILES[lvl]
            self.assertFalse(p["differentiate_legs"])
            self.assertEqual(p["angle_variants_per_task"], 0)
            self.assertEqual(p["coverage_rounds"], 0)
            self.assertEqual(p["frontier_rounds"], 0)
            self.assertEqual(p["adjudicate_samples"], 1)
        for lvl in (3, 4):
            p = research.EFFORT_PROFILES[lvl]
            self.assertTrue(p["differentiate_legs"])
            self.assertGreaterEqual(p["angle_variants_per_task"], 1)
            self.assertGreaterEqual(p["coverage_rounds"], 1)
            self.assertGreaterEqual(p["frontier_rounds"], 1)
            self.assertEqual(p["adjudicate_samples"], 3)
        # Structured decompose and the full frontier search roster are on at EVERY level now.
        for lvl in (1, 2, 3, 4):
            p = research.EFFORT_PROFILES[lvl]
            self.assertTrue(p["structured_decompose"])
            self.assertEqual(p["search_legs"], ["codex", "gemini", "claude"])

    def test_mmr_order_demotes_same_host_crowding(self):
        items = [
            {"url": "https://a.com/1", "title": "1", "price_usd": 10},
            {"url": "https://a.com/2", "title": "2", "price_usd": 11},
            {"url": "https://a.com/3", "title": "3", "price_usd": 12},
            {"url": "https://a.com/4", "title": "4", "price_usd": 13},
            {"url": "https://b.com/1", "title": "5", "price_usd": 14},  # lone different host, last by relevance
        ]
        out = research.mmr_order(items, lam=0.7)
        self.assertEqual(out[0]["url"], "https://a.com/1")  # top relevance still wins
        b_pos = next(i for i, it in enumerate(out) if it["url"] == "https://b.com/1")
        self.assertLess(b_pos, 4, "MMR should pull the lone different-host item out of last place")

    def test_aggregate_adjudications_majority_and_median(self):
        accept = lambda p: {"action": "accept", "price": p, "currency": "USD", "reason": "ok"}
        self.assertEqual(research.aggregate_adjudications([]), None)
        v = research.aggregate_adjudications([accept(100), accept(110), {"action": "reject", "reason": "x"}])
        self.assertEqual(v["action"], "accept")
        self.assertEqual(v["price"], 110)  # median of [100, 110]
        r = research.aggregate_adjudications([{"action": "reject"}, {"action": "reject"}, accept(100)])
        self.assertEqual(r["action"], "reject")

    def test_coerce_tasks_preserves_structured_fields_and_floor_2(self):
        cfg = research.make_config(3)
        payload = {"tasks": [
            {"id": "task-1", "query": "x", "source_class": "Official_Store", "angle": "exact SKU",
             "angle_variants": ["x bundle", "x lot"]},
            {"id": "task-2", "query": "y", "source_class": "classifieds"},
        ]}
        tasks = research.coerce_tasks(payload, "x", cfg)
        self.assertEqual(len(tasks), 2)  # floor is 2, not 3 (single-SKU plans allowed)
        self.assertEqual(tasks[0]["source_class"], "official_store")
        self.assertEqual(tasks[0]["angle_variants"], ["x bundle", "x lot"])

    def test_task_query_set_angle_variants_gated(self):
        task = {"id": "task-1", "query": "base", "query_variants": ["v2"], "angle_variants": ["angle1", "angle2"]}
        self.assertEqual(len(research.task_query_set(task, 2, 0)), 2)  # angles off
        expanded = research.task_query_set(task, 2, 2)
        self.assertEqual(len(expanded), 4)  # 2 surface + 2 angle
        self.assertTrue(any(tv.get("_angle") for tv in expanded))

    def test_coerce_intent_self_ask_fields(self):
        intent = research.coerce_intent({"intent": {
            "subject_keywords": ["x"], "ambiguous": True,
            "alternatives": ["Reading A", "Reading B"], "complexity": "single_sku",
            "clarify_question": "Which one did you mean?"}})
        self.assertTrue(intent["ambiguous"])
        self.assertEqual(intent["complexity"], "single_sku")
        # alternatives are user-facing (clarify buttons + report note) -> case is PRESERVED now
        self.assertEqual(intent["alternatives"], ["Reading A", "Reading B"])
        self.assertEqual(intent["clarify_question"], "Which one did you mean?")
        # unknown complexity is dropped; a blank clarify_question normalizes to None
        self.assertIsNone(research.coerce_intent({"intent": {"complexity": "bogus"}})["complexity"])
        self.assertIsNone(research.default_intent()["clarify_question"])
        self.assertIsNone(research.coerce_intent({"intent": {"clarify_question": "   "}})["clarify_question"])

    def test_decompose_prompt_gated_sections(self):
        quick = research.build_decompose_prompt("find x", research.make_config(1))
        deep = research.build_decompose_prompt("find x", research.make_config(3))
        for p in (quick, deep):
            self.assertIn("self-ask", p)        # §1.1 always on
            self.assertIn("complexity", p)      # §1.3 always on
            self.assertIn("COVERAGE GRID", p)   # structured decompose now on at every level
            self.assertIn("source_class", p)
        self.assertNotIn("angle_variants", quick)  # angle variants still gated to deep/max
        self.assertIn("angle_variants", deep)

    def test_search_prompt_leg_focus_and_angle(self):
        cfg = research.make_config(3)
        task = {"id": "t1", "query": "q", "focus": "f", "preferred_sites": []}
        base = research.build_search_prompt("p", task, "codex", cfg)
        self.assertNotIn("SOURCE-CLASS LEAN", base)
        leaned = research.build_search_prompt("p", task, "codex", cfg, leg_focus="classifieds")
        self.assertIn("SOURCE-CLASS LEAN", leaned)
        angled = research.build_search_prompt("p", {**task, "_angle": True}, "codex", cfg, leg_focus="regional")
        self.assertIn("ORTHOGONAL ANGLE", angled)
        # pinned-site runs suppress the soft lean (hard domain constraint already steers)
        pinned = research.build_search_prompt("p", task, "codex", research.make_config(3, sites="olx.ua"), leg_focus="classifieds")
        self.assertNotIn("SOURCE-CLASS LEAN", pinned)

    def test_covered_source_classes_maps_via_task_id(self):
        tasks = [{"id": "task-1", "source_class": "official_store"},
                 {"id": "task-2", "source_class": "classifieds"}]
        verified = [{"task_id": "task-1#v2"}, {"task_id": "task-1"}]
        self.assertEqual(research.covered_source_classes(verified, tasks), {"official_store"})

    def test_budget_remaining_and_stage_gate(self):
        cfg = {"time_budget_sec": 1500}
        now = research.time.monotonic()
        # ~0s elapsed: full budget remains and an optional stage fits.
        self.assertGreater(research.budget_remaining_sec(now, cfg), 1400)
        self.assertTrue(research.stage_fits_budget(now, cfg))
        # started long enough ago that only <= the reserve remains: optional stage is skipped.
        drained = now - (cfg["time_budget_sec"] - research.SYNTHESIS_RESERVE_SEC + 5)
        self.assertLessEqual(research.budget_remaining_sec(drained, cfg), research.SYNTHESIS_RESERVE_SEC)
        self.assertFalse(research.stage_fits_budget(drained, cfg))
        # no budget configured -> unbounded: remaining is None and every stage fits.
        self.assertIsNone(research.budget_remaining_sec(now, {}))
        self.assertTrue(research.stage_fits_budget(now, {}))

    def test_clamp_round_timeout_math(self):
        reserve = research.SYNTHESIS_RESERVE_SEC
        now = research.time.monotonic()
        cfg = {"time_budget_sec": reserve + 2000}  # 2000s of budget above the synthesis reserve
        # plenty left: remaining-reserve (~2000) > configured 300 -> configured wins.
        self.assertEqual(research.clamp_round_timeout(300, now, cfg), 300)
        # tighter: remaining-reserve ~400 < configured 600 -> clamps toward remaining-reserve.
        started = now - 1600  # remaining = reserve+2000-1600 = reserve+400 -> spendable ~400
        self.assertTrue(398 <= research.clamp_round_timeout(600, started, cfg) <= 400)
        # drained past the reserve: clamps to the 60s floor, never negative.
        empty = now - 2100  # remaining < reserve -> remaining-reserve negative
        self.assertEqual(research.clamp_round_timeout(600, empty, cfg), 60)
        # unbounded -> configured unchanged.
        self.assertEqual(research.clamp_round_timeout(600, now, {}), 600)

    def test_emit_deadline_skip_records_event_and_run_field(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            skipped = []
            research.emit_deadline_skip(run_dir, "frontier", 123.7, skipped)
            research.emit_deadline_skip(run_dir, "frontier", 100.0, skipped)  # same stage -> deduped
            research.emit_deadline_skip(run_dir, "final_factcheck", None, skipped)
            rows = [research.json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
            meta = research.read_json(run_dir / "run.json", {})
        skips = [r for r in rows if r["event"] == "stage_skipped_deadline"]
        self.assertEqual([r["stage"] for r in skips], ["frontier", "frontier", "final_factcheck"])
        self.assertEqual(skips[0]["remaining_sec"], 123)  # int-truncated
        self.assertIsNone(skips[2]["remaining_sec"])       # unbounded -> None
        self.assertEqual(skipped, ["frontier", "final_factcheck"])            # deduped accumulation
        self.assertEqual(meta["skipped_by_deadline"], ["frontier", "final_factcheck"])  # persisted

    def test_decompose_prompt_plan_and_solve_and_exclusivity(self):
        p = research.build_decompose_prompt("find x", research.make_config(3))
        self.assertIn("Plan-and-Solve", p)   # plan-then-tasks framing near the top
        self.assertIn("EXTRACT", p)
        self.assertIn("MUTUALLY EXCLUSIVE", p)  # task-boundary contract

    def test_decompose_prompt_exclude_keyword_guard(self):
        p = research.build_decompose_prompt("find x", research.make_config(3))
        self.assertIn("NEVER add generic words that co-occur with valid offers", p)
        self.assertIn('bad: ["prompt", "free", "api"]', p)

    def test_recheck_and_frontier_prompts_include_do_not_report_urls(self):
        known = [f"https://ex.com/{i}" for i in range(25)]
        rp = research.build_recheck_prompt(
            "find x", {"title": "t", "url": "https://ex.com/rejected"}, research.make_config(3), known)
        self.assertIn("Do NOT re-report", rp)
        self.assertIn("https://ex.com/0", rp)
        self.assertIn("https://ex.com/19", rp)   # 20th URL kept
        self.assertNotIn("https://ex.com/20", rp)  # capped at 20
        fp = research.build_frontier_prompt("find x", 100.0, [], {"subject_keywords": ["x"]}, None, known)
        self.assertIn("Do NOT re-report", fp)
        self.assertIn("https://ex.com/0", fp)
        self.assertNotIn("https://ex.com/20", fp)
        # no known URLs -> no block at all
        self.assertNotIn("Do NOT re-report",
                         research.build_recheck_prompt("find x", {"title": "t"}, research.make_config(3)))
        self.assertNotIn("Do NOT re-report", research.build_frontier_prompt("find x", 100.0, [], None))

    def test_synthesis_prompt_ambiguity_and_skip_note(self):
        finding = {"title": "x", "url": "https://e", "price_usd": 10}
        p = research.build_synthesis_prompt(
            "find x", [], [finding], [], {"sites": [], "judge_effort": "xhigh"}, None,
            {"subject_keywords": ["x"], "ambiguous": True, "alternatives": ["a", "b"]},
            ["coverage", "frontier"],
        )
        self.assertIn("intent.ambiguous", p)
        self.assertIn("proceeded on ONE assumed", p)  # assumption-statement instruction
        self.assertIn("verification_stages_skipped_deadline", p)  # skipped-stage degradation note wired in

    # ---- interactive clarify gate ----

    def test_decompose_prompt_clarify_instruction(self):
        p = research.build_decompose_prompt("find x", research.make_config(2))
        self.assertIn("clarify_question", p)                    # emit-a-question instruction present
        self.assertIn("SAME language as the user request", p)   # question in the user's language
        # the first-alternative-is-the-default rule the UI/synthesis rely on
        self.assertIn("FIRST entry in intent.alternatives MUST be the assumed/default reading", p)

    def test_make_config_interactive_default_and_passthrough(self):
        self.assertFalse(research.make_config("standard", None)["interactive"])   # default off
        self.assertFalse(research.make_config("standard", None, interactive=None)["interactive"])
        self.assertTrue(research.make_config("standard", None, interactive=True)["interactive"])

    def test_should_ask_clarify_gate(self):
        intent = {"ambiguous": True, "clarify_question": "Which region?", "alternatives": ["EU", "US"]}
        ask, q, alts = research.should_ask_clarify({"interactive": True}, intent)
        self.assertTrue(ask)
        self.assertEqual(q, "Which region?")
        self.assertEqual(alts, ["EU", "US"])
        # NON-interactive runs must NEVER ask (scripts never block), even on an ambiguous plan
        self.assertFalse(research.should_ask_clarify({"interactive": False}, intent)[0])
        self.assertFalse(research.should_ask_clarify({}, intent)[0])
        # interactive but no concrete question / not ambiguous -> no ask
        self.assertFalse(research.should_ask_clarify({"interactive": True},
                                                     {"ambiguous": True, "clarify_question": ""})[0])
        self.assertFalse(research.should_ask_clarify({"interactive": True},
                                                     {"ambiguous": False, "clarify_question": "q"})[0])

    def test_submit_and_read_clarification(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            try:
                entry = research.submit_clarification(run_dir, "sub1", {"answer": "  the max plan  "})
                self.assertEqual(entry, {"answer": "the max plan", "skip": False})
                # registry fast-path AND clarify.json on disk both carry it (belt-and-suspenders)
                self.assertEqual(research.read_clarification(run_dir, "sub1")["answer"], "the max plan")
                self.assertEqual(research.read_json(run_dir / "clarify.json", None)["answer"], "the max plan")
                # explicit skip and a blank answer both normalize to skip=True ("proceed now")
                self.assertTrue(research.submit_clarification(run_dir, "sub1", {"skip": True})["skip"])
                self.assertTrue(research.submit_clarification(run_dir, "sub1", {"answer": ""})["skip"])
            finally:
                research.clear_clarification("sub1")

    def test_wait_for_clarification(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            try:
                # pre-written clarify.json -> returned immediately (even before the deadline check)
                research.write_json(run_dir / "clarify.json", {"answer": "EU", "skip": False})
                got = research.wait_for_clarification(run_dir, "w1", timeout_sec=5, poll_interval=0.01)
                self.assertEqual(got["answer"], "EU")
            finally:
                research.clear_clarification("w1")

        with tempfile.TemporaryDirectory() as tmp2:
            run_dir2 = research.Path(tmp2)
            try:
                # nothing written -> None on a tiny timeout, and it returns quickly
                t0 = research.time.monotonic()
                self.assertIsNone(
                    research.wait_for_clarification(run_dir2, "w2", timeout_sec=0.05, poll_interval=0.01))
                self.assertLess(research.time.monotonic() - t0, 2.0)
                # a skip payload is a DISTINCT result from an answer (skip=True, empty answer)
                research.write_json(run_dir2 / "clarify.json", {"answer": "", "skip": True})
                got2 = research.wait_for_clarification(run_dir2, "w2", timeout_sec=1, poll_interval=0.01)
                self.assertTrue(got2["skip"])
                self.assertEqual(got2["answer"], "")
            finally:
                research.clear_clarification("w2")

    def test_wait_for_clarification_aborts_on_cancel(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)

            def boom():
                raise research.RunCancelled()

            with self.assertRaises(research.RunCancelled):
                research.wait_for_clarification(run_dir, "wc", timeout_sec=5,
                                                check_cancel=boom, poll_interval=0.01)

    def test_clarify_wait_excluded_from_budget(self):
        # Replicates execute_research's mechanism on a controllable clock: while parked on the clarify
        # question `now` advances, but `started` is advanced by the SAME amount, so the waited seconds
        # are excluded from budget_remaining_sec (the run's search window is not shortened).
        from unittest import mock

        cfg = {"time_budget_sec": 1500}
        clock = {"t": 1000.0}
        with mock.patch.object(research.time, "monotonic", lambda: clock["t"]):
            started = research.time.monotonic()                    # run starts at t=1000
            clock["t"] = 1030.0                                    # 30s of real search elapsed
            remaining_before_wait = research.budget_remaining_sec(started, cfg)
            wait_started = research.time.monotonic()               # enter clarify at t=1030
            clock["t"] = 1055.0                                    # user ponders 25s
            started += research.time.monotonic() - wait_started    # <- exclusion: started += 25
            remaining_after_wait = research.budget_remaining_sec(started, cfg)
            # and WITHOUT the exclusion those 25s would have been billed:
            billed = research.budget_remaining_sec(started - 25.0, cfg)
        self.assertAlmostEqual(remaining_before_wait, 1470.0, delta=0.001)
        self.assertAlmostEqual(remaining_after_wait, 1470.0, delta=0.001)  # wait cost the budget nothing
        self.assertAlmostEqual(billed, 1445.0, delta=0.001)                # the 25s would otherwise vanish

    def test_clarify_endpoint_records_answer(self):
        import http.client
        import json as _json
        import tempfile
        import threading
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = research.Path(tmp)
            run_dir = runs_dir / "clar-run"
            run_dir.mkdir()
            research.write_json(run_dir / "run.json", {"run_id": "clar-run", "status": "running"})
            with mock.patch.object(research, "RUNS_DIR", runs_dir):
                server = research.http.server.ThreadingHTTPServer(("127.0.0.1", 0), research.ResearchHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                    conn.request("POST", "/api/runs/clar-run/clarify", body=_json.dumps({"answer": "EU region"}),
                                 headers={"Content-Type": "application/json"})
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 202)
                    resp.read()
                    conn.close()
                    # a POST to a run that does not exist -> 404
                    conn2 = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                    conn2.request("POST", "/api/runs/nope/clarify", body="{}",
                                  headers={"Content-Type": "application/json"})
                    self.assertEqual(conn2.getresponse().status, 404)
                    conn2.close()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
            try:
                stored = research.read_clarification(run_dir, "clar-run")
                self.assertEqual(stored["answer"], "EU region")
                self.assertFalse(stored["skip"])
            finally:
                research.clear_clarification("clar-run")

    # ---- adversarial-review fixes (findings 1-6) ----

    def test_build_clarified_prompt_folds_answer_and_execute_research_rebinds(self):
        original = "cheapest gpu"
        p = research.build_clarified_prompt(original, "for gaming, EU region")
        self.assertTrue(p.startswith(original))  # original kept verbatim
        self.assertIn("User clarification (authoritative): for gaming, EU region", p)
        self.assertNotEqual(p, original)
        # The rebind must wire into execute_research's LOCAL prompt (not only the re-decompose), so
        # every downstream stage (search, gap audit, coverage/frontier, rescue, synthesis, fact-check)
        # sees the disambiguated request. Guard the wiring, not just the helper.
        import inspect
        src = inspect.getsource(research.execute_research)
        self.assertIn("prompt = build_clarified_prompt(prompt", src)

    def test_protected_proc_survives_straggler_kill_but_not_cancel(self):
        import subprocess
        run_id = "test-protected-run"
        prot = subprocess.Popen(["sleep", "30"], start_new_session=True)
        reg = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            research.register_proc(run_id, "prot-rec", prot.pid, protected=True)
            research.register_proc(run_id, "reg-rec", reg.pid)
            killed = research.kill_stragglers(run_id)  # a fan-out phase's quorum kill
            self.assertIn("reg-rec", killed)
            self.assertNotIn("prot-rec", killed)  # overlapping audit call is protected
            self.assertTrue(research.was_dropped_as_straggler(run_id, "reg-rec"))
            self.assertFalse(research.was_dropped_as_straggler(run_id, "prot-rec"))
            # Cancellation reaps EVERYTHING, protected audits included.
            research.ACTIVE_RUNS.add(run_id)
            self.assertTrue(research.request_cancel(run_id))
            self.assertTrue(research.was_dropped_as_straggler(run_id, "prot-rec"))
        finally:
            for p in (prot, reg):
                try:
                    p.kill()
                except Exception:
                    pass
                try:
                    p.wait(timeout=3)
                except Exception:
                    pass
            research.clear_run_registry(run_id)
            research.ACTIVE_RUNS.discard(run_id)
            with research.CANCEL_LOCK:
                research.CANCELLED_RUNS.discard(run_id)

    def test_recheck_do_not_report_excludes_items_own_url(self):
        # A disputed item lives inside `verified`, so its own URL is in known_urls. The rescue asks the
        # model to RECOVER this exact item, so its own URL (and price-candidate URLs) must NOT appear in
        # the "do NOT re-report" block — while OTHER verified URLs still steer the round toward novelty.
        item = {"title": "t", "url": "https://ex.com/own?utm_source=ad",
                "price_candidates": [{"url": "https://ex.com/cand"}]}
        known = ["https://ex.com/own", "https://ex.com/other-a",
                 "https://ex.com/cand?utm_source=x", "https://ex.com/other-b"]
        filtered = research.known_urls_minus_item(known, item)
        self.assertNotIn("https://ex.com/own", filtered)             # own url dropped (tracking-normalized)
        self.assertNotIn("https://ex.com/cand?utm_source=x", filtered)  # own price-candidate url dropped too
        self.assertIn("https://ex.com/other-a", filtered)
        self.assertIn("https://ex.com/other-b", filtered)
        block = research.do_not_report_block(filtered)
        self.assertNotIn("ex.com/own", block)
        self.assertNotIn("ex.com/cand", block)
        self.assertIn("ex.com/other-a", block)
        self.assertIn("ex.com/other-b", block)

    def test_post_synthesis_stages_gate_on_smaller_reserve_and_warn_on_skip(self):
        import inspect
        # The post-synthesis reserve is much smaller than the synthesis reserve, so review/fact-check
        # (which run AFTER synthesis) aren't skipped with plenty of budget left.
        self.assertLess(research.POST_SYNTHESIS_RESERVE_SEC, research.SYNTHESIS_RESERVE_SEC)
        cfg = {"time_budget_sec": 1000}
        # remaining sits BETWEEN the two reserves: a pre-synthesis optional stage skips, a
        # post-synthesis stage still fits.
        remaining = (research.POST_SYNTHESIS_RESERVE_SEC + research.SYNTHESIS_RESERVE_SEC) / 2
        started = research.time.monotonic() - (1000 - remaining)
        self.assertFalse(research.stage_fits_budget(started, cfg))  # default (synthesis) reserve -> skip
        self.assertTrue(research.stage_fits_budget(
            started, cfg, reserve_sec=research.POST_SYNTHESIS_RESERVE_SEC))
        # execute_research gates BOTH post-synthesis stages on the smaller reserve and, when it skips
        # either (decided after synthesize consumed skipped_by_deadline), prepends a visible callout.
        src = inspect.getsource(research.execute_research)
        self.assertEqual(src.count("reserve_sec=POST_SYNTHESIS_RESERVE_SEC"), 2)
        self.assertIn("NOT REVIEWED", src)
        self.assertIn("NOT FACT-CHECKED", src)

    def test_straggler_drop_rekills_late_job_within_grace(self):
        import concurrent.futures
        import subprocess
        import tempfile
        import time

        run_id = "test-late-straggler-run"

        def fast_job(i):
            time.sleep(0.05)
            # Carry a finding so the zero-findings guard (S2) reaps rather than extends the grace.
            return {"record_id": f"fast-{i}", "leg": "gemini", "task_id": "t", "latency_sec": 0.1,
                    "success": True,
                    "stdout": '{"findings":[{"title":"x","url":"https://e.com/1","price":10,"currency":"USD"}]}'}

        def late_slow_job():
            # Spawns its tracked subprocess only AFTER the first straggler sweep would have fired,
            # mimicking a late audit-added task. The re-firing kill must still reap it, so the
            # post-quorum wait stays bounded instead of stretching to the full 30s.
            time.sleep(1.5)
            proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
            research.register_proc(run_id, "late-rec", proc.pid)
            try:
                proc.wait()
            finally:
                research.unregister_proc(run_id, "late-rec")
            return {"record_id": "late-rec", "latency_sec": 30.0, "success": False}

        config = dict(research.make_config("quick", None))
        config["straggler_grace_sec"] = 1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                run_dir.mkdir()
                started = time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [
                        executor.submit(fast_job, 1),
                        executor.submit(fast_job, 2),
                        executor.submit(fast_job, 3),
                        executor.submit(late_slow_job),
                    ]
                    records = research.collect_with_straggler_drop(futures, run_dir, config)
                elapsed = time.monotonic() - started
            self.assertEqual(len(records), 4)
            self.assertLess(elapsed, 15.0)  # re-fired kill reaped the late job; not awaited to 30s
            self.assertTrue(research.was_dropped_as_straggler(run_id, "late-rec"))
        finally:
            research.clear_run_registry(run_id)

    # ---- Round 6: S1 quorum hygiene ----
    _FINDING_STDOUT = '{"findings":[{"title":"x","url":"https://e.com/1","price":10,"currency":"USD"}]}'

    def test_s1_failed_fast_records_shrink_quorum_base(self):
        # A fast leg that RAN AND FAILED (success=False) must not advance the quorum — it shrinks the
        # base instead. 3 fast (1 ok+finding, 2 failed) + 1 slow: effective base = 3-2 = 1, so the one
        # successful fast meets quorum and the slow straggler is reaped.
        import concurrent.futures
        import tempfile
        import threading as th
        from unittest import mock

        release = th.Event()

        def ok_fast():
            return {"leg": "gemini", "record_id": "ok", "task_id": "t", "latency_sec": 0.01,
                    "success": True, "stdout": self._FINDING_STDOUT}

        def bad_fast(rid):
            return {"leg": "gemini", "record_id": rid, "task_id": "t", "latency_sec": 0.01, "success": False}

        def slow():
            release.wait(5)
            return {"leg": "codex", "record_id": "s", "task_id": "t", "latency_sec": 0.01, "success": True}

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        futs = [pool.submit(ok_fast), pool.submit(bad_fast, "b1"), pool.submit(bad_fast, "b2"), pool.submit(slow)]
        for f in futs[:3]:
            f.result()
        config = dict(research.make_config("standard", None))
        config["straggler_grace_sec"] = 0.2
        killed = th.Event()

        def fake_kill(run_id, include_protected=False):
            killed.set(); release.set(); return []

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "q"
            run_dir.mkdir()
            with mock.patch.object(research, "kill_stragglers", fake_kill):
                records = research.collect_with_straggler_drop(futs, run_dir, config, fast_quorum_total=3)
        pool.shutdown(wait=True)
        self.assertTrue(killed.is_set())
        self.assertEqual(len(records), 4)

    def test_s1_all_fast_dead_disengages_quorum(self):
        # Every fast leg is dead -> the quorum disengages: NO deadline is armed, so the phase waits
        # for the slow leg to finish on its own rather than a grace timer reaping the only leg that
        # can still deliver.
        import concurrent.futures
        import tempfile
        import threading as th
        import time
        from unittest import mock

        def bad_fast(rid):
            return {"leg": "gemini", "record_id": rid, "task_id": "t", "latency_sec": 0.01, "success": False}

        def slow():
            time.sleep(0.4)
            return {"leg": "codex", "record_id": "s", "task_id": "t", "latency_sec": 0.01,
                    "success": True, "stdout": self._FINDING_STDOUT}

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        futs = [pool.submit(bad_fast, "b1"), pool.submit(bad_fast, "b2"), pool.submit(slow)]
        for f in futs[:2]:
            f.result()
        config = dict(research.make_config("standard", None))
        config["straggler_grace_sec"] = 0.05
        killed = th.Event()

        def fake_kill(run_id, include_protected=False):
            killed.set(); return []

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "q"
            run_dir.mkdir()
            with mock.patch.object(research, "kill_stragglers", fake_kill):
                records = research.collect_with_straggler_drop(futs, run_dir, config, fast_quorum_total=2)
        pool.shutdown(wait=True)
        self.assertFalse(killed.is_set())  # quorum disengaged: slow leg awaited, not reaped
        self.assertEqual(len(records), 3)
        self.assertTrue(any(r["record_id"] == "s" for r in records))

    # ---- Round 6: S2 zero-findings reaper guard ----
    def test_s2_zero_findings_extends_grace_instead_of_killing(self):
        # Quorum met but NOTHING found yet: the reaper guard extends the grace (emitting
        # straggler_grace_extended) and awaits the pending call rather than guaranteeing an empty
        # phase. The loop still terminates once the pending call completes.
        import concurrent.futures
        import tempfile
        import threading as th
        import time
        from unittest import mock

        def empty_fast(rid):
            return {"leg": "gemini", "record_id": rid, "task_id": "t", "latency_sec": 0.01,
                    "success": True, "stdout": ""}

        # Outlasts the 1s minimum wait clamp so the grace deadline actually expires mid-wait (with the
        # pending call still running) and the zero-findings guard fires before the call completes.
        def slow():
            time.sleep(1.3)
            return {"leg": "codex", "record_id": "s", "task_id": "t", "latency_sec": 0.01,
                    "success": True, "stdout": self._FINDING_STDOUT}

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        futs = [pool.submit(empty_fast, "f1"), pool.submit(empty_fast, "f2"), pool.submit(slow)]
        for f in futs[:2]:
            f.result()
        config = dict(research.make_config("standard", None))
        config["straggler_grace_sec"] = 0.05
        killed = th.Event()

        def fake_kill(run_id, include_protected=False):
            killed.set(); return []

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / "q"
            run_dir.mkdir()
            with mock.patch.object(research, "kill_stragglers", fake_kill):
                records = research.collect_with_straggler_drop(futs, run_dir, config, fast_quorum_total=2)
            events = [research.json.loads(l)["event"]
                      for l in (run_dir / "events.jsonl").read_text().splitlines()]
        pool.shutdown(wait=True)
        self.assertFalse(killed.is_set())  # zero findings -> extend, never kill
        self.assertIn("straggler_grace_extended", events)
        self.assertEqual(len(records), 3)  # loop terminated once the slow call completed

    # ---- Round 6: Q1 model-assisted verification ----
    def test_model_verify_eligible_predicate(self):
        def item(reasons, url="https://e.com/1", price=100):
            return {"reasons": reasons, "url": url, "price": price, "price_usd": price}
        self.assertTrue(research.model_verify_eligible(item(["bot_blocked"])))
        self.assertTrue(research.model_verify_eligible(item(["http_403"])))
        self.assertTrue(research.model_verify_eligible(item(["timeout", "missing_price"])))
        self.assertFalse(research.model_verify_eligible(item(["off_intent"])))        # semantic
        self.assertFalse(research.model_verify_eligible(item(["bot_blocked", "wrong_tier"])))  # mixed
        self.assertFalse(research.model_verify_eligible(item(["bot_blocked"], url=None)))       # no url
        self.assertFalse(research.model_verify_eligible({"reasons": ["bot_blocked"], "url": "https://e.com/1"}))  # no price
        self.assertFalse(research.model_verify_eligible(item([])))                    # no reasons
        self.assertFalse(research.model_verify_eligible(item(["missing_price"])))     # no genuine network reason

    def test_apply_model_verdict_promotes_with_confidence_penalty(self):
        finding = research.normalize_finding(
            {"title": "MacBook Air M2", "url": "https://walmart.com/ip/1", "price": 199, "currency": "USD"},
            "gemini", "t", "r")
        finding["reasons"] = ["bot_blocked"]
        mv = {research.dedupe_key(finding): {"live": True, "price": 205, "currency": "USD",
              "title": "MacBook Air M2", "availability": "available", "seller": "Walmart",
              "url": "https://walmart.com/ip/1", "leg": "gemini"}}
        verified, rejected = research.verify_findings([finding], None, None, None, model_verdicts=mv)
        self.assertEqual(len(verified), 1)
        self.assertEqual(len(rejected), 0)
        v = verified[0]
        self.assertTrue(v.get("model_verified"))
        self.assertEqual(v["price"], 205)
        self.assertEqual((v.get("url_check") or {}).get("method"), "model")
        research.apply_trust(verified)
        research.apply_confidence(verified)
        self.assertTrue(any("model-verified" in f for f in v["confidence_calibrated"]["factors"]))
        # Below a machine-live-verified equivalent, but well above an unverified item.
        live_equiv = dict(v)
        live_equiv.pop("model_verified", None)
        live_equiv["live_check"] = {"ok": True, "live_price": 205}
        self.assertLess(v["confidence_calibrated"]["score"], research.calibrate_confidence(live_equiv)["score"])

    def test_apply_model_verdict_rejects_on_not_live(self):
        finding = research.normalize_finding(
            {"title": "x", "url": "https://e.com/1", "price": 50, "currency": "USD"}, "gemini", "t", "r")
        mv = {research.dedupe_key(finding): {"live": False, "leg": "gemini", "notes": "could not open"}}
        verified, rejected = research.verify_findings([finding], None, None, None, model_verdicts=mv)
        self.assertEqual(len(verified), 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("model_check_failed", rejected[0]["reasons"])
        self.assertFalse(research.is_rescuable(rejected[0]))          # final — will not loop back
        self.assertFalse(research.model_verify_eligible(rejected[0]))  # nor re-enter model-verify

    def test_run_model_verify_records_verdicts_top_k_and_leg_fallback(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=240, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "leg": leg, "task_id": task_id, "record_id": task_id,
                    "stdout": '{"live": true, "price": 150, "currency": "USD", "title": "x"}'}

        def mk(price, url):
            return {"reasons": ["bot_blocked"], "url": url, "price": price, "price_usd": price, "title": "t"}

        rejected = [mk(300, "https://e.com/3"), mk(100, "https://e.com/1"), mk(200, "https://e.com/2")]
        cfg = dict(research.make_config("standard", None))
        cfg["model_verify_cap"] = 2
        run_id = "mv-topk"
        research.init_leg_health(run_id)
        research.init_leg_budget(run_id, {"gemini": 5, "claude": 5})
        research.force_disable_leg(run_id, "claude", "test")  # force the claude->gemini fallback
        verdicts: dict = {}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                run_dir.mkdir()
                with mock.patch.object(research, "call_model", side_effect=fake_call):
                    checked = research.run_model_verify("p", rejected, run_dir, cfg, None, verdicts)
        finally:
            research.clear_leg_health(run_id)
            research.clear_leg_budget(run_id)
        self.assertEqual(checked, 2)
        self.assertEqual([leg for leg, _ in calls], ["gemini", "gemini"])       # claude disabled -> gemini
        self.assertIn(research.dedupe_key({"url": "https://e.com/1"}), verdicts)  # cheapest selected
        self.assertIn(research.dedupe_key({"url": "https://e.com/2"}), verdicts)
        self.assertNotIn(research.dedupe_key({"url": "https://e.com/3"}), verdicts)  # dearest dropped by cap

    def test_run_model_verify_skips_when_no_web_leg(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(*a, **k):
            calls.append(a)
            return {"success": True}

        rejected = [{"reasons": ["bot_blocked"], "url": "https://e.com/1", "price": 100, "price_usd": 100}]
        cfg = dict(research.make_config("standard", None))
        run_id = "mv-skip"
        research.init_leg_health(run_id)
        research.init_leg_budget(run_id, {"gemini": 5, "claude": 5})
        research.force_disable_leg(run_id, "claude", "test")
        research.force_disable_leg(run_id, "gemini", "test")
        verdicts: dict = {}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run_dir = research.Path(tmp) / run_id
                run_dir.mkdir()
                with mock.patch.object(research, "call_model", side_effect=fake_call):
                    checked = research.run_model_verify("p", rejected, run_dir, cfg, None, verdicts)
        finally:
            research.clear_leg_health(run_id)
            research.clear_leg_budget(run_id)
        self.assertEqual(checked, 0)
        self.assertEqual(calls, [])
        self.assertEqual(verdicts, {})

    def test_run_rechecks_excludes_model_verify_eligible(self):
        import tempfile
        from unittest import mock

        prompts = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=300, effort="medium", claude_model=None):
            prompts.append(prompt)
            return {"success": True, "leg": leg, "task_id": task_id, "record_id": task_id, "stdout": '{"findings": []}'}

        # 4 network-only items against model_verify_cap=3: the top-3 cheapest go to model-verify
        # (excluded from rescue), the overflow item must KEEP the rescue path — otherwise it would
        # lose both recovery attempts.
        net_items = [{"reasons": ["bot_blocked"], "url": f"https://e.com/net{i}", "price": 100 + i,
                      "price_usd": 100 + i, "title": f"net{i}", "source_model": "gemini"}
                     for i in range(4)]
        sem_item = {"reasons": ["excluded_by_keyword"], "url": "https://e.com/sem", "price": 100,
                    "price_usd": 100, "title": "sem", "source_model": "gemini"}
        cfg = dict(research.make_config("standard", None))
        self.assertEqual(cfg["model_verify_cap"], 3)
        run_id = "rc-excl"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / run_id
            run_dir.mkdir()
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_rechecks("p", net_items + [sem_item], run_dir, cfg, 1, {})
        self.assertTrue(any("e.com/sem" in p for p in prompts))     # semantic reject -> still rescued
        for i in range(3):
            self.assertFalse(any(f"e.com/net{i}" in p for p in prompts))  # top-K -> model-verify
        self.assertTrue(any("e.com/net3" in p for p in prompts))   # beyond the cap -> rescue kept

    def test_select_model_verify_candidates_usd_ranking(self):
        # Mixed currencies must rank by USD, not raw native numbers: 4500 (UAH, no USD conversion)
        # must NOT beat $120 for the last verify slot — unconverted prices sort last.
        native = {"reasons": ["bot_blocked"], "url": "https://e.com/uah", "price": 4500,
                  "price_usd": None, "title": "native"}
        usd = {"reasons": ["bot_blocked"], "url": "https://e.com/usd", "price": 120,
               "price_usd": 120, "title": "usd"}
        cfg = {"model_verify_cap": 1}
        picked = research.select_model_verify_candidates([native, usd], cfg)
        self.assertEqual([it["url"] for it in picked], ["https://e.com/usd"])
        self.assertEqual(research.select_model_verify_candidates([native, usd], {"model_verify_cap": 0}), [])

    # ---- Round 6: P1 slow-leg cap in follow-up rounds ----
    def test_rechecks_slow_leg_cap(self):
        import tempfile
        from unittest import mock

        legs_used = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=480, effort="medium", claude_model=None):
            legs_used.append(leg)
            return {"success": True, "leg": leg, "task_id": task_id, "record_id": task_id, "stdout": '{"findings": []}'}

        # Rescuable but SEMANTIC (excluded_by_keyword) so they are not model-verify-eligible (else the
        # rescue loop would drop them); source=gemini so codex is a preferred non-source leg.
        items = [{"reasons": ["excluded_by_keyword"], "url": f"https://e.com/{i}", "price": 10 + i,
                  "price_usd": 10 + i, "title": f"t{i}", "source_model": "gemini"} for i in range(6)]
        cfg = dict(research.make_config("deep", None))  # codex_task_cap 3, recheck_legs 1, max_recheck_items 6
        self.assertEqual(cfg["codex_task_cap"], 3)
        run_id = "rc-cap"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp) / run_id
            run_dir.mkdir()
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_rechecks("p", items, run_dir, cfg, 1, {})
        self.assertEqual(legs_used.count("codex"), 3)  # exactly the per-phase cap, not one per item

    def test_primary_codex_cap_spans_main_and_audit(self):
        import tempfile
        from unittest import mock

        calls = []

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "leg": leg, "task_id": task_id, "record_id": task_id, "stdout": '{"findings": []}'}

        class Supplier:
            def result(self, timeout=None):
                return [{"id": "audit-1", "query": "aq1", "query_variants": [], "preferred_sites": []},
                        {"id": "audit-2", "query": "aq2", "query_variants": [], "preferred_sites": []}]

        tasks = [{"id": f"task-{i}", "query": f"q{i}", "query_variants": [], "preferred_sites": []}
                 for i in range(1, 4)]  # 3 main tasks
        cfg = research.make_config("standard", None)  # codex_task_cap 2
        self.assertEqual(cfg["codex_task_cap"], 2)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_primary_search("p", tasks, research.Path(tmp), cfg,
                                            extra_tasks_supplier=Supplier(), extra_tasks_sink=[])
        codex_tasks = sorted(t for leg, t in calls if leg == "codex")
        self.assertEqual(codex_tasks, ["task-1", "task-2"])  # cap spent on main; audit tasks get no codex

    # ---- Round 6: M1 empty-but-valid results are not parse failures ----
    def test_empty_success_record_no_parse_failed(self):
        rec = {"leg": "gemini", "task_id": "t", "record_id": "r1", "success": True,
               "stdout": "", "stdout_file": "f"}
        findings, parse_rej, parsed = research.parse_model_records([rec])
        self.assertEqual(findings, [])
        self.assertEqual(parse_rej, [])            # no rejected placeholder for an empty-but-successful call
        self.assertFalse(parsed[0]["parse_failed"])  # still a completed call in the per-model stats
        self.assertTrue(parsed[0]["no_sources"])

    def test_real_parse_error_still_placeholder(self):
        rec = {"leg": "gemini", "task_id": "t", "record_id": "r2", "success": True,
               "stdout": "not json {", "stdout_file": "f"}
        findings, parse_rej, parsed = research.parse_model_records([rec])
        self.assertEqual(findings, [])
        self.assertEqual(len(parse_rej), 1)
        self.assertEqual(parse_rej[0]["reasons"], ["parse_failed"])
        self.assertTrue(parsed[0]["parse_failed"])


if __name__ == "__main__":
    unittest.main()
