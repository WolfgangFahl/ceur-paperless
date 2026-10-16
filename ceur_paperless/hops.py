"""
Created on 2026-10-16

@author: wf

the nodes beyond Paper and Proceedings that the graph walks of
https://cr.bitplan.com/index.php/CEUR-WS_Graph_Walks need in the kiosk:
Scholar, Event and EventSeries as tags, Paper and Front matter as document types
"""

import re
from dataclasses import dataclass
from typing import Optional

import requests

from ceursemantify.model import Proceedings


@dataclass
class EventNode:
    """
    the event of a volume and its series as tag names
    """

    event: str
    series: str
    event_qid: Optional[str] = None
    series_qid: Optional[str] = None
    source: str = "volume page"


class EventLookup:
    """
    event and event series of a volume: Wikidata where the proceedings item has
    P4745 is proceedings of and the event P179 part of the series,
    the acronym of the volume page otherwise
    """

    ENDPOINT = "https://query.wikidata.org/sparql"
    USER_AGENT = "ceur-paperless/0.0.1 (https://github.com/WolfgangFahl/ceur-paperless)"

    def __init__(self, use_wikidata: bool = True, timeout: int = 60):
        self.use_wikidata = use_wikidata
        self.timeout = timeout

    @staticmethod
    def series_stem(acronym: str) -> str:
        """
        the series part of an event acronym, e.g. SemTab for SemTab 2024, Wikidata for Wikidata 2022
        """
        stem = re.sub(r"[\s'-]*(19|20)?\d{2}\s*$", "", acronym).strip()
        return stem or acronym

    def wikidata(self, volume: int) -> dict:
        """
        the first row of the event query for the given volume, empty if none
        """
        query = f"""SELECT ?proc ?event ?eventShort ?series ?seriesShort ?seriesLabel WHERE {{
  {{ VALUES ?url {{ <http://ceur-ws.org/Vol-{volume}/> <https://ceur-ws.org/Vol-{volume}/> }} ?proc wdt:P973 ?url }}
  UNION {{ ?proc wdt:P179 wd:Q27230297; wdt:P478 "{volume}" }}
  ?proc wdt:P4745 ?event.
  OPTIONAL {{ ?event wdt:P1813 ?eventShort }}
  OPTIONAL {{ ?event wdt:P179 ?series. OPTIONAL {{ ?series wdt:P1813 ?seriesShort }} }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}} LIMIT 1"""
        response = requests.get(
            self.ENDPOINT,
            params={"query": query},
            headers={"Accept": "application/sparql-results+json", "User-Agent": self.USER_AGENT},
            timeout=self.timeout,
        )
        response.raise_for_status()
        bindings = response.json()["results"]["bindings"]
        row = {key: value["value"] for key, value in bindings[0].items()} if bindings else {}
        return row

    def lookup(self, volume: int, proceedings: Proceedings) -> Optional[EventNode]:
        """
        the event node of the given volume or None if neither Wikidata nor the volume page names an event
        """
        acronym = proceedings.event.acronym if proceedings.event else None
        row = self.wikidata(volume) if self.use_wikidata else {}
        if not acronym and not row:
            return None
        def qid(key: str) -> Optional[str]:
            return row[key].rsplit("/", 1)[-1] if key in row else None

        # the slug of the event is the acronym of the volume page, the one a reader of CEUR-WS knows
        event = acronym or row.get("eventShort") or qid("event")
        series = row.get("seriesShort") or row.get("seriesLabel") or self.series_stem(event)
        node = EventNode(
            event=event,
            series=series,
            event_qid=qid("event"),
            series_qid=qid("series"),
            source="Wikidata" if row else "volume page",
        )
        return node
