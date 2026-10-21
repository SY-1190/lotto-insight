"""Integrity and atomic-preservation tests; no network is used."""

from copy import deepcopy
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_lotto.py"
SPEC = importlib.util.spec_from_file_location("update_lotto", SCRIPT)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)
NOW = datetime(2002, 12, 14, 22, 0, tzinfo=updater.KST)
DRAWS = [
    {"round": 1, "date": "2002-12-07", "numbers": [10, 23, 29, 33, 37, 40], "bonus": 16},
    {"round": 2, "date": "2002-12-14", "numbers": [9, 13, 21, 25, 32, 42], "bonus": 2},
]


def official_payload(draws):
    rows = []
    for draw in draws:
        row = {"ltEpsd": draw["round"], "ltRflYmd": draw["date"].replace("-", ""), "bnsWnNo": draw["bonus"]}
        row.update({f"tm{i}WnNo": value for i, value in enumerate(draw["numbers"], 1)})
        rows.append(row)
    return {"data": {"list": rows}}


def mirror_payload(draws):
    return [{"draw_no": d["round"], "date": d["date"] + "T00:00:00Z", "numbers": d["numbers"], "bonus_no": d["bonus"]} for d in draws]


def html_with(draws):
    return '<!doctype html>\r\n<style>body{color:red}</style>\r\n<script type="application/json" id="lotto-data">' + json.dumps({"draws": draws, "meta": {"coverageVerified": True}}) + '</script>\r\n<script>const text = "keep me";</script>'


class IntegrityTests(unittest.TestCase):
    def test_invalid_rounds_numbers_dates_and_future_are_rejected(self):
        cases = []
        for key, value in (("numbers", [9, 9, 21, 25, 32, 42]), ("bonus", 9), ("date", "2002-12-15"), ("round", 3), ("bonus", True)):
            bad = deepcopy(DRAWS)
            bad[1][key] = value
            cases.append(bad)
        cases.extend(([DRAWS[1]], [DRAWS[0], DRAWS[0]], DRAWS[:1]))
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(updater.DataError):
                updater.validate_draws(bad, NOW, require_latest=True)

    def test_korean_saturday_boundary(self):
        before = datetime(2026, 10, 24, 20, 44, 59, tzinfo=updater.KST)
        after = datetime(2026, 10, 24, 20, 45, 0, tzinfo=updater.KST)
        self.assertEqual(updater.expected_round(before), 1246)
        self.assertEqual(updater.expected_round(after), 1247)

    def test_failures_and_historical_conflict_preserve_original_bytes(self):
        conflicting = deepcopy(DRAWS)
        conflicting[0]["bonus"] = 17
        bad_mirror = deepcopy(DRAWS)
        bad_mirror[1]["bonus"] = 3

        def unreachable(url):
            raise updater.DataError("offline")

        def contradictory_mirrors(url):
            if url.startswith(updater.OFFICIAL_API):
                raise updater.DataError("offline")
            return mirror_payload(DRAWS if url == updater.MIRRORS[0] else bad_mirror)

        for fetch in (unreachable, lambda _: official_payload(conflicting), contradictory_mirrors):
            with self.subTest(fetch=fetch), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "index.html"
                initial = html_with(DRAWS[:1]).encode()
                path.write_bytes(initial)
                with self.assertRaises(updater.DataError):
                    updater.update_file(path, NOW, fetch)
                self.assertEqual(path.read_bytes(), initial)

    def test_atomic_update_changes_only_json_and_check_only_is_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.html"
            initial = html_with(DRAWS[:1])
            path.write_bytes(initial.encode())
            before, span = updater.read_embedded(initial)
            result = updater.update_file(path, NOW, lambda _: official_payload(list(reversed(DRAWS))))
            self.assertTrue(result["changed"])
            changed = path.read_bytes().decode()
            data, new_span = updater.read_embedded(changed)
            self.assertEqual(initial[:span[0]], changed[:new_span[0]])
            self.assertEqual(initial[span[1]:], changed[new_span[1]:])
            self.assertEqual(data["draws"], DRAWS)
            self.assertTrue(data["meta"]["coverageVerified"])
            def forbidden(_):
                self.fail("check-only must never request the network")
            self.assertEqual(updater.update_file(path, NOW, forbidden, check_only=True)["latest"], 2)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_two_mirrors_are_required_and_script_breakout_is_escaped(self):
        seen = []
        def fetch(url):
            seen.append(url)
            if url.startswith(updater.OFFICIAL_API):
                raise updater.DataError("offline")
            return mirror_payload(DRAWS)
        draws, provenance = updater.collect_draws(DRAWS[:1], NOW, fetch)
        self.assertEqual(draws, DRAWS)
        self.assertEqual(seen[1:], list(updater.MIRRORS))
        self.assertEqual(provenance["verification"], "two-mirror-agreement")
        html = html_with(DRAWS)
        payload, span = updater.read_embedded(html)
        payload["meta"]["notes"] = '</script><img src=x onerror="alert(1)">'
        rendered = updater.render_payload(html, span, payload)
        self.assertNotIn('<img src=x', rendered)
        self.assertEqual(updater.read_embedded(rendered)[0], payload)


if __name__ == "__main__":
    unittest.main()
