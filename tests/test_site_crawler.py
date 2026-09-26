from __future__ import annotations

import json
import unittest

from cli import site_crawler as sc
import links_store


class NormalizeUrlTests(unittest.TestCase):
    """v3.7: the crawler's own normalize_url() now strips tracking params
    and sorts the remaining query string, mirroring links_store.normalize_url
    exactly -- see the TRACKING_PARAMS comment in site_crawler.py for why
    they must stay in sync.
    """

    def test_tracking_param_sets_match_app_normalizer(self):
        self.assertEqual(sc.TRACKING_PARAMS, links_store.TRACKING_PARAMS)

    def test_tracking_params_are_stripped(self):
        self.assertEqual(
            sc.normalize_url("https://a.example/story?id=1&utm_source=newsletter"),
            "https://a.example/story?id=1",
        )

    def test_remaining_query_params_are_sorted(self):
        self.assertEqual(
            sc.normalize_url("https://a.example/story?b=2&a=1"),
            sc.normalize_url("https://a.example/story?a=1&b=2"),
        )

    def test_two_pages_differing_only_by_tracking_param_are_the_same_url(self):
        one = sc.normalize_url("https://a.example/story?id=7&fbclid=xyz")
        two = sc.normalize_url("https://a.example/story?id=7")
        self.assertEqual(one, two)

    def test_non_tracking_params_are_preserved(self):
        self.assertIn("id=7", sc.normalize_url("https://a.example/story?id=7"))

    def test_existing_path_and_host_normalization_still_works(self):
        self.assertEqual(sc.normalize_url("HTTPS://A.example/b/../c/"), "https://a.example/c/")
        self.assertEqual(sc.normalize_url("a.example/x", add_scheme=True), "https://a.example/x")


class SiteProfilesParsingTests(unittest.TestCase):
    """v3.7: --site-profiles is a JSON host->delay map; bad input must never
    abort a crawl that's otherwise ready to run."""

    def test_valid_json_parses_and_lowercases_hosts(self):
        result = sc.parse_site_profiles('{"Slow.Example.com": 3, "other.example": "1.5"}')
        self.assertEqual(result, {"slow.example.com": 3.0, "other.example": 1.5})

    def test_empty_string_returns_empty_dict(self):
        self.assertEqual(sc.parse_site_profiles(""), {})

    def test_invalid_json_is_ignored_not_fatal(self):
        self.assertEqual(sc.parse_site_profiles("{not valid json"), {})

    def test_non_dict_json_is_ignored(self):
        self.assertEqual(sc.parse_site_profiles("[1,2,3]"), {})

    def test_non_numeric_values_are_dropped(self):
        result = sc.parse_site_profiles(json.dumps({"a.example": "n/a", "b.example": 2}))
        self.assertEqual(result, {"b.example": 2.0})

    def test_negative_values_are_clamped_to_zero(self):
        result = sc.parse_site_profiles(json.dumps({"a.example": -5}))
        self.assertEqual(result, {"a.example": 0.0})


class SiteProfileDelayIsAppliedTests(unittest.TestCase):
    """v3.7: a per-host profile delay must act as a floor on top of --delay
    during an actual crawl, without needing real network access.
    """

    def test_crawl_site_waits_at_least_the_profile_delay_for_that_host(self):
        waits: list[float] = []

        class FakeStop:
            def is_set(self):
                return False

            def wait(self, seconds):
                waits.append(seconds)
                return False

        pages = {
            "https://a.example/": "<html><body><a href='/story1'>S1</a></body></html>",
            "https://a.example/story1": "<html><body>done</body></html>",
        }

        def fake_fetch_html(opener, url, timeout, max_bytes):
            html = pages.get(url)
            if html is None:
                return None
            return url, html

        original_stop = sc.STOP
        original_fetch_html = sc.fetch_html
        original_make_opener = sc.make_opener
        sc.STOP = FakeStop()
        sc.fetch_html = fake_fetch_html
        sc.make_opener = lambda user_agent: object()
        try:
            sc.crawl_site(
                "https://a.example/",
                depth=1, max_pages=10, timeout=5, delay=0.01, max_page_mb=5,
                include_subdomains=False, respect_robots=False, user_agent="test",
                use_sitemaps=False,
                site_profiles={"a.example": 2.5},
            )
        finally:
            sc.STOP = original_stop
            sc.fetch_html = original_fetch_html
            sc.make_opener = original_make_opener

        # The main page is fetched without waiting (fetched_pages starts at
        # 0); the second page must wait at least the per-host profile floor,
        # not the much smaller base --delay.
        self.assertIn(2.5, waits)
        self.assertNotIn(0.01, waits)


if __name__ == "__main__":
    unittest.main()
