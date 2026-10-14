"""
Created on 2026-10-14

@author: wf
"""

import os
import tempfile

from basemkit.basetest import Basetest

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

    def test_paper_of_spt(self) -> None:
        """
        test the mapping of the ceur-spt JSON of Vol-3889/paper1
        """
        meta = {
            "spt.title": "Kepler-aSI : Semantic Annotation for Tabular Data",
            "spt.pdfUrl": "https://ceur-ws.org/Vol-3889/paper1.pdf",
            "spt.volume": {"number": 3889, "date": "2025-01-03"},
            "cvb.authors": "Wiem Baazouzi,Marouen Kachroudi,Sami Faiz",
            "cvb.pages": "12-26",
        }
        paper = Paper.of_spt(3889, "paper1", meta)
        self.assertEqual("2025-01-03", paper.created)
        values = paper.field_values()
        self.assertEqual("Vol-3889/paper1", values["ceur_id"])
        self.assertEqual(3889, values["ceur_volume"])
        self.assertEqual("12-26", values["pages"])
        empty = Paper.of_spt(3889, "paper9", {})
        self.assertEqual("paper9", empty.title)
        self.assertEqual({"ceur_id", "ceur_volume"}, set(empty.field_values()))

    def test_paper_names(self) -> None:
        """
        test the natural order of the paper PDFs of a volume directory
        """
        with tempfile.TemporaryDirectory() as volume_dir:
            for name in ["paper10.pdf", "paper2.pdf", "Text2KG_Paper_13.pdf", "index.html", "paper1.PDF"]:
                open(os.path.join(volume_dir, name), "w").close()
            names = Importer.paper_names(volume_dir)
        self.assertEqual(["Text2KG_Paper_13", "paper1", "paper2", "paper10"], names)
