"""Offline provenance guard for the OUROBOROS proposal-only study card.

No external API, provider, or network call is required.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit


DOC = Path(__file__).resolve().parents[1] / "docs" / "LEADERS-STUDY.md"
PUBLIC_REFERENCE_HOSTS = {
    "openai.github.io",
    "langfuse.com",
}


def check_study_card(text: str) -> None:
    """Fail closed on unattributed references or unreceipted execution claims."""
    if "## Source references" not in text:
        raise ValueError("SOURCE_REFERENCES_SECTION_MISSING")
    section = text.split("## Source references", 1)[1].split("\n## ", 1)[0]
    urls = re.findall(r"https://[^\s)]+", section)
    if len(urls) < 3:
        raise ValueError("INSUFFICIENT_AUDITABLE_REFERENCE_URLS")
    if any(urlsplit(url).netloc not in PUBLIC_REFERENCE_HOSTS for url in urls):
        raise ValueError("UNQUALIFIED_REFERENCE_HOST")
    if "Program receipt status: NOT AVAILABLE" not in text:
        raise ValueError("RECEIPT_STATUS_NOT_EXPLICIT")
    if re.search(r"study receipted in OUROBOROS", text, re.IGNORECASE):
        raise ValueError("UNPROVEN_PROGRAM_RECEIPT_CLAIM")
    if "PROPOSE-ONLY" not in text:
        raise ValueError("STUDY_SCOPE_NOT_EXPLICIT")


class DomainStudyProvenanceTests(unittest.TestCase):
    def test_committed_study_has_sources_and_explicit_receipt_abstention(self):
        check_study_card(DOC.read_text(encoding="utf-8"))

    def test_synthetic_missing_references_and_false_receipt_are_denied(self):
        valid = DOC.read_text(encoding="utf-8")
        invalid = (
            valid.replace("https://", "not-a-url://"),
            valid.replace("Program receipt status: NOT AVAILABLE", "Status: complete"),
            valid + "\nStudy receipted in OUROBOROS phase-3 program logs.\n",
        )
        for specimen in invalid:
            with self.subTest(specimen_length=len(specimen)):
                with self.assertRaises(ValueError):
                    check_study_card(specimen)


if __name__ == "__main__":
    unittest.main()
