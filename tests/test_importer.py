"""
Created on 2026-10-14

@author: wf
"""

import requests
from basemkit.basetest import Basetest
from ceursemantify.volume_html import VolumeHtml

from ceur_paperless.hops import EventLookup
from ceur_paperless.importer import Importer, Paper, VolumeRange


class TestImporter(Basetest):
    """
    test the parts of the importer that need no Paperless-ngx instance
    """

    def setUp(self, debug: bool = False, profile: bool = True) -> None:
        Basetest.setUp(self, debug=debug, profile=profile)

    def test_volume_range(self) -> None:
        """
        test the expansion of volume specs
        """
        self.assertEqual(50, len(VolumeRange.numbers(["3850-3899"])))
        self.assertEqual([3889, 3901, 3902], VolumeRange.numbers(["3901-3902", "3889"]))
        self.assertEqual([1, 2, 5], VolumeRange.numbers(["1-2,5"]))
        for invalid in ["x", "3899-3850"]:
            with self.subTest(spec=invalid):
                with self.assertRaises(ValueError):
                    VolumeRange.numbers([invalid])

    def test_paper_of_model(self) -> None:
        """
        test the papers of a volume page as the semantify mapping defines them:
        table of contents entries with a link to a pdf, a ToC.pdf outside the table of contents is no paper
        """
        html = """<html><body>
<span class="CEURVOLNR">Vol-3889</span><span class="CEURPUBDATE">2025-01-03</span>
<a href="ToC.pdf">table of contents</a>
<div class="CEURTOC"><ul>
<li id="preface"><a href="paper0.pdf"><span class="CEURTITLE">Preface</span></a></li>
<li id="paper1"><a href="paper1.pdf"><span class="CEURTITLE">Kepler-aSI : Semantic Annotation for Tabular Data</span></a>
<span class="CEURPAGES">12-26</span>
<span class="CEURAUTHOR">Wiem Baazouzi</span>, <span class="CEURAUTHOR">Marouen Kachroudi</span></li>
<li id="session1">Session 1</li>
</ul></div></body></html>"""
        result = VolumeHtml(html).proceedings()
        proceedings = result.target
        self.assertEqual(["preface", "paper1"], [paper.id for paper in proceedings.papers])
        preface = Paper.of_model(3889, proceedings.papers[0], proceedings.pubDate)
        self.assertEqual("paper0.pdf", preface.filename)
        self.assertEqual("Vol-3889/preface", preface.ceur_id)
        self.assertEqual("https://ceur-ws.org/Vol-3889/preface/", preface.landing_page)
        paper = Paper.of_model(3889, proceedings.papers[1], proceedings.pubDate)
        self.assertEqual("2025-01-03", paper.created)
        values = paper.field_values()
        self.assertEqual("Vol-3889/paper1", values["ceur_id"])
        self.assertEqual(3889, values["ceur_volume"])
        self.assertEqual("12-26", values["pages"])
        self.assertEqual("Wiem Baazouzi, Marouen Kachroudi", values["authors"])
        self.assertEqual("https://ceur-ws.org/Vol-3889/paper1.pdf", values["pdf_url"])
        self.assertEqual("paper1", values["paper_id"])
        self.assertEqual("https://ceur-ws.org/Vol-3889/paper1/", values["landing_page"])
        self.assertEqual("Front matter", preface.document_type)
        self.assertEqual("Paper", paper.document_type)
        self.assertEqual(["Wiem Baazouzi", "Marouen Kachroudi"], paper.author_names)

    def test_failing_volume(self) -> None:
        """
        test that a volume that raises does not end the range, see issue 3
        """

        class RaisingImporter(Importer):
            def prepare(self) -> None:
                pass

            def import_volume(self, volume, report) -> None:
                if volume == 2:
                    raise UnicodeDecodeError("utf-8", b"\xb8", 0, 1, "invalid start byte")
                report.documents += 1

        importer = RaisingImporter("http://127.0.0.1:1", "no token", "/nonexistent")
        done = []
        report = importer.import_volumes([1, 2, 3], progress=lambda volume, report: done.append(volume))
        self.assertEqual([1, 2, 3], done)
        self.assertEqual(2, report.documents)
        self.assertEqual([2], report.failed)
        self.assertIn("Vol-2 failed: UnicodeDecodeError", report.problems[0])

    def test_retry(self) -> None:
        """
        test that connection errors and gateway errors of the API are retried with backoff, see issue 3
        """
        importer = Importer("http://127.0.0.1:8000", "no token", "/nonexistent")
        retry = importer.session.get_adapter("http://127.0.0.1:8000/api/").max_retries
        self.assertEqual(8, retry.connect)
        self.assertEqual(0, retry.read)
        self.assertEqual({502, 503, 504}, set(retry.status_forcelist))

    def fake_importer(self, posts, found_after: int = None):
        """
        an importer with a fake session: the POSTs answer with the given status codes in turn,
        the name lookup finds the object once found_after POSTs have been sent
        """

        class FakeResponse:
            def __init__(self, status: int, data: dict):
                self.status_code = status
                self.ok = status < 400
                self.text = str(data)
                self._data = data

            def json(self):
                return self._data

            def raise_for_status(self):
                if not self.ok:
                    raise requests.HTTPError(f"{self.status_code} Server Error")

        class FakeSession:
            def __init__(self):
                self.headers = {}
                self.post_count = 0

            def get(self, url):
                found = found_after is not None and self.post_count >= found_after
                return FakeResponse(200, {"results": [{"id": 7, "name": "Vol-1"}] if found else []})

            def post(self, url, json=None, **kwargs):
                status = posts[min(self.post_count, len(posts) - 1)]
                self.post_count += 1
                return FakeResponse(status, {"id": 42, "name": json["name"]})

            def patch(self, url, json=None):
                return FakeResponse(200, {})

        importer = Importer("http://127.0.0.1:1", "no token", "/nonexistent")
        importer.session = FakeSession()
        importer.create_backoff = 0
        return importer

    def test_create_after_failed_post(self) -> None:
        """
        test that a failed create is followed by a lookup and retried with backoff:
        a concurrent creator, issue 4, or a transient server error do not fail the volume
        """
        # the object was created by a concurrent importer: the lookup after the failed POST finds it
        importer = self.fake_importer([400], found_after=1)
        self.assertEqual(7, importer.tag_id("Vol-1"))
        self.assertEqual(1, importer.session.post_count)
        # a transient 500: the second POST succeeds
        importer = self.fake_importer([500, 201])
        self.assertEqual(42, importer.tag_id("Vol-1"))
        self.assertEqual(42, importer.document_type_id("Paper"))
        # a persistent 500 fails after the given number of attempts
        importer = self.fake_importer([500])
        with self.assertRaises(requests.HTTPError):
            importer.tag_id("Vol-1")
        self.assertEqual(importer.create_attempts, importer.session.post_count)

    def test_series_stem(self) -> None:
        """
        test the series part of event acronyms from volume pages
        """
        for acronym, expected in [
            ("Wikidata 2022", "Wikidata"),
            ("SemTab 2024", "SemTab"),
            ("ISWC2022", "ISWC"),
            ("SWAT4HCLS 2024", "SWAT4HCLS"),
            ("KGCW'23", "KGCW"),
            ("ESWC", "ESWC"),
        ]:
            with self.subTest(acronym=acronym):
                self.assertEqual(expected, EventLookup.series_stem(acronym))
