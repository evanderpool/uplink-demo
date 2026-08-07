"""Fetch public-domain industry test corpora for Uplink.

    python scripts/fetch_corpora.py                    # all industries
    python scripts/fetch_corpora.py finance tech       # a subset
    python scripts/fetch_corpora.py --contact you@example.com

Downloads a small curated set of US-government documents (public domain) into
`corpora/<industry>/`, converting HTML to plain text (Uplink indexes txt, not
html). The folder is gitignored: the capability is public, corpora are
fetched per machine.

SEC EDGAR requires a User-Agent that includes contact information
(https://www.sec.gov/os/accessing-edgar-data). Pass it with --contact or the
UPLINK_CONTACT environment variable; without it the finance corpus is
skipped, deliberately, rather than sent with a fake identity.

After fetching, index each industry as its own collection:

    python -m uplink index corpora/finance --collection finance
    python -m uplink index corpora/health  --collection health
    python -m uplink index corpora/tech    --collection tech
"""

from __future__ import annotations

import argparse
import http.client
import os
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

BASE_UA = "uplink-fetch/0.2 (+https://github.com/evanderpool/uplink)"

# Each entry: (url, output filename). PDFs are saved as-is; .htm sources are
# converted to .txt. All documents are US-government works (public domain).
CORPORA: dict[str, list[tuple[str, str]]] = {
    "finance": [
        # Annual reports (10-K) from SEC EDGAR.
        ("https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm",
         "apple-10k-fy2023.txt"),
        ("https://www.sec.gov/Archives/edgar/data/789019/000095017023035122/msft-20230630.htm",
         "microsoft-10k-fy2023.txt"),
        ("https://www.sec.gov/Archives/edgar/data/1318605/000162828024002390/tsla-20231231.htm",
         "tesla-10k-fy2023.txt"),
        # Spreadsheets: the same corpus in tabular form, which exercises a
        # different extraction path (openpyxl -> header-preserving chunks)
        # and lets a question span narrative and numbers.
        ("https://www.bls.gov/cps/cpsaat18.xlsx",
         "bls-employed-by-industry-occupation.xlsx"),
        ("https://www.bls.gov/cps/cpsaat01.xlsx",
         "bls-employment-status-civilian-population.xlsx"),
    ],
    "health": [
        # CDC infection-control guidelines.
        ("https://www.cdc.gov/infectioncontrol/pdf/guidelines/isolation-guidelines-H.pdf",
         "cdc-isolation-precautions.pdf"),
        ("https://www.cdc.gov/infectioncontrol/pdf/guidelines/disinfection-guidelines-H.pdf",
         "cdc-disinfection-sterilization.pdf"),
        ("https://www.cdc.gov/infectioncontrol/pdf/guidelines/environmental-guidelines-P.pdf",
         "cdc-environmental-infection-control.pdf"),
        ("https://www.bls.gov/iif/nonfatal-injuries-and-illnesses-tables/"
         "table-1-injury-and-illness-rates-by-industry-2022-national.xlsx",
         "bls-injury-illness-rates-by-industry-2022.xlsx"),
        ("https://data.cdc.gov/api/views/489q-934x/rows.csv?accessType=DOWNLOAD",
         "cdc-provisional-covid-deaths-by-week.csv"),
    ],
    "tech": [
        # NIST security publications.
        ("https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-53r5.pdf",
         "nist-sp-800-53r5-security-controls.pdf"),
        ("https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-61r2.pdf",
         "nist-sp-800-61r2-incident-handling.pdf"),
        ("https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-171r2.pdf",
         "nist-sp-800-171r2-protecting-cui.pdf"),
        ("https://nvlpubs.nist.gov/nistpubs/CSWP/NIST.CSWP.04162018.pdf",
         "nist-cybersecurity-framework-1-1.pdf"),
        # CISA's catalog of vulnerabilities known to be exploited in the
        # wild — the tabular counterpart to the NIST guidance above.
        ("https://www.cisa.gov/sites/default/files/csv/known_exploited_vulnerabilities.csv",
         "cisa-known-exploited-vulnerabilities.csv"),
    ],
}

# Hosts whose bot filters reject a User-Agent containing a URL and want a
# plain "Org contact@example.com" instead.
_NEEDS_CONTACT = ("sec.gov", "bls.gov")


class _HTMLToText(HTMLParser):
    """Strip tags, keep visible text with block-level line breaks."""

    _BLOCK = {"p", "div", "tr", "li", "br", "h1", "h2", "h3", "h4", "h5",
              "h6", "table", "section"}
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip() + "\n"


def html_to_text(html: str) -> str:
    p = _HTMLToText()
    p.feed(html)
    return p.text()


def fetch(url: str, ua: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch public-domain test corpora.")
    parser.add_argument("industries", nargs="*", default=[],
                        help=f"Which to fetch (default: all of {', '.join(CORPORA)}).")
    parser.add_argument("--out", default="corpora", help="Output folder.")
    parser.add_argument("--contact", default=os.environ.get("UPLINK_CONTACT", ""),
                        help="Contact email for SEC EDGAR's User-Agent policy.")
    parser.add_argument("--org", default="uplink-corpus-fetch",
                        help="Organization name for the SEC User-Agent.")
    args = parser.parse_args(argv)

    industries = args.industries or list(CORPORA)
    unknown = [i for i in industries if i not in CORPORA]
    if unknown:
        print(f"unknown industries: {', '.join(unknown)}", file=sys.stderr)
        return 2

    failures = 0
    for industry in industries:
        dest = Path(args.out) / industry
        dest.mkdir(parents=True, exist_ok=True)
        for url, filename in CORPORA[industry]:
            target = dest / filename
            if target.exists():
                print(f"have    {industry}/{filename}")
                continue
            needs_contact = any(host in url for host in _NEEDS_CONTACT)
            if needs_contact and not args.contact:
                print(f"skip    {industry}/{filename} (SEC needs --contact or UPLINK_CONTACT)")
                failures += 1
                continue
            # SEC's filter wants a plain "Org contact@example.com" UA and
            # rejects UAs containing URLs.
            ua = f"{args.org} {args.contact}".strip() if needs_contact else BASE_UA
            try:
                data = fetch(url, ua)
            except (urllib.error.URLError, OSError, TimeoutError,
                    http.client.HTTPException) as exc:
                # HTTPException covers IncompleteRead/BadStatusLine — a
                # dropped connection mid-body must cost one file, not the run.
                print(f"FAIL    {industry}/{filename}: {exc}", file=sys.stderr)
                failures += 1
                continue
            if filename.endswith(".txt"):
                text = html_to_text(data.decode("utf-8", "replace"))
                target.write_text(text, encoding="utf-8")
            else:
                target.write_bytes(data)
            print(f"fetched {industry}/{filename} ({len(data) // 1024} KB)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
