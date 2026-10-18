"""
Created on 2026-10-14

@author: wf
"""

import datetime
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import unquote, urlencode, urlparse

import requests
from ceursemantify.model import Paper as ModelPaper
from ceursemantify.model import Proceedings
from ceursemantify.volume_html import VolumeHtml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ceur_paperless.hops import EventLookup


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
    paper_id: str
    filename: str
    title: Optional[str] = None
    created: Optional[str] = None
    authors: Optional[str] = None
    pages: Optional[str] = None
    pdf_url: Optional[str] = None
    author_names: List[str] = field(default_factory=list)

    @property
    def document_type(self) -> str:
        """
        Paper for a table of contents entry with authors, Front matter for one without, e.g. the preface
        """
        return "Paper" if self.author_names else "Front matter"

    @property
    def ceur_id(self) -> str:
        return f"Vol-{self.volume}/{self.paper_id}"

    @property
    def upload_title(self) -> Optional[str]:
        """
        the title as sent to Paperless-ngx, which renders the title override as template:
        a title with template braces, e.g. {{citation needed}}, is sent as raw block, see issue 6
        """
        title = self.title
        if title and any(mark in title for mark in ("{{", "{%", "{#")):
            title = "{% raw %}" + title + "{% endraw %}"
        return title

    @property
    def published_elsewhere(self) -> bool:
        """
        True if the pdf url points to another volume, an entry of a paper published elsewhere,
        see issue 6 and semantify issue 35
        """
        match = re.search(r"/Vol-(\d+)/", unquote(urlparse(self.pdf_url).path)) if self.pdf_url else None
        return match is not None and int(match.group(1)) != self.volume

    @staticmethod
    def plausible_date(value: Optional[str]) -> Optional[str]:
        """
        the given ISO date if it lies between the first CEUR-WS volume and the current year, else None;
        an impossible CEURPUBDATE, e.g. 3014-11-22, fails the consumption in the search index, see issue 6
        """
        plausible = None
        if value:
            try:
                year = datetime.date.fromisoformat(value).year
                if 1995 <= year <= datetime.date.today().year:
                    plausible = value
            except ValueError:
                pass
        return plausible

    @property
    def landing_page(self) -> str:
        """
        the landing page as in register-dois.py of semantify
        """
        return f"https://ceur-ws.org/Vol-{self.volume}/{self.paper_id}/"

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
    def of_model(cls, volume: int, paper: ModelPaper, created: Optional[str] = None) -> "Paper":
        """
        create a paper from a paper of the semantify research model

        Args:
            volume: the volume number
            paper: the paper as mapped from the table of contents of the volume page
            created: the publication date of the volume
        """
        author_names = [author.full_name() for author in paper.authors]
        authors = ", ".join(author_names) or None
        pages = None
        if paper.pageStart:
            pages = f"{paper.pageStart}-{paper.pageEnd}" if paper.pageEnd else paper.pageStart
        # the mirror holds the decoded name, e.g. a space for %20, see issue 5
        path = unquote(urlparse(paper.pdfUrl).path)
        prefix = f"/Vol-{volume}/"
        filename = path[path.index(prefix) + len(prefix) :] if prefix in path else os.path.basename(path)
        model_paper = cls(
            volume=volume,
            paper_id=paper.id,
            filename=filename,
            title=paper.title or paper.id,
            created=cls.plausible_date(created),
            authors=authors,
            pages=pages,
            pdf_url=paper.pdfUrl,
            author_names=author_names,
        )
        return model_paper


@dataclass
class ImportReport:
    """
    the result of an import run
    """

    documents: int = 0
    uploaded: int = 0
    problems: List[str] = field(default_factory=list)
    failed: List[int] = field(default_factory=list)

    def problem(self, text: str) -> None:
        """
        record the given problem and show it right away
        """
        self.problems.append(text)
        print(f"problem: {text}", flush=True)

    def not_consumed(self, volume: int, ceur_id: str, timeout: int = 120) -> None:
        """
        record a paper not consumed within the timeout; the consumption may have failed,
        so its volume counts as failed and ends up in the failed list, see issue 6
        """
        self.problem(f"{ceur_id} not consumed within {timeout} s")
        if volume not in self.failed:
            self.failed.append(volume)


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
        viewer: Optional[str] = None,
        timeout: int = 120,
        hops: Optional[EventLookup] = None,
        converted: Optional[str] = None,
    ):
        """
        constructor

        Args:
            paperless_url: base url of the Paperless-ngx instance
            token: API token
            mirror: local directory of the CEUR-WS mirror holding Vol-N directories
            viewer: user name of a read only viewer that gets view permissions
            timeout: seconds to wait for the consumption of an upload
            hops: the event lookup when Scholar, Event, EventSeries tags and document types are to be set
            converted: directory with Vol-N directories of the PDFs converted from PostScript only papers
        """
        self.api = f"{paperless_url.rstrip('/')}/api"
        self.mirror = mirror
        self.converted = converted
        self.viewer = viewer
        self.timeout = timeout
        self.hops = hops
        self.document_type_ids: Dict[str, int] = {}
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Token {token}"
        # an API outage of up to about 4 minutes, e.g. a restart of the instance, does not fail a volume
        # read errors are not retried since a repeated POST could upload twice
        retry = Retry(
            total=8,
            connect=8,
            read=0,
            status=8,
            backoff_factor=2,
            backoff_max=60,
            status_forcelist=[502, 503, 504],
            allowed_methods=None,
            raise_on_status=False,
        )
        self.session.mount("http://", HTTPAdapter(max_retries=retry))
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.field_ids: Dict[str, int] = {}
        self.viewer_id: Optional[int] = None
        # a failed create is looked up and retried, see issue 4
        self.create_attempts = 5
        self.create_backoff = 2

    def get_json(self, url: str) -> dict:
        response = self.session.get(url)
        response.raise_for_status()
        return response.json()

    def find_named(self, endpoint: str, name: str) -> Optional[dict]:
        """
        the object of the given endpoint with the given name or None
        """
        results = self.get_json(f"{self.api}/{endpoint}/?{urlencode({'name__iexact': name})}")["results"]
        return results[0] if results else None

    def create_named(self, endpoint: str, data: dict) -> dict:
        """
        create the named object; after a failed POST the name is looked up since a concurrent importer
        may have created it, otherwise the POST is retried with backoff, see issue 4
        """
        for attempt in range(self.create_attempts):
            response = self.session.post(f"{self.api}/{endpoint}/", json=data)
            if response.ok:
                return response.json()
            found = self.find_named(endpoint, data["name"])
            if found:
                return found
            if attempt < self.create_attempts - 1:
                time.sleep(self.create_backoff * 2**attempt)
        response.raise_for_status()

    def prepare(self) -> None:
        """
        make sure the custom fields exist and look up the viewer
        """
        existing = {f["name"]: f["id"] for f in self.get_json(f"{self.api}/custom_fields/?page_size=100")["results"]}
        for name, data_type in self.FIELDS.items():
            if name not in existing:
                existing[name] = self.create_named("custom_fields", {"name": name, "data_type": data_type})["id"]
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

    def tag_id(self, name: str, parent: Optional[int] = None) -> int:
        """
        the id of the tag with the given name, created if missing, visible to the viewer;
        with a parent the tag is put below it, a tag found under another parent is moved
        """
        tag = self.find_named("tags", name)
        if tag:
            tag_id = tag["id"]
            if parent is not None and tag.get("parent") != parent:
                response = self.session.patch(f"{self.api}/tags/{tag_id}/", json={"parent": parent})
                response.raise_for_status()
        else:
            data = {"name": name}
            if parent is not None:
                data["parent"] = parent
            tag_id = self.create_named("tags", data)["id"]
        if self.viewer_id is not None:
            response = self.session.patch(
                f"{self.api}/tags/{tag_id}/", json={"set_permissions": self.view_permissions()}
            )
            response.raise_for_status()
        return tag_id

    def document_type_id(self, name: str) -> int:
        """
        the id of the document type with the given name, created if missing
        """
        if name not in self.document_type_ids:
            found = self.find_named("document_types", name)
            type_id = found["id"] if found else self.create_named("document_types", {"name": name})["id"]
            if self.viewer_id is not None:
                response = self.session.patch(
                    f"{self.api}/document_types/{type_id}/", json={"set_permissions": self.view_permissions()}
                )
                response.raise_for_status()
            self.document_type_ids[name] = type_id
        return self.document_type_ids[name]

    def volume_ancestors(self, volume: int, proceedings: Proceedings, report: ImportReport) -> List[int]:
        """
        the tags above the volume tag: the root EventSeries, the series and the event, the event last;
        empty if no event is known for the volume
        """
        node = self.hops.lookup(volume, proceedings)
        if node is None:
            report.problem(f"Vol-{volume} no event on Wikidata or on the volume page")
            return []
        root = self.tag_id("EventSeries")
        series = self.tag_id(node.series, root)
        event = self.tag_id(node.event, series)
        print(
            f"Vol-{volume} event {node.event} {node.event_qid or ''} series {node.series} {node.series_qid or ''} from {node.source}",
            flush=True,
        )
        return [root, series, event]

    def paper_tags(self, paper: Paper, volume_tags: List[int]) -> List[int]:
        """
        the volume tag with its ancestors, the root Scholar and one tag per author below it;
        the ancestors are sent along since Paperless-ngx takes an ancestor missing in a PATCH
        as a removed parent and drops its descendants with it
        """
        root = self.tag_id("Scholar")
        tags = volume_tags + [root] + [self.tag_id(name, root) for name in paper.author_names]
        return tags

    def find_document(self, tag_id: int, filename: str) -> Optional[int]:
        """
        the id of the document with the given tag and original file name or None;
        Paperless-ngx keeps the base name, so a paper in a subdirectory, e.g. papers/paper_1.pdf,
        is compared by its base name, see issue 6
        """
        docs = self.get_json(
            f"{self.api}/documents/?tags__id__all={tag_id}&page_size=1000&fields=id,original_file_name"
        )
        base_name = os.path.basename(filename)
        for doc in docs["results"]:
            if doc.get("original_file_name") == base_name:
                return doc["id"]
        return None

    def source_path(self, paper: Paper) -> Optional[str]:
        """
        the path of the pdf of the paper: the mirror, else the converted PDFs
        of the PostScript only papers, see semantify issue 39; None if there is none
        """
        path = None
        bases = [self.mirror] + ([self.converted] if self.converted else [])
        for base in bases:
            candidate = os.path.join(base, f"Vol-{paper.volume}", paper.filename)
            if os.path.isfile(candidate):
                path = candidate
                break
        return path

    def import_paper(
        self, paper: Paper, tag_id: int, report: ImportReport, volume_tags: Optional[List[int]] = None
    ) -> Optional[int]:
        """
        upload the paper if it is not there yet and set its fields
        """
        doc_id = self.find_document(tag_id, paper.filename)
        if doc_id is None:
            path = self.source_path(paper)
            data = {"title": paper.upload_title, "tags": tag_id}
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
            report.not_consumed(paper.volume, paper.ceur_id, self.timeout)
            return None
        custom = [{"field": self.field_ids[key], "value": value} for key, value in paper.field_values().items()]
        data = {"custom_fields": custom}
        if self.hops:
            # the serializer adds the ancestors of each tag: Scholar, the event, its series and EventSeries
            data["tags"] = self.paper_tags(paper, volume_tags or [tag_id])
            data["document_type"] = self.document_type_id(paper.document_type)
        response = self.session.patch(f"{self.api}/documents/{doc_id}/", json=data)
        if not response.ok:
            report.problem(f"{paper.ceur_id} fields {response.status_code} {response.text[:200]}")
        report.documents += 1
        return doc_id

    def import_volume(self, volume: int, report: ImportReport) -> None:
        """
        import the papers of the given volume as the semantify mapping of its volume page defines them
        """
        index_html = os.path.join(self.mirror, f"Vol-{volume}", "index.html")
        if not os.path.isfile(index_html):
            report.problem(f"Vol-{volume} not in the mirror")
            return
        result = VolumeHtml.of_file(index_html).proceedings()
        for issue in result.issues:
            report.problem(f"Vol-{volume} {issue}")
        proceedings = result.target
        if proceedings.pubDate and Paper.plausible_date(proceedings.pubDate) is None:
            report.problem(f"Vol-{volume} CEURPUBDATE {proceedings.pubDate} implausible, created left out")
        ancestors = self.volume_ancestors(volume, proceedings, report) if self.hops else []
        tag_id = self.tag_id(f"Vol-{volume}", ancestors[-1] if ancestors else None)
        volume_tags = [tag_id] + ancestors
        doc_ids = []
        for model_paper in proceedings.papers:
            paper = Paper.of_model(volume, model_paper, proceedings.pubDate)
            if paper.published_elsewhere:
                report.problem(f"{paper.ceur_id} published elsewhere {paper.pdf_url}, skipped")
                continue
            if self.source_path(paper) is None:
                report.problem(f"{paper.ceur_id} {paper.filename} not in the mirror nor converted")
                continue
            doc_id = self.import_paper(paper, tag_id, report, volume_tags)
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
                report.problem(f"Vol-{volume} permissions {response.status_code} {response.text[:200]}")

    def import_volumes(self, volumes: List[int], progress=None) -> ImportReport:
        """
        import the given volumes
        """
        self.prepare()
        report = ImportReport()
        for volume in volumes:
            # one failing volume does not end the range, see issue 3
            try:
                self.import_volume(volume, report)
            except Exception as ex:
                report.failed.append(volume)
                report.problem(f"Vol-{volume} failed: {type(ex).__name__}: {ex}")
            if progress:
                progress(volume, report)
        return report
