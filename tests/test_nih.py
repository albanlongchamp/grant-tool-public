import csv
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from grantlib import nih


def record(app_id=1234567, year=2025, abstract="We will study seed storage."):
    return {
        "appl_id":app_id,"fiscal_year":year,"core_project_num":"R01GM123456",
        "project_num":"5R01GM123456-02","project_title":"Seed storage mechanisms",
        "abstract_text":abstract,"phr_text":"Improving crop availability.","activity_code":"R01",
        "agency_ic_admin":{"code":"GM","abbreviation":"NIGMS"},
        "organization":{"org_name":"Example University"},
        "principal_investigators":[{"full_name":"EXAMPLE, A"}],
        "award_amount":500000,"project_detail_url":"https://reporter.nih.gov/project-details/1234567",
    }


class NIHTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.db=self.root/"nih.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def csv_file(self,name,rows):
        path=self.root/name
        with path.open("w",newline="",encoding="utf-8-sig") as stream:
            writer=csv.DictWriter(stream,fieldnames=list(rows[0]))
            writer.writeheader();writer.writerows(rows)
        return path

    def test_personal_database_refused(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TABLE documents(id TEXT)")
        with self.assertRaisesRegex(ValueError,"separate database"):
            nih.initialize(self.db)

    def test_application_identity_and_annual_project_grouping(self):
        conn=nih.initialize(self.db)
        for item in [record(),record(),record(1234568,2026)]:
            nih.import_api_record(conn,item)
        conn.commit();conn.close()
        summary=nih.stats(self.db)
        self.assertEqual(summary["record_count"],2)
        self.assertEqual(summary["core_project_count"],1)
        self.assertEqual(summary["fiscal_years"],{"2025":1,"2026":1})
        self.assertEqual(summary["activities"],{"R01":2})
        self.assertEqual(summary["evidence_label"],"funded_project_description")

    def test_changed_records_keep_raw_versions(self):
        conn=nih.initialize(self.db)
        item=record()
        nih.import_api_record(conn,item)
        item["project_title"]="Updated title"
        nih.import_api_record(conn,item)
        self.assertEqual(conn.execute("SELECT count(*) FROM nih_record_versions").fetchone()[0],2)
        self.assertEqual(conn.execute("SELECT count(*) FROM nih_records").fetchone()[0],1)
        conn.close()

    def test_missing_abstract_reported_not_filled_with_title(self):
        conn=nih.initialize(self.db)
        nih.import_api_record(conn,record(abstract=None))
        conn.commit();conn.close()
        self.assertEqual(nih.stats(self.db)["records_missing_abstract"],1)
        self.assertEqual(nih.search(self.db,"seed")["results"][0]["abstract"],"")

    def test_local_search_has_provenance_and_filters(self):
        conn=nih.initialize(self.db)
        nih.import_api_record(conn,record())
        nih.import_api_record(conn,record(1234568,2026))
        conn.commit();conn.close()
        found=nih.search(self.db,"seed storage",fiscal_year=2026,activity="R01")
        self.assertEqual(found["total"],1)
        self.assertEqual(found["results"][0]["application_id"],"1234568")
        self.assertTrue(found["results"][0]["source_id"])
        self.assertEqual(nih.search(self.db,'" OR 1=1 --')["total"],0)

    def test_bounded_api_cap_and_resume(self):
        calls=[]
        def request(payload):
            calls.append(payload)
            offset,limit=payload["offset"],payload["limit"]
            return {"meta":{"total":7,"search_id":"public-query-id"},"results":[record(2000000+i) for i in range(offset,min(7,offset+limit))]}
        first=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic="seedling",max_records=3,page_size=2,request_json=request)
        self.assertEqual(first["status"],"capped")
        self.assertEqual(first["next_offset"],3)
        self.assertEqual([p["limit"] for p in calls],[2,1])
        self.assertNotIn("criteria",calls[1])
        self.assertIn("requested_cap_reached",first["warnings"])
        self.assertEqual(nih.stats(self.db)["record_count"],3)
        calls.clear()
        second=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic="seedling",max_records=7,page_size=2,request_json=request)
        self.assertEqual(second["status"],"complete")
        self.assertEqual(second["next_offset"],7)
        self.assertEqual(calls[0]["offset"],3)
        self.assertEqual(nih.stats(self.db)["record_count"],7)
        self.assertEqual(second["coverage"],"selected_query_only")

    def test_collection_membership_filters_overlapping_public_queries(self):
        def fetch(topic, ids, cap=None):
            return nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic=topic,
                max_records=cap or len(ids),request_json=lambda payload:{
                    "meta":{"total":len(ids)},
                    "results":[record(app) for app in ids[payload['offset']:payload['offset']+payload['limit']]]})
        irrigation=fetch('seedling irrigation',[2000001,2000002])
        dormancy=fetch('seedling dormancy',[2000002,2000003,2000004],2)
        collections=nih.stats(self.db)['collections']
        self.assertEqual(len(collections),2)
        scoped={item['query']:item for item in collections}
        self.assertEqual(scoped['seedling irrigation']['status'],'complete')
        self.assertEqual(scoped['seedling dormancy']['status'],'capped')
        self.assertEqual(scoped['seedling dormancy']['total_count'],3)
        self.assertEqual(scoped['seedling dormancy']['unique_received_count'],2)
        result=nih.search(self.db,'',collection=irrigation['run_id'])
        self.assertEqual({r['application_id'] for r in result['results']},{'2000001','2000002'})
        self.assertEqual(result['collection']['query'],'seedling irrigation')
        self.assertEqual(nih.search(self.db,'',run_id=dormancy['run_id'])['total'],2)
        self.assertEqual(nih.search(self.db,'',collection=dormancy['run_id'],fiscal_year=2026)['total'],0)
        with self.assertRaisesRegex(ValueError,'Unknown public collection'):
            nih.search(self.db,'',collection="x' OR 1=1 --")

    def test_collection_catalog_is_not_limited_to_twenty_runs(self):
        for i in range(21):
            nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic=f'topic {i}',max_records=1,
                request_json=lambda payload:{'meta':{'total':0},'results':[]})
        summary=nih.stats(self.db)
        self.assertEqual(len(summary['api_runs']),20)
        self.assertEqual(len(summary['collections']),21)

    def test_search_fields_are_validated_and_define_query_identity(self):
        requests=[]
        def request(payload):
            requests.append(payload)
            return {'meta':{'total':1},'results':[record()]}
        default=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic='irrigation',max_records=1,request_json=request)
        specific=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic='irrigation',max_records=1,
            search_fields='projecttitle,abstracttext',request_json=request)
        self.assertNotEqual(default['query_key'],specific['query_key'])
        self.assertEqual(requests[-1]['criteria']['advanced_text_search']['search_field'],'projecttitle,abstracttext')
        self.assertEqual(len(nih.stats(self.db)['collections']),2)
        with self.assertRaisesRegex(ValueError,'documented'):
            nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic='irrigation',max_records=1,
                search_fields='private_notes',request_json=request)

    def test_api_window_warning_and_explicit_cap(self):
        def request(payload):
            return {"meta":{"total":20001},"results":[record()]}
        result=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic="seedling",max_records=1,request_json=request)
        self.assertIn("query_exceeds_15000_record_window_use_partitions_or_exporter",result["warnings"])
        self.assertEqual(result["status"],"capped")
        with self.assertRaises(ValueError):
            nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic="seedling",max_records=15001,request_json=request)

    def test_empty_page_does_not_claim_complete(self):
        result=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic="seedling",max_records=4,
                              request_json=lambda p:{"meta":{"total":20},"results":[]})
        self.assertEqual(result["status"],"incomplete")
        self.assertIn("empty_page_before_confirmed_end",result["warnings"])

    def test_failure_keeps_completed_page_checkpoint(self):
        def request(payload):
            if payload["offset"]:
                raise OSError("test connection loss")
            return {"meta":{"total":3},"results":[record()]}
        result=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic="seedling",max_records=3,page_size=1,request_json=request)
        self.assertEqual(result["status"],"failed")
        self.assertEqual(result["next_offset"],1)
        self.assertEqual(nih.stats(self.db)["record_count"],1)

    def test_repeated_api_pages_do_not_claim_complete(self):
        result=nih.fetch_pilot(self.db,fiscal_years=[2025],public_topic="seedling",max_records=2,page_size=1,
                              request_json=lambda p:{"meta":{"total":2},"results":[record()]})
        self.assertEqual(result["status"],"incomplete")
        self.assertEqual(result["unique_received_count"],1)
        self.assertIn("duplicate_application_ids_in_api_query",result["warnings"])

    def test_project_abstract_bulk_join_and_idempotence(self):
        projects=self.csv_file("projects.csv",[
            {"APPLICATION_ID":"111","FY":"2025","PROJECT_TITLE":"Seed storage","CORE_PROJECT_NUM":"R01GM1","FULL_PROJECT_NUM":"1R01GM1-01","ACTIVITY":"R01"},
            {"APPLICATION_ID":"112","FY":"2026","PROJECT_TITLE":"Seedling continuation","CORE_PROJECT_NUM":"R01GM1","FULL_PROJECT_NUM":"5R01GM1-02","ACTIVITY":"R01"},
        ])
        abstracts=self.csv_file("abstracts.csv",[
            {"APPLICATION_ID":"111","ABSTRACT_TEXT":"First line, with comma.\nSecond line."},
            {"APPLICATION_ID":"999","ABSTRACT_TEXT":"Unmatched project."},
        ])
        first=nih.import_exporter(self.db,project_files=[projects],abstract_files=[abstracts])
        self.assertEqual(first["stats"]["record_count"],2)
        self.assertEqual(first["stats"]["core_project_count"],1)
        self.assertEqual(first["stats"]["records_missing_abstract"],1)
        self.assertEqual(first["stats"]["orphan_abstracts"],1)
        found=nih.search(self.db,"",fiscal_year=2025)["results"][0]
        self.assertEqual(found["abstract"],"First line, with comma.\nSecond line.")
        self.assertTrue(found["abstract_source_id"].startswith("exporter:abstract:"))
        self.assertEqual(found["reporter_url"],"")  # No fabricated source URL.
        repeat=nih.import_exporter(self.db,project_files=[projects],abstract_files=[abstracts])
        self.assertTrue(all(f["status"]=="already_imported" for f in repeat["files"]))
        self.assertEqual(repeat["stats"]["record_count"],2)

    def test_abstracts_imported_after_projects_join(self):
        projects=self.csv_file("projects.csv",[{"APPLICATION_ID":"111","FY":"2025","PROJECT_TITLE":"Seedling"}])
        abstracts=self.csv_file("abstracts.csv",[{"APPLICATION_ID":"111","ABSTRACT_TEXT":"Irrigation study."}])
        nih.import_exporter(self.db,project_files=[projects])
        self.assertEqual(nih.stats(self.db)["records_missing_abstract"],1)
        nih.import_exporter(self.db,abstract_files=[abstracts])
        self.assertEqual(nih.search(self.db,"irrigation")["total"],1)

    def test_zip_streaming_does_not_extract_paths(self):
        archive=self.root/"abstracts.zip"
        with zipfile.ZipFile(archive,"w") as z:
            z.writestr("../outside.csv","APPLICATION_ID,ABSTRACT_TEXT\n111,Public abstract\n")
        result=nih.import_exporter(self.db,abstract_files=[archive])
        self.assertEqual(result["files"][0]["status"],"complete")
        self.assertFalse((self.root.parent/"outside.csv").exists())
        self.assertEqual(result["stats"]["orphan_abstracts"],1)

    def test_bulk_header_errors_are_reported(self):
        bad=self.root/"bad.csv"
        bad.write_text("WRONG_ID,ABSTRACT_TEXT\n111,Text\n")
        result=nih.import_exporter(self.db,abstract_files=[bad])
        self.assertEqual(result["files"][0]["status"],"failed")
        self.assertIn("APPLICATION_ID",result["files"][0]["error"])

    def test_request_pacing_and_retry_after(self):
        sleeps=[]
        error=urllib.error.HTTPError(nih.API_URL,429,"Rate limited",{"Retry-After":"2"},None)
        response=io.BytesIO(json.dumps({"results":[],"meta":{"total":0}}).encode())
        with patch("grantlib.nih.urllib.request.urlopen",side_effect=[error,response]):
            result=nih._request_json({"limit":1},sleep=sleeps.append)
        self.assertEqual(result["meta"]["total"],0)
        self.assertEqual(sleeps,[1.05,2.0,1.05])


if __name__=="__main__":
    unittest.main()
