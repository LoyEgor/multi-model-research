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
        # wrong product (exclude keyword)
        self.assertEqual(research.intent_rejection({"title": "MacBook Air for parts"}, intent), "off_intent")
        # wrong product (no subject keyword)
        self.assertEqual(research.intent_rejection({"title": "Dell XPS laptop"}, intent), "off_intent")
        # wrong tier (pro < max_5x)
        self.assertEqual(research.intent_rejection({"title": "macbook", "tier": "pro", "price_usd": 50}, intent), "wrong_tier")
        # at-or-above official price
        self.assertEqual(research.intent_rejection({"title": "macbook", "tier": "max_5x", "price_usd": 120}, intent), "not_below_official")
        # good: right subject, right tier, below official
        self.assertIsNone(research.intent_rejection({"title": "macbook", "tier": "max_20x", "price_usd": 80}, intent))
        # no intent → never rejects
        self.assertIsNone(research.intent_rejection({"title": "anything"}, None))

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
        self.assertEqual(cfg["query_variants_per_task"], 2)
        self.assertEqual(research.make_config("quick", None)["query_variants_per_task"], 1)
        self.assertEqual(research.make_config("max", None)["query_variants_per_task"], 3)
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
        cfg = research.make_config("deep", None)  # 2 query variants, search_legs codex+gemini+claude
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                research.run_primary_search("p", tasks, run_dir, cfg)
        # 1 task × 2 queries × 3 legs = 6 calls; variant id distinct
        self.assertEqual(len(calls), 6)
        self.assertEqual({tid for _, tid in calls}, {"task-1", "task-1#v2"})

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

        def fake_call(leg, prompt, run_dir, task_type, task_id, timeout=900, effort="medium", claude_model=None):
            calls.append((leg, task_id))
            return {"success": True, "stdout": '{"findings": []}', "record_id": "x", "leg": leg, "task_id": task_id}

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = research.Path(tmp)
            config = research.make_config("max", None)
            with mock.patch.object(research, "call_model", side_effect=fake_call):
                records, dropped = research.run_rechecks("p", items, run_dir, config, 1, {})
        self.assertEqual(dropped, 2)  # 14 candidates, max profile caps at 12 per round
        self.assertEqual(len(records), 24)  # recheck_legs=2 per item
        # max effort searches with 3 legs; a codex-sourced item is rescued by the 2 OTHER families.
        first_item_legs = {leg for leg, task_id in calls if task_id.startswith("recheck-1-1-")}
        self.assertEqual(first_item_legs, {"gemini", "claude"})

    def test_claude_search_leg_gated_by_effort(self):
        self.assertEqual(research.make_config("standard", None)["search_legs"], ["codex", "gemini"])
        deep = research.make_config("deep", None)
        self.assertIn("claude", deep["search_legs"])
        self.assertGreater(deep["claude_search_budget"], 0)
        self.assertEqual(research.make_config("max", None)["search_legs"], ["codex", "gemini", "claude"])

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


if __name__ == "__main__":
    unittest.main()
