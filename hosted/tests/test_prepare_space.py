import unittest
from pathlib import Path

from prepare_space import FILES


class ExportContractTests(unittest.TestCase):
    def test_public_static_links_are_exported(self):
        static = Path(__file__).resolve().parents[1] / "static"
        index = (static / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/static/privacy.html"', index)
        self.assertEqual(FILES["static/privacy.html"], "hosted/static/privacy.html")
        for target, source in FILES.items():
            self.assertTrue((static.parent.parent / source).is_file(), target)

    def test_privacy_contact_uses_approved_mailbox(self):
        privacy = Path(__file__).resolve().parents[1] / "static" / "privacy.html"
        content = privacy.read_text(encoding="utf-8")
        self.assertIn('href="mailto:stephen@szlholdings.com"', content)
