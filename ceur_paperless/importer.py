"""
Created on 2026-10-14

@author: wf
"""

import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests


class VolumeRange:
    """
    volume numbers given as single numbers and ranges, e.g. 3850-3899 3901
    """

    @staticmethod
    def numbers(specs: List[str]) -> List[int]:
        """
        expand the given specs to the sorted list of volume numbers
        """
        numbers = set()
        for spec in specs:
            for part in spec.split(","):
                part = part.strip()
                match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
                if not match:
                    raise ValueError(f"invalid volume spec {part}")
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else start
                if end < start:
                    raise ValueError(f"invalid volume range {part}")
                numbers.update(range(start, end + 1))
        return sorted(numbers)


@dataclass
class Paper:
    """
    a paper of a CEUR-WS volume with the values of its document
    """

    volume: int
    name: str
    title: Optional[str] = None
    created: Optional[str] = None
    authors: Optional[str] = None
    pages: Optional[str] = None
    pdf_url: Optional[str] = None

    @property
    def ceur_id(self) -> str:
        return f"Vol-{self.volume}/{self.name}"

    @property
    def paper_id(self) -> str:
        """
        the id of the paper in its volume: the stem of the PDF name as in the ceur-spt mapping of semantify
        """
        return self.name

    @property
    def landing_page(self) -> str:
        return f"https://ceur-ws.org/Vol-{self.volume}/{self.paper_id}"

    @property
    def filename(self) -> str:
        return f"{self.name}.pdf"

    def field_values(self) -> Dict[str, object]:
        """
        the custom field values, None values left out
        """
        values = {
            "ceur_id": self.ceur_id,
            "ceur_volume": self.volume,
            "authors": self.authors,
            "pages": self.pages,
            "pdf_url": self.pdf_url,
            "paper_id": self.paper_id,
            "landing_page": self.landing_page,
        }
        return {key: value for key, value in values.items() if value is not None}

    @classmethod
    def of_spt(cls, volume: int, name: str, meta: dict) -> "Paper":
        """
        create a paper from the ceur-spt JSON of the paper
        """
        paper = cls(
            volume=volume,
            name=name,
            title=meta.get("spt.title") or name,
            created=(meta.get("spt.volume") or {}).get("date"),
            authors=meta.get("cvb.authors"),
            pages=meta.get("cvb.pages"),
            pdf_url=meta.get("spt.pdfUrl"),
        )
        return paper


@dataclass
class ImportReport:
    """
    the result of an import run
    """

    documents: int = 0
    uploaded: int = 0
    problems: List[str] = field(default_factory=list)


class Importer:
    """
    import CEUR-WS volumes from a local mirror into Paperless-ngx
    """

    FIELDS = {
        "ceur_id": "string",
        "ceur_volume": "integer",
        # a string field holds at most 128 characters
        "authors": "longtext",
        "pages": "string",
        "pdf_url": "url",
        # DOI relevant fields, see semantify issue 14
        "paper_id": "string",
        "landing_page": "url",
    }

    def __init__(
        self,
        paperless_url: str,
        token: str,
        mirror: str,
        spt_url: str = "https://ceurspt.wikidata.dbis.rwth-aachen.de",
        viewer: Optional[str] = None,
        timeout: int = 120,
    ):
        """
        constructor

        Args:
            paperless_url: base url of the Paperless-ngx instance
            token: API token
            mirror: local directory of the CEUR-WS mirror holding Vol-N directories
            spt_url: base url of ceur-spt
            viewer: user name of a read only viewer that gets view permissions
            timeout: seconds to wait for the consumption of an upload
        """
        self.api = f"{paperless_url.rstrip('/')}/api"
        self.mirror = mirror
        self.spt_url = spt_url.rstrip("/")
        self.viewer = viewer
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"
        self.field_ids: Dict[str, int] = {}
        self.viewer_id: Optional[int] = None

    @staticmethod
    def paper_names(volume_dir: str) -> List[str]:
        """
        the names of the paper PDFs of a volume directory, sorted naturally
        """
        names = [f[:-4] for f in os.listdir(volume_dir) if f.lower().endswith(".pdf")]

        def natural(name: str):
            return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]

        return sorted(names, key=natural)

    def get_json(self, url: str) -> dict:
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def prepare(self) -> None:
        """
        make sure the custom fields exist and look up the viewer
        """
        existing = {f["name"]: f["id"] for f in self.get_json(f"{self.api}/custom_fields/?page_size=100")["results"]}
        for name, data_type in self.FIELDS.items():
            if name not in existing:
                response = self.session.post(f"{self.api}/custom_fields/", json={"name": name, "data_type": data_type})
                response.raise_for_status()
                existing[name] = response.json()["id"]
        self.field_ids = existing
        if self.viewer:
            users = self.get_json(f"{self.api}/users/?username__iexact={self.viewer}")["results"]
            if not users:
                raise ValueError(f"viewer {self.viewer} not found")
            self.viewer_id = users[0]["id"]

    def view_permissions(self) -> dict:
        """
        the set_permissions value granting view to the viewer
        """
        return {"view": {"users": [self.viewer_id], "groups": []}, "change": {"users": [], "groups": []}}

    def tag_id(self, name: str) -> int:
        """
        the id of the tag with the given name, created if missing, visible to the viewer
        """
        tags = self.get_json(f"{self.api}/tags/?name__iexact={name}")["results"]
        if tags:
            tag_id = tags[0]["id"]
        else:
            response = self.session.post(f"{self.api}/tags/", json={"name": name})
            response.raise_for_status()
            tag_id = response.json()["id"]
        if self.viewer_id is not None:
            response = self.session.patch(
                f"{self.api}/tags/{tag_id}/", json={"set_permissions": self.view_permissions()}
            )
            response.raise_for_status()
        return tag_id

    def find_document(self, tag_id: int, filename: str) -> Optional[int]:
        """
        the id of the document with the given tag and original file name or None
        """
        docs = self.get_json(f"{self.api}/documents/?tags__id__all={tag_id}&page_size=1000&fields=id,original_file_name")
        for doc in docs["results"]:
            if doc.get("original_file_name") == filename:
                return doc["id"]
        return None

    def import_paper(self, paper: Paper, tag_id: int, report: ImportReport) -> Optional[int]:
        """
        upload the paper if it is not there yet and set its fields
        """
        doc_id = self.find_document(tag_id, paper.filename)
        if doc_id is None:
            path = os.path.join(self.mirror, f"Vol-{paper.volume}", paper.filename)
            data = {"title": paper.title, "tags": tag_id}
            if paper.created:
                data["created"] = paper.created
            with open(path, "rb") as pdf:
                response = self.session.post(
                    f"{self.api}/documents/post_document/", files={"document": (paper.filename, pdf)}, data=data
                )
            response.raise_for_status()
            report.uploaded += 1
            for _ in range(self.timeout):
                doc_id = self.find_document(tag_id, paper.filename)
                if doc_id is not None:
                    break
                time.sleep(1)
        if doc_id is None:
            report.problems.append(f"{paper.ceur_id} not consumed within {self.timeout} s")
            return None
        custom = [{"field": self.field_ids[key], "value": value} for key, value in paper.field_values().items()]
        response = self.session.patch(f"{self.api}/documents/{doc_id}/", json={"custom_fields": custom})
        if not response.ok:
            report.problems.append(f"{paper.ceur_id} fields {response.status_code} {response.text[:200]}")
        report.documents += 1
        return doc_id

    def import_volume(self, volume: int, report: ImportReport) -> None:
        """
        import all paper PDFs of the given volume
        """
        volume_dir = os.path.join(self.mirror, f"Vol-{volume}")
        if not os.path.isdir(volume_dir):
            report.problems.append(f"Vol-{volume} not in the mirror")
            return
        tag_id = self.tag_id(f"Vol-{volume}")
        doc_ids = []
        for name in self.paper_names(volume_dir):
            response = self.session.get(f"{self.spt_url}/Vol-{volume}/{name}.json")
            meta = response.json() if response.ok else {}
            if not response.ok:
                report.problems.append(f"Vol-{volume}/{name} ceur-spt {response.status_code}")
            doc_id = self.import_paper(Paper.of_spt(volume, name, meta), tag_id, report)
            if doc_id is not None:
                doc_ids.append(doc_id)
        if self.viewer_id is not None and doc_ids:
            # bulk_edit updates the search index of the documents with the new viewer
            response = self.session.post(
                f"{self.api}/documents/bulk_edit/",
                json={
                    "documents": doc_ids,
                    "method": "set_permissions",
                    "parameters": {"set_permissions": self.view_permissions(), "merge": True},
                },
            )
            if not response.ok:
                report.problems.append(f"Vol-{volume} permissions {response.status_code} {response.text[:200]}")

    def import_volumes(self, volumes: List[int], progress=None) -> ImportReport:
        """
        import the given volumes
        """
        self.prepare()
        report = ImportReport()
        for volume in volumes:
            self.import_volume(volume, report)
            if progress:
                progress(volume, report)
        return report
