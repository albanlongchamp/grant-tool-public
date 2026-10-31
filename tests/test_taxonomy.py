import unittest

from grantlib.taxonomy import annotate, classify_document, expand_query, parse_references


class IdentityTests(unittest.TestCase):
    def test_collaborator_inside_personal_revision_stays_collaborator(self):
        item = classify_document("resubmission/collaborator.Research Strategy.docx")
        self.assertEqual(item["role"], "collaborator_or_template")
        self.assertEqual(item["cycle"], "resubmission")

    def test_known_collaborator_folder_is_never_own_work(self):
        for path in ("collaborators/Specific Aims.doc", "templates/own_grant.docx", "sample_grants/full proposal.pdf", "collaborator/Facilities.docx"):
            with self.subTest(path=path):
                self.assertEqual(classify_document(path)["role"], "collaborator_or_template")

    def test_unreviewed_archive_ownership_requires_review(self):
        item = classify_document("unreviewed_archive/ResearchPlan.docx")
        self.assertEqual(item["role"], "ownership_uncertain")
        self.assertIn("ownership_uncertain", item["review_flags"])

    def test_root_cycle_not_guessed_from_parent_name(self):
        item = classify_document("own_grant_project/PROJECT SUMMARY.docx")
        self.assertEqual(item["cycle"], "unspecified")

    def test_reference_and_review_identity(self):
        self.assertEqual(classify_document("literature/paper.pdf")["role"], "literature")
        self.assertEqual(classify_document("resubmission/Reviewer_Comments.docx")["kind"], "reviewer_feedback")
        self.assertEqual(classify_document("resubmission/Introduction.docx")["kind"], "reviewer_response")

    def test_named_research_strategy_and_full_application(self):
        self.assertEqual(classify_document("resubmission/Grant_ResearchS_Draft.docx")["kind"], "research_strategy")
        self.assertEqual(classify_document("previous_submission/application_drafts/ASSIST Final Preview.pdf")["kind"], "full_application")
        self.assertEqual(classify_document("templates/full proposal.pdf")["kind"], "full_application")

    def test_submission_cycles_use_explicit_generic_folder_labels(self):
        for folder in ("resubmission", "revision"):
            with self.subTest(folder=folder):
                self.assertEqual(classify_document(f"{folder}/strategy.docx")["cycle"], "resubmission")
        for folder in ("historical", "previous_submission", "unreviewed_archive"):
            with self.subTest(folder=folder):
                self.assertEqual(classify_document(f"{folder}/strategy.docx")["cycle"], "historical")

    def test_collaborator_filename_prefix_is_explicit(self):
        self.assertEqual(classify_document("own_grant/collaborator_strategy.docx")["role"], "collaborator_or_template")
        self.assertEqual(classify_document("own_grant/collaboratorless_strategy.docx")["role"], "own_grant")


class AnnotationTests(unittest.TestCase):
    def test_proposal_never_becomes_demonstrated(self):
        item = annotate("We will measure nitrate in mouse tissue using LC-MS metabolomics.", "Approach",
                        topic_rules={"nitrogen": r"\bnitrate\b"})
        self.assertEqual(item["evidence_status"], "proposed")
        self.assertIn("methods", item["categories"])
        self.assertEqual(["nitrogen"], item["topics"])
        self.assertIn("metabolomics", item["techniques"])
        self.assertIn("mouse", item["models"])

    def test_mixed_passage_requires_review(self):
        item = annotate("We have shown protection in mice. We will test the intervention in human participants.")
        self.assertEqual(item["evidence_status"], "mixed_or_unclear")
        self.assertIn("mixed_proposal_and_reported_evidence", item["review_flags"])

    def test_past_result_is_unverified(self):
        item = annotate("Our preliminary data suggest that the treatment improves recovery.")
        self.assertEqual(item["evidence_status"], "reported_result_unverified")
        self.assertIn("reported_claim_requires_source_verification", item["review_flags"])

    def test_expectation_is_proposal(self):
        self.assertEqual(annotate("We expect a reduction in injury.", "Anticipated Results")["evidence_status"], "proposed")
        self.assertEqual(annotate("Our central hypothesis is that the compound improves recovery.")["evidence_status"], "proposed")

    def test_negated_evidence_not_reported_as_positive(self):
        item = annotate("We have not demonstrated protection in human participants.")
        self.assertEqual(item["evidence_status"], "mixed_or_unclear")
        self.assertIn("negated_evidence_language", item["review_flags"])

    def test_bibliography_is_not_verified_evidence(self):
        item = annotate("1. Example A. A published study. 2024. doi:10.1234/Example.1", "References")
        self.assertEqual(item["evidence_status"], "not_applicable")
        self.assertIn("bibliography_is_not_verified_claim_support", item["review_flags"])

    def test_literature_not_automatically_verified(self):
        item = annotate("We demonstrated a measurable effect.", document={"role": "literature"})
        self.assertEqual(item["evidence_status"], "reported_result_unverified")
        self.assertIn("literature_support_not_verified", item["review_flags"])

    def test_collaborator_cannot_be_attributed_to_user(self):
        item = annotate("We showed improved recovery.", document={"role": "collaborator_or_template"})
        self.assertIn("do_not_attribute_collaborator_work_to_user", item["review_flags"])

    def test_nonhuman_does_not_imply_human_model(self):
        item = annotate("We will study nonhuman cell lines.")
        self.assertIn("cell_culture", item["models"])
        self.assertNotIn("human", item["models"])

    def test_no_research_topics_are_built_in(self):
        self.assertEqual(annotate("We will study soil nitrate in dormant seeds.")["topics"], [])

    def test_single_cell_is_not_automatically_bulk(self):
        item = annotate("Single-cell RNA sequencing will be performed.")
        self.assertIn("single_cell_rna_seq", item["techniques"])
        self.assertNotIn("bulk_rna_seq", item["techniques"])

    def test_unicode_names_match_configured_topics_without_mutating_source(self):
        source = "We will test CO₂ uptake after drought–rewatering stress."
        item = annotate(source, topic_rules={"carbon_dioxide": r"\bco2\b", "drought": r"drought-rewatering"})
        self.assertEqual(["carbon_dioxide", "drought"], item["topics"])
        self.assertIn("CO₂", source)

    def test_review_text_not_primary_evidence(self):
        item = annotate("We demonstrated improvement.", document={"kind": "reviewer_response"})
        self.assertEqual(item["evidence_status"], "not_applicable")


class QueryAndReferenceTests(unittest.TestCase):
    def test_query_expansion_uses_word_boundaries(self):
        groups = (("co2", "carbon dioxide"), ("leaf", "foliar"))
        self.assertIn("carbon dioxide", expand_query("CO₂ methods", groups))
        self.assertNotIn("foliar", expand_query("leafhopper", groups))
        self.assertIn("randomized controlled trial", expand_query("RCT"))
        self.assertNotIn("randomized controlled trial", expand_query("RCTs"))

    def test_empty_query(self):
        self.assertEqual(expand_query("  "), [])

    def test_reference_extraction_preserves_case_and_balanced_parentheses(self):
        item = parse_references("DOI:10.1234/AbCd.2. (https://doi.org/10.5555/Test(2024)). PMID: 31216949; refs 12, 13.")
        self.assertEqual(item["dois"], ["10.1234/AbCd.2", "10.5555/Test(2024)"])
        self.assertEqual(item["pmids"], ["31216949"])

    def test_bare_numbers_do_not_become_pmids(self):
        self.assertEqual(parse_references("We enrolled 123456 participants.")["pmids"], [])


if __name__ == "__main__":
    unittest.main()
