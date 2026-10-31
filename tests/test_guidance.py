"""Offline tests for authority, provenance, and mechanism-specific guidance."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from grantlib.guidance import load_guidance, search_guidance


class GuidanceTests(unittest.TestCase):
    def test_curated_catalog_has_required_provenance(self):
        records = load_guidance()
        self.assertGreaterEqual(len(records), 6)
        self.assertEqual(len({r["id"] for r in records}), len(records))
        for record in records:
            self.assertEqual(record["checked_on"], "2026-10-30")
            self.assertLessEqual(date.fromisoformat(record["checked_on"]), date.today())
            self.assertIn(record["content_type"], {"advice", "requirement", "framework"})
            self.assertTrue(record["tips"])
            self.assertTrue(record["caveats"])

    def test_k23_includes_career_but_excludes_r01_framework(self):
        ids = {r["id"] for r in search_guidance(mechanism="K23")}
        self.assertIn("nih-k23-overview", ids)
        self.assertIn("nih-career-application-instructions", ids)
        self.assertIn("nih-general-writing", ids)
        self.assertNotIn("nih-simplified-research-review", ids)
        self.assertNotIn("nsf-proposal-preparation", ids)

    def test_r01_excludes_k_award_requirements(self):
        ids = {r["id"] for r in search_guidance(mechanism="r01")}
        self.assertIn("nih-simplified-research-review", ids)
        self.assertIn("nih-rigor-reproducibility", ids)
        self.assertNotIn("nih-career-application-instructions", ids)
        self.assertNotIn("nih-k23-overview", ids)

    def test_k08_does_not_inherit_k23_specific_overview(self):
        ids = {r["id"] for r in search_guidance(mechanism="NIH K08")}
        self.assertIn("nih-career-application-instructions", ids)
        self.assertNotIn("nih-k23-overview", ids)

    def test_nsf_is_separate_and_unknown_mechanism_is_not_universal(self):
        self.assertEqual({r["agency"] for r in search_guidance(mechanism="NSF")}, {"NSF"})
        self.assertEqual(search_guidance(mechanism="FOUNDATION_UNKNOWN"), [])
        self.assertEqual(search_guidance(mechanism="Z99"), [])

    def test_search_preserves_provenance_and_scope(self):
        records = search_guidance("mentor independence", mechanism="K23")
        self.assertTrue(records)
        self.assertIn("nih-career-application-instructions", {r['id'] for r in records})
        self.assertTrue(all(r["source_url"].startswith("https://") for r in records))
        self.assertEqual(search_guidance("zzzzunfindable"), [])

    def _catalog(self, record):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "catalog.json"
        path.write_text(json.dumps({"schema_version": 1, "sources": [record]}))
        return path

    def test_spoofed_host_and_non_https_rejected(self):
        for url in ("https://grants.nih.gov.example.com/tips", "http://grants.nih.gov/tips", "https://user@grants.nih.gov/tips"):
            record = deepcopy(load_guidance()[0])
            record["source_url"] = url
            with self.assertRaises(ValueError):
                load_guidance(self._catalog(record))

    def test_missing_or_invalid_provenance_rejected(self):
        for key, value in (("checked_on", "2026-99-99"), ("source_url", ""), ("applies_to", []), ("content_type", "verified_fact")):
            record = deepcopy(load_guidance()[0])
            record[key] = value
            with self.assertRaises(ValueError):
                load_guidance(self._catalog(record))

    def test_k_award_dms_exception_replaces_generic_plan_guidance(self):
        k_ids = {r['id'] for r in search_guidance('DMS', mechanism='K23')}
        self.assertIn('nih-k-award-dms-exemption-2026', k_ids)
        self.assertNotIn('nih-dms-writing-2026', k_ids)
        r_ids = {r['id'] for r in search_guidance('DMS', mechanism='R01')}
        self.assertIn('nih-dms-writing-2026', r_ids)
        self.assertNotIn('nih-k-award-dms-exemption-2026', r_ids)

    def test_agency_records_require_the_official_domain(self):
        record = dict(load_guidance()[0], agency='HRSA', source_url='https://data.hrsa.gov/report')
        self.assertEqual(load_guidance(self._catalog(record))[0]['agency'], 'HRSA')
        record['source_url']='https://data.hrsa.gov.example.com/report'
        with self.assertRaises(ValueError):
            load_guidance(self._catalog(record))

    def test_future_policy_dates_are_valid_and_retained(self):
        item = next(r for r in load_guidance() if r['id']=='nih-career-program-updates-2027')
        self.assertEqual(item['effective_from'], '2027-10-12')
        item['effective_from']='2027-19-50'
        with self.assertRaises(ValueError):
            load_guidance(self._catalog(item))


if __name__ == "__main__":
    unittest.main()
