"""
Created on 2026-10-14

@author: wf
"""

import sys
from argparse import ArgumentParser, Namespace
from dataclasses import dataclass
from typing import List, Optional

from basemkit.base_cmd import BaseCmd

import ceur_paperless
from ceur_paperless.hops import EventLookup
from ceur_paperless.importer import Importer, VolumeRange


@dataclass
class Version:
    """
    Version information for ceur-paperless
    """

    name: str = "ceur-paperless"
    version: str = ceur_paperless.__version__
    date: str = "2026-10-14"
    updated: str = "2026-10-14"
    description: str = "Paperless-ngx as fifth platform of the CEUR-WS approach comparison"
    authors: str = "Wolfgang Fahl"
    doc_url: str = "https://github.com/WolfgangFahl/ceur-paperless"
    chat_url: str = "https://github.com/WolfgangFahl/ceur-paperless/discussions"
    cm_url: str = "https://github.com/WolfgangFahl/ceur-paperless"


class CeurPaperlessCmd(BaseCmd):
    """
    command line interface of ceur-paperless
    """

    def __init__(self):
        super().__init__(Version())

    def add_arguments(self, parser: ArgumentParser) -> None:
        super().add_arguments(parser)
        parser.add_argument("volumes", nargs="*", help="volume numbers and ranges, e.g. 3850-3899")
        parser.add_argument("--url", default="http://127.0.0.1:8000", help="base url of the Paperless-ngx instance")
        parser.add_argument("--mirror", help="local CEUR-WS mirror directory holding the Vol-N directories")
        parser.add_argument("--viewer", help="user name of a read only viewer that gets view permissions")
        parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for each consumption")
        parser.add_argument(
            "--hops",
            action="store_true",
            help="set Scholar, Event and EventSeries tags and the document types Paper and Front matter",
        )
        parser.add_argument(
            "--no-wikidata", action="store_true", help="with --hops take the event from the volume page only"
        )

    def handle_args(self, args: Namespace) -> bool:
        handled = super().handle_args(args)
        if not handled and args.volumes:
            if not args.mirror:
                raise ValueError("--mirror is needed for an import")
            # the token is read from stdin so that it is neither in the process list nor in a file
            token = sys.stdin.readline().strip()
            hops = EventLookup(use_wikidata=not args.no_wikidata) if args.hops else None
            importer = Importer(args.url, token, args.mirror, viewer=args.viewer, timeout=args.timeout, hops=hops)
            volumes = VolumeRange.numbers(args.volumes)

            def progress(volume, report):
                print(f"Vol-{volume}: {report.documents} documents, {report.uploaded} uploaded", flush=True)

            report = importer.import_volumes(volumes, progress=progress)
            print(f"{len(volumes)} volumes, {report.documents} documents, {report.uploaded} uploaded")
            print(f"{len(report.problems)} problems")
            if report.failed:
                print(f"{len(report.failed)} failed volumes: {' '.join(str(volume) for volume in report.failed)}")
                self.exit_code = 1
            handled = True
        return handled


def main(argv: Optional[List[str]] = None) -> int:
    cmd = CeurPaperlessCmd()
    exit_code = cmd.run(argv)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
