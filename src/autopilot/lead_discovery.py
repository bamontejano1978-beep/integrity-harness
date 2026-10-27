"""Deterministic lead discovery and qualification for Integrity CRM.

The module intentionally starts with plain CSV/JSON inputs instead of live web
scraping. Search APIs, directories, or manual exports can feed the same shape,
while scoring stays reproducible and reviewable.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


CRM_FIELDS = [
    "cuenta",
    "web",
    "segmento",
    "pais",
    "senal_de_compra",
    "contacto",
    "rol",
    "canal",
    "fecha_contacto",
    "estado",
    "hipotesis_de_dolor",
    "agente_tercero",
    "efectos_sensibles",
    "comprador_identificado",
    "acceso_probable",
    "score_0_12",
    "siguiente_paso",
    "notas",
]


@dataclass(frozen=True)
class SearchResult:
    """One raw result from a search engine, directory, spreadsheet, or notes file."""

    title: str
    url: str = ""
    snippet: str = ""
    source: str = ""
    country: str = ""
    published_at: str = ""

    @property
    def text(self) -> str:
        return " ".join(
            part for part in [self.title, self.snippet, self.url, self.source, self.country] if part
        )

    @property
    def domain(self) -> str:
        if not self.url:
            return ""
        parsed = urlparse(self.url if "://" in self.url else f"https://{self.url}")
        return parsed.netloc.lower().removeprefix("www.")


@dataclass(frozen=True)
class ScoreDimension:
    """A transparent 0-2 score with the evidence that triggered it."""

    value: int
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class LeadScore:
    """Integrity lead score matching the CRM's 0-12 model."""

    agent: ScoreDimension
    impact: ScoreDimension
    urgency: ScoreDimension
    buyer: ScoreDimension
    fit: ScoreDimension
    access: ScoreDimension

    @property
    def total(self) -> int:
        return sum(
            dimension.value
            for dimension in [
                self.agent,
                self.impact,
                self.urgency,
                self.buyer,
                self.fit,
                self.access,
            ]
        )

    def reasons(self) -> list[str]:
        items: list[str] = []
        for name, dimension in [
            ("agent", self.agent),
            ("impact", self.impact),
            ("urgency", self.urgency),
            ("buyer", self.buyer),
            ("fit", self.fit),
            ("access", self.access),
        ]:
            for reason in dimension.reasons:
                items.append(f"{name}: {reason}")
        return items


@dataclass(frozen=True)
class QualifiedLead:
    """One deduplicated, CRM-ready candidate."""

    account: str
    url: str
    segment: str
    country: str
    buying_signal: str
    pain_hypothesis: str
    recommended_role: str
    recommended_channel: str
    next_step: str
    score: LeadScore
    source_count: int = 1
    evidence_urls: tuple[str, ...] = ()
    contact_email: str = ""
    notes: str = ""

    def to_crm_row(self) -> dict[str, str]:
        return {
            "cuenta": self.account,
            "web": self.url,
            "segmento": self.segment,
            "pais": self.country,
            "senal_de_compra": self.buying_signal,
            "contacto": self.contact_email,
            "rol": self.recommended_role,
            "canal": self.recommended_channel,
            "fecha_contacto": "",
            "estado": "por_contactar",
            "hipotesis_de_dolor": self.pain_hypothesis,
            "agente_tercero": "si" if self.score.agent.value >= 1 else "probable",
            "efectos_sensibles": _sensitive_effects_from_text(
                " ".join([self.buying_signal, self.pain_hypothesis])
            ),
            "comprador_identificado": "rol probable" if self.score.buyer.value >= 1 else "no",
            "acceso_probable": "media" if self.score.access.value >= 1 else "baja",
            "score_0_12": str(self.score.total),
            "siguiente_paso": self.next_step,
            "notas": self.notes,
        }


@dataclass(frozen=True)
class DiscoveryReport:
    """Complete output from one discovery run."""

    leads: tuple[QualifiedLead, ...]
    rejected: tuple[dict[str, str], ...] = ()

    def to_json(self) -> str:
        payload = {
            "lead_count": len(self.leads),
            "rejected_count": len(self.rejected),
            "leads": [
                {
                    "account": lead.account,
                    "url": lead.url,
                    "segment": lead.segment,
                    "country": lead.country,
                    "score": lead.score.total,
                    "score_reasons": lead.score.reasons(),
                    "buying_signal": lead.buying_signal,
                    "pain_hypothesis": lead.pain_hypothesis,
                    "next_step": lead.next_step,
                    "evidence_urls": list(lead.evidence_urls),
                }
                for lead in self.leads
            ],
            "rejected": list(self.rejected),
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)


_SIGNALS = {
    "agent": [
        "ai agent",
        "ai agents",
        "agente ia",
        "agentes ia",
        "agentic",
        "claims agent",
        "autonomous workflow",
        "autonomous workflows",
        "tool use",
        "multi-agent",
        "copilot",
        "copilots",
    ],
    "tools": [
        "crm",
        "erp",
        "payment",
        "payments",
        "approval",
        "approvals",
        "pago",
        "pagos",
        "workflow",
        "workflows",
        "api",
        "n8n",
        "rpa",
        "ticketing",
        "case management",
        "document",
        "documents",
        "documental",
        "whatsapp",
        "email",
    ],
    "sensitive": [
        "bank",
        "banking",
        "banca",
        "finance",
        "financial",
        "insurance",
        "seguros",
        "health",
        "healthcare",
        "salud",
        "public sector",
        "sector publico",
        "government",
        "administracion publica",
        "legal",
        "hr",
        "human resources",
        "education",
        "energia",
        "energy",
        "telco",
        "critical infrastructure",
    ],
    "eu": [
        "europe",
        "european",
        "eu market",
        "union market",
        "ai act",
        "aiact",
        "gdpr",
        "ue",
        "europa",
        "spain",
        "espana",
        "france",
        "germany",
    ],
    "governance": [
        "compliance",
        "governance",
        "risk",
        "trust",
        "safety",
        "security",
        "human oversight",
        "logs",
        "audit",
        "auditoria",
        "iso 42001",
        "responsible ai",
        "model risk",
        "vendor risk",
        "procurement",
        "human-in-the-loop",
    ],
    "urgency": [
        "launch",
        "deploy",
        "deployment",
        "deployed",
        "rollout",
        "pilot",
        "production",
        "customers",
        "enterprise",
        "selling",
        "market",
        "introduced",
        "announced",
        "despliegue",
        "piloto",
        "produccion",
        "clientes",
    ],
}


def load_search_results(path: Path) -> list[SearchResult]:
    """Load search results from CSV or JSON.

    CSV headers accepted: title, url, snippet, source, country, published_at.
    JSON input may be a list of objects with the same keys.
    """

    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("JSON lead discovery input must be a list of objects")
        return [_result_from_mapping(item) for item in payload]
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [_result_from_mapping(row) for row in csv.DictReader(handle)]
    raise ValueError("lead discovery input must be .csv or .json")


def discover_leads(results: list[SearchResult], *, min_score: int = 7) -> DiscoveryReport:
    """Deduplicate and qualify raw results into CRM-ready leads."""

    grouped: dict[str, list[SearchResult]] = {}
    for result in results:
        key = result.domain or _slug(result.title)
        grouped.setdefault(key, []).append(result)

    leads: list[QualifiedLead] = []
    rejected: list[dict[str, str]] = []
    for group in grouped.values():
        lead = _qualify_group(group)
        if lead.score.total >= min_score:
            leads.append(lead)
        else:
            rejected.append(
                {
                    "account": lead.account,
                    "url": lead.url,
                    "score": str(lead.score.total),
                    "reason": "below_min_score",
                }
            )
    return DiscoveryReport(
        leads=tuple(sorted(leads, key=lambda lead: (-lead.score.total, lead.account.lower()))),
        rejected=tuple(rejected),
    )


def write_crm_csv(leads: list[QualifiedLead] | tuple[QualifiedLead, ...], path: Path) -> None:
    """Write qualified leads using the current Integrity CRM CSV schema."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CRM_FIELDS)
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead.to_crm_row())


def _result_from_mapping(item: object) -> SearchResult:
    if not isinstance(item, dict):
        raise ValueError("each lead discovery item must be an object")
    return SearchResult(
        title=str(item.get("title") or item.get("cuenta") or "").strip(),
        url=str(item.get("url") or item.get("web") or "").strip(),
        snippet=str(item.get("snippet") or item.get("senal_de_compra") or "").strip(),
        source=str(item.get("source") or item.get("fuente") or "").strip(),
        country=str(item.get("country") or item.get("pais") or "").strip(),
        published_at=str(item.get("published_at") or item.get("fecha") or "").strip(),
    )


def _qualify_group(group: list[SearchResult]) -> QualifiedLead:
    primary = _choose_primary(group)
    text = " ".join(result.text for result in group).lower()
    score = _score_text(text)
    segment = _segment(text, primary.country)
    account = _account_name(primary)
    signal = _buying_signal(group, score)
    pain = _pain_hypothesis(segment, text)
    role = _recommended_role(segment)
    next_step = _next_step(segment, account)
    urls = tuple(dict.fromkeys(result.url for result in group if result.url))
    email = _extract_email([result.text for result in group])
    return QualifiedLead(
        account=account,
        url=primary.url,
        segment=segment,
        country=primary.country or _country_from_text(text),
        buying_signal=signal,
        pain_hypothesis=pain,
        recommended_role=role,
        recommended_channel="LinkedIn",
        next_step=next_step,
        score=score,
        source_count=len(group),
        evidence_urls=urls,
        contact_email=email,
        notes="; ".join(score.reasons()[:5]),
    )


def _score_text(text: str) -> LeadScore:
    agent_hits = _hits(text, _SIGNALS["agent"])
    tool_hits = _hits(text, _SIGNALS["tools"])
    sensitive_hits = _hits(text, _SIGNALS["sensitive"])
    eu_hits = _hits(text, _SIGNALS["eu"])
    governance_hits = _hits(text, _SIGNALS["governance"])
    urgency_hits = _hits(text, _SIGNALS["urgency"])

    return LeadScore(
        agent=_dimension(agent_hits, high=1, medium=1, label="agentic capability"),
        impact=_dimension(
            sensitive_hits + tool_hits,
            high=3,
            medium=1,
            label="sensitive sector or external effect",
        ),
        urgency=_dimension(urgency_hits, high=2, medium=1, label="deployment or sales urgency"),
        buyer=_dimension(
            governance_hits + ["enterprise"] if "enterprise" in text else governance_hits,
            high=2,
            medium=1,
            label="governance/procurement buyer signal",
        ),
        fit=_dimension(
            agent_hits + tool_hits + governance_hits + eu_hits,
            high=4,
            medium=2,
            label="Integrity offer fit",
        ),
        access=_dimension(
            _hits(text, ["demo", "pilot", "sandbox", "api", "documentation", "case study"]),
            high=2,
            medium=1,
            label="likely review access",
        ),
    )


def _dimension(hits: list[str], *, high: int, medium: int, label: str) -> ScoreDimension:
    unique = tuple(dict.fromkeys(hits))
    if len(unique) >= high:
        return ScoreDimension(2, (f"{label}: {', '.join(unique[:4])}",))
    if len(unique) >= medium:
        return ScoreDimension(1, (f"{label}: {', '.join(unique[:3])}",))
    return ScoreDimension(0, ())


def _hits(text: str, terms: list[str]) -> list[str]:
    found: list[str] = []
    for term in terms:
        pattern = r"(?<![a-z0-9])" + re.escape(term.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, text):
            found.append(term)
    return found


def _segment(text: str, country: str) -> str:
    lower_country = country.lower()
    outside_eu = any(token in lower_country for token in ["us", "uk", "latam", "israel", "india"])
    if outside_eu and _hits(text, _SIGNALS["eu"]):
        return "Proveedor no UE / entrada mercado europeo"
    if any(term in text for term in ["consulting", "consultora", "integrator", "integrador", "agency"]):
        return "Vendedor/integrador de agentes"
    if any(term in text for term in ["ai act", "iso 42001", "compliance", "governance", "dpo"]):
        return "Partner compliance / AI Act"
    if any(term in text for term in ["bank", "banca", "insurance", "seguros", "health", "public sector"]):
        return "Comprador final regulado"
    if outside_eu:
        return "Proveedor no UE / entrada mercado europeo"
    return "Cuenta a cualificar"


def _choose_primary(group: list[SearchResult]) -> SearchResult:
    return sorted(group, key=_primary_rank)[0]


def _primary_rank(result: SearchResult) -> tuple[int, int, int]:
    title = result.title.lower()
    title_penalty = 1 if any(term in title for term in ["case study", "blog", "news"]) else 0
    return (title_penalty, 0 if result.url else 1, len(result.title))


def _account_name(result: SearchResult) -> str:
    title = re.sub(r"\s*[-|–].*$", "", result.title).strip()
    title = re.sub(
        r"\s+(AI agents?|agent|agents?)\s+for\s+.*$",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    title = re.sub(r"\s+insurance claims agent\s*$", "", title, flags=re.IGNORECASE).strip()
    if title and title.lower() not in {"website", "site", "homepage", "learn more"}:
        return title[:80]
    if result.domain:
        domain = result.domain.split(":")[0]
        parts = [part for part in domain.split(".") if part not in {"com", "ai", "io", "co", "uk", "de", "fr", "nl", "es", "law"}]
        return (parts[0] if parts else domain.split(".")[0]).replace("-", " ").title()
    return "Unknown account"


def _buying_signal(group: list[SearchResult], score: LeadScore) -> str:
    snippets = [result.snippet for result in group if result.snippet]
    base = snippets[0] if snippets else group[0].title
    reasons = "; ".join(score.reasons()[:3])
    if reasons:
        return f"{base} Signals: {reasons}."
    return base


def _pain_hypothesis(segment: str, text: str) -> str:
    if "Proveedor no UE" in segment:
        return (
            "Necesita evidencia tecnica buyer-facing para reducir friccion con compradores "
            "europeos que preguntan por AI Act, logs, supervision humana y autonomia."
        )
    if "Vendedor/integrador" in segment:
        return (
            "Sus clientes enterprise pueden pedir prueba independiente de limites, tools, "
            "datos, aprobaciones y recuperacion antes de aceptar agentes."
        )
    if "Partner compliance" in segment:
        return (
            "Puede necesitar una capa tecnica de evidencia para complementar AI Act readiness, "
            "gobernanza, auditoria y vigilancia postdespliegue."
        )
    if any(term in text for term in ["bank", "banca", "payment", "insurance", "health"]):
        return (
            "Como comprador/deployer en sector sensible, necesita evidencia propia sobre "
            "autonomia, supervision humana, privacidad, pagos o workflows."
        )
    return (
        "Puede necesitar una revision tecnica independiente si sus agentes tocan herramientas, "
        "datos, workflows o decisiones con impacto operativo."
    )


def _recommended_role(segment: str) -> str:
    if "Proveedor no UE" in segment:
        return "Founder / GTM / Head of Enterprise Sales / compliance"
    if "Vendedor/integrador" in segment:
        return "CEO / CTO / Head of Delivery / AI practice lead"
    if "Partner compliance" in segment:
        return "AI compliance lead / socio tecnologia / DPO"
    if "Comprador" in segment:
        return "AI governance / CISO / vendor risk / operaciones"
    return "AI lead / innovation / risk"


def _next_step(segment: str, account: str) -> str:
    if "Proveedor no UE" in segment:
        return f"Find founder or enterprise sales contact at {account} and send EU market readiness message."
    if "Vendedor/integrador" in segment:
        return f"Buscar CTO/fundador de {account} y enviar mensaje para integradores."
    if "Partner compliance" in segment:
        return f"Contactar {account} como partner tecnico AI Act."
    if "Comprador" in segment:
        return f"Buscar AI governance/vendor risk en {account} y enviar mensaje buyer-side."
    return f"Cualificar {account} antes de contactar."


def _country_from_text(text: str) -> str:
    if any(term in text for term in ["united states", " usa ", " us "]):
        return "US"
    if any(term in text for term in ["united kingdom", " uk "]):
        return "UK"
    if any(term in text for term in ["spain", "espana"]):
        return "ES"
    if "europe" in text or "european" in text:
        return "EU"
    return ""


def _sensitive_effects_from_text(text: str) -> str:
    lower = text.lower()
    effects = []
    for label, terms in [
        ("pagos", ["payment", "payments", "pago", "pagos"]),
        ("datos", ["data", "datos", "privacy", "privacidad", "health", "salud"]),
        ("workflows", ["workflow", "workflows", "crm", "erp", "ticketing"]),
        ("herramientas", ["tools", "tool use", "api", "n8n", "rpa"]),
    ]:
        if any(term in lower for term in terms):
            effects.append(label)
    return "/".join(effects) if effects else "por cualificar"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "unknown"


def _extract_email(texts: list[str]) -> str:
    """Return the first plausible email address found across all texts, or empty string.

    Picks the most specific match: prefers addresses whose local-part contains
    a role keyword (cto, cpo, dpo, ciso, compliance, risk, ai, tech, data) so
    the CRM field lands on the likeliest decision-maker rather than a generic
    info@ address.  Falls back to the first address found if none match.
    """
    _EMAIL_RE = re.compile(
        r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        re.IGNORECASE,
    )
    _ROLE_KEYWORDS = re.compile(
        r"(cto|cpo|ciso|dpo|compliance|risk|ai|tech|data|govern|legal|security)",
        re.IGNORECASE,
    )
    _NOISE = re.compile(
        r"@(example|test|sentry|noreply|no-reply|email\.com|domain\.com)",
        re.IGNORECASE,
    )

    candidates: list[str] = []
    for text in texts:
        for match in _EMAIL_RE.finditer(text):
            email = match.group(0).lower()
            if not _NOISE.search(email):
                candidates.append(email)

    if not candidates:
        return ""

    for email in candidates:
        local = email.split("@")[0]
        if _ROLE_KEYWORDS.search(local):
            return email

    return candidates[0]



    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "unknown"
