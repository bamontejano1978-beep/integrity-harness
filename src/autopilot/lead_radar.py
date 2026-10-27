"""Configurable lead radar for Integrity commercial prospecting."""

from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import yaml

from autopilot.lead_discovery import DiscoveryReport, SearchResult, discover_leads


RAW_RESULT_FIELDS = ["title", "url", "snippet", "source", "country", "published_at"]

# Remote sources are third-party input: only plain web schemes are fetched, and every
# response is size- and time-bounded so one hostile or huge source cannot exhaust a run.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})
_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_MAX_BYTES = 1_500_000

# DTD and entity declarations are the only route to entity-expansion abuse in the
# standard-library XML parser, so they are rejected before parsing.
_FORBIDDEN_XML_DECLARATION = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

_EXCLUDED_LINK_DOMAINS = {
    "facebook.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "instagram.com",
    "youtube.com",
    "discord.com",
    "drive.google.com",
    "app.cbinsights.com",
    "api-docs.cbinsights.com",
    "qualtrics.com",
    "mailto",
    "support.cbinsights.com",
    "cta-service-cms2.hubspot.com",
}

_EXCLUDED_LINK_LABELS = {
    "home",
    "about",
    "contact",
    "privacy",
    "terms",
    "login",
    "sign in",
    "sign up",
    "start free trial",
    "knowledge center",
    "developer portal",
    "docs",
    "support",
    "trust center",
    "changelog",
    "meetups",
    "sales",
    "facebook",
    "linkedin",
    "twitter",
    "x",
}


@dataclass(frozen=True)
class LeadRadarSource:
    """One configured source to crawl."""

    name: str
    url: str
    country: str = ""
    kind: str = "html"
    include_links: bool = True
    emit_page_result: bool = True
    max_link_results: int = 25
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    max_bytes: int = _DEFAULT_MAX_BYTES


@dataclass(frozen=True)
class LeadRadarConfig:
    """Lead radar configuration loaded from YAML."""

    sources: tuple[LeadRadarSource, ...]
    min_score: int = 7


@dataclass(frozen=True)
class LeadRadarReport:
    """Output of one radar run."""

    raw_results: tuple[SearchResult, ...]
    discovery: DiscoveryReport

    def to_json(self) -> str:
        payload = {
            "raw_result_count": len(self.raw_results),
            "qualified_count": len(self.discovery.leads),
            "rejected_count": len(self.discovery.rejected),
            "leads": [
                {
                    "account": lead.account,
                    "url": lead.url,
                    "segment": lead.segment,
                    "country": lead.country,
                    "score": lead.score.total,
                    "buying_signal": lead.buying_signal,
                    "next_step": lead.next_step,
                    "evidence_urls": list(lead.evidence_urls),
                }
                for lead in self.discovery.leads
            ],
            "rejected": list(self.discovery.rejected),
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)


class _RadarHTMLParser(HTMLParser):
    """Small HTML extractor for titles, meta descriptions, text, and anchors."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""
        self._tag_stack: list[str] = []
        self._capture_title = False
        self._active_href: str | None = None
        self._active_anchor: list[str] = []
        self.text_chunks: list[str] = []
        self.anchors: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {name.lower(): value or "" for name, value in attrs}
        tag = tag.lower()
        self._tag_stack.append(tag)
        if tag == "title":
            self._capture_title = True
        if tag == "meta" and attrs_dict.get("name", "").lower() == "description":
            self.description = attrs_dict.get("content", "").strip()
        if tag == "a":
            self._active_href = attrs_dict.get("href")
            self._active_anchor = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._capture_title = False
        if tag == "a" and self._active_href:
            label = _clean_text(" ".join(self._active_anchor))
            if label:
                self.anchors.append((label, self._active_href))
            self._active_href = None
            self._active_anchor = []
        if self._tag_stack:
            self._tag_stack.pop()

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if not text:
            return
        if self._capture_title:
            self.title = _clean_text(f"{self.title} {text}")
        if self._active_href is not None:
            self._active_anchor.append(text)
        if self._tag_stack and self._tag_stack[-1] in {"h1", "h2", "h3", "p", "li"}:
            self.text_chunks.append(text)


def load_radar_config(path: Path) -> LeadRadarConfig:
    """Load a lead radar YAML config."""

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("lead radar config must be a YAML mapping")
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError("lead radar config requires at least one source")
    sources = tuple(_source_from_mapping(item, base_dir=path.parent) for item in raw_sources)
    return LeadRadarConfig(sources=sources, min_score=int(payload.get("min_score", 7)))


def run_lead_radar(config: LeadRadarConfig, *, min_score: int | None = None) -> LeadRadarReport:
    """Fetch configured sources, extract raw candidates, and qualify leads."""

    raw_results: list[SearchResult] = []
    for source in config.sources:
        raw_results.extend(_crawl_source(source))
    discovery = discover_leads(raw_results, min_score=config.min_score if min_score is None else min_score)
    return LeadRadarReport(raw_results=tuple(raw_results), discovery=discovery)


def write_raw_results_csv(results: tuple[SearchResult, ...] | list[SearchResult], path: Path) -> None:
    """Write radar raw results in the discovery input schema."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_RESULT_FIELDS)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "title": result.title,
                    "url": result.url,
                    "snippet": result.snippet,
                    "source": result.source,
                    "country": result.country,
                    "published_at": result.published_at,
                }
            )


def _source_from_mapping(item: object, *, base_dir: Path) -> LeadRadarSource:
    if not isinstance(item, dict):
        raise ValueError("each lead radar source must be a mapping")
    name = str(item.get("name") or "").strip()
    url = str(item.get("url") or "").strip()
    if not name or not url:
        raise ValueError("each lead radar source requires name and url")
    return LeadRadarSource(
        name=name,
        url=_normalize_source_url(url, base_dir),
        country=str(item.get("country") or "").strip(),
        kind=str(item.get("kind") or "html").strip().lower(),
        include_links=bool(item.get("include_links", True)),
        emit_page_result=bool(item.get("emit_page_result", True)),
        max_link_results=_positive_int(item.get("max_link_results", 25), field="max_link_results"),
        timeout_seconds=_positive_float(
            item.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS), field="timeout_seconds"
        ),
        max_bytes=_positive_int(item.get("max_bytes", _DEFAULT_MAX_BYTES), field="max_bytes"),
    )


def _positive_int(value: Any, *, field: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"lead radar source {field} must be an integer") from exc
    if number <= 0:
        raise ValueError(f"lead radar source {field} must be greater than zero")
    return number


def _positive_float(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"lead radar source {field} must be a number") from exc
    if number <= 0:
        raise ValueError(f"lead radar source {field} must be greater than zero")
    return number


def _crawl_source(source: LeadRadarSource) -> list[SearchResult]:
    if source.kind == "csv":
        return _parse_csv(source)
    content = _fetch_text(
        source.url,
        timeout_seconds=source.timeout_seconds,
        max_bytes=source.max_bytes,
    )
    if source.kind == "rss":
        return _parse_feed(content, source)
    if source.kind != "html":
        raise ValueError(f"unsupported lead radar source kind: {source.kind}")
    return _parse_html(content, source)


def _parse_csv(source: LeadRadarSource) -> list[SearchResult]:
    """Read a local CSV file and map each row to a SearchResult.

    Accepted column names (case-insensitive, first match wins per field):
      title   : title | cuenta | company | name | account
      url     : url | web | website | link | homepage
      snippet : snippet | description | notes | senal_de_compra | bio | about
      email   : email | contacto | contact | contact_email | mail
      country : country | pais | location | region

    Any email found in the ``email`` column is appended to the snippet so
    ``_extract_email`` in lead_discovery picks it up automatically.
    """
    _ALIASES: dict[str, list[str]] = {
        "title":   ["title", "cuenta", "company", "name", "account"],
        "url":     ["url", "web", "website", "link", "homepage"],
        "snippet": ["snippet", "description", "notes", "senal_de_compra", "bio", "about"],
        "email":   ["email", "contacto", "contact", "contact_email", "mail"],
        "country": ["country", "pais", "location", "region"],
    }

    def _pick(row: dict[str, str], aliases: list[str]) -> str:
        lower = {k.lower().strip(): v for k, v in row.items()}
        for alias in aliases:
            if alias in lower:
                return (lower[alias] or "").strip()
        return ""

    path = Path(source.url)
    results: list[SearchResult] = []
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            title = _pick(row, _ALIASES["title"])
            url = _pick(row, _ALIASES["url"])
            snippet = _pick(row, _ALIASES["snippet"])
            email = _pick(row, _ALIASES["email"])
            country = _pick(row, _ALIASES["country"]) or source.country

            if not title:
                continue

            # Append email into snippet so _extract_email finds it.
            if email:
                snippet = f"{snippet} {email}".strip() if snippet else email

            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    snippet=snippet[:700],
                    source=source.name,
                    country=country,
                )
            )
    return results


def _fetch_text(url: str, *, timeout_seconds: float, max_bytes: int) -> str:
    """Read one source, rejecting non-web schemes and bounding size and time."""

    scheme = urlparse(url).scheme.lower()
    if _looks_like_local_path(url) or not scheme:
        # Local fixtures are resolved at config-load time and carry no scheme on POSIX,
        # while Windows drive letters are parsed as a single-letter scheme.
        return _read_local_text(Path(url), max_bytes=max_bytes)
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(f"lead radar source url must use http or https, got: {scheme}")
    request = Request(
        url,
        headers={
            "User-Agent": "IntegrityLeadRadar/0.1 (+https://example.invalid/integrity)",
            "Accept": "text/html,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - scheme checked above.
        _require_allowed_url(response.geturl() or url)
        data = response.read(max_bytes)
        charset = response.headers.get_content_charset() or "utf-8"
    return data.decode(charset, errors="replace")


def _require_allowed_url(url: str) -> None:
    """Re-check the scheme after redirects, which may leave the allowed set."""

    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(f"lead radar source redirected to a disallowed scheme: {scheme or 'none'}")


def _read_local_text(path: Path, *, max_bytes: int) -> str:
    """Read a configured local fixture without loading an unbounded file."""

    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"lead radar source exceeded max_bytes ({max_bytes}): {path}")
    return data.decode("utf-8")


def _normalize_source_url(url: str, base_dir: Path) -> str:
    if _looks_like_local_path(url):
        return url
    parsed = urlparse(url)
    if parsed.scheme:
        return url
    direct_path = Path(url)
    if direct_path.exists():
        return str(direct_path.resolve())
    return str((base_dir / url).resolve())


def _parse_html(content: str, source: LeadRadarSource) -> list[SearchResult]:
    parser = _RadarHTMLParser()
    parser.feed(content)
    title = parser.title or source.name
    page_text = _clean_text(" ".join([parser.description, *parser.text_chunks]))
    results = []
    if source.emit_page_result:
        results.append(
            SearchResult(
                title=title,
                url=source.url,
                snippet=page_text[:700],
                source=source.name,
                country=source.country,
            )
        )
    if not source.include_links:
        return results
    source_domain = _domain(source.url)
    link_results = []
    for label, href in parser.anchors:
        absolute = urljoin(source.url, href)
        if not _is_candidate_anchor(label, absolute, source_domain):
            continue
        link_results.append(
            SearchResult(
                title=label,
                url=absolute,
                snippet=_anchor_snippet(source),
                source=source.name,
                country=source.country,
            )
        )
        if len(link_results) >= source.max_link_results:
            break
    return results + link_results


def _parse_feed(content: str, source: LeadRadarSource) -> list[SearchResult]:
    root = _parse_xml(content)
    results: list[SearchResult] = []
    for item in root.findall(".//item"):
        results.append(
            SearchResult(
                title=_text(item, "title") or source.name,
                url=_text(item, "link"),
                snippet=_clean_text(_text(item, "description")),
                source=source.name,
                country=source.country,
                published_at=_text(item, "pubDate"),
            )
        )
    if results:
        return results
    for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        link = ""
        link_node = entry.find("{http://www.w3.org/2005/Atom}link")
        if link_node is not None:
            link = link_node.attrib.get("href", "")
        results.append(
            SearchResult(
                title=_atom_text(entry, "title") or source.name,
                url=link,
                snippet=_clean_text(_atom_text(entry, "summary") or _atom_text(entry, "content")),
                source=source.name,
                country=source.country,
                published_at=_atom_text(entry, "updated"),
            )
        )
    return results


def _parse_xml(content: str) -> ET.Element:
    """Parse third-party XML behind a DTD/entity guard.

    The standard-library parser expands internal entities, so a hostile feed can
    request exponential expansion. Rejecting DTD and entity declarations closes that
    route; this is a pre-parse guard, not a general-purpose hardened XML parser.
    """

    declaration = _FORBIDDEN_XML_DECLARATION.search(content)
    if declaration is not None:
        raise ValueError(
            "lead radar feed declares "
            f"<!{declaration.group(1).upper()}, which is not accepted"
        )
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"lead radar feed is not well-formed XML: {exc}") from exc


def _anchor_snippet(source: LeadRadarSource) -> str:
    source_lower = source.name.lower()
    extras = []
    if "ai act" in source_lower or "governance" in source_lower:
        extras.append("AI Act compliance governance audit human oversight European buyers")
    if "agent" in source_lower or "langchain" in source_lower:
        extras.append("AI agent enterprise deployment tool use production workflows")
    context = f" Context: {'; '.join(extras)}." if extras else ""
    if "eu" in source.country.lower() or "europe" in source.country.lower():
        return f"Linked from {source.name}. Context: European enterprise buyers.{context}"
    return f"Linked from {source.name}.{context}"


def _text(node: ET.Element, tag: str) -> str:
    found = node.find(tag)
    return _clean_text("".join(found.itertext())) if found is not None else ""


def _atom_text(node: ET.Element, tag: str) -> str:
    found = node.find(f"{{http://www.w3.org/2005/Atom}}{tag}")
    return _clean_text("".join(found.itertext())) if found is not None else ""


def _is_candidate_anchor(label: str, url: str, source_domain: str) -> bool:
    normalized_label = _clean_text(label).lower()
    if len(normalized_label) < 3 or normalized_label in _EXCLUDED_LINK_LABELS:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "file"}:
        return False
    target_domain = parsed.netloc.lower().removeprefix("www.")
    if any(target_domain == domain or target_domain.endswith(f".{domain}") for domain in _EXCLUDED_LINK_DOMAINS):
        return False
    return not source_domain or not target_domain or target_domain != source_domain


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower().removeprefix("www.")


def _looks_like_local_path(url: str) -> bool:
    return bool(re.match(r"^[a-zA-Z]:[\\/]", url))


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
