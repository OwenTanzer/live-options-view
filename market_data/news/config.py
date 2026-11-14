"""Sources, topic streams and deterministic rules for the news archive.

Feed URLs, relevance keywords, exclusions and category rules are copied
verbatim from the pinned prototypes (see SOURCE_BASELINES). Only the four
fixture-backed sources are enabled; every other source is configured but
disabled until a release step enables it. Relevance and categories are
versioned enrichment (classify.py), not collection filters: every entry of an
enabled source is archived as an observation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

SCHEMA_VERSION = "news-record/v1"
MANIFEST_SCHEMA = "news-run-manifest/v3"   # v3 adds source_scope (the run's full source roster)
READABLE_MANIFEST_SCHEMAS = ("news-run-manifest/v2", MANIFEST_SCHEMA)
COMMIT_SCHEMA = "news-run-commit/v1"
INDEX_SCHEMA = "news-index/v1"
CODE_VERSION = "news-m2.1"
ADAPTER_VERSION = "rss-v1"
RULES_VERSION = "rules-v1"
NAMESPACE = "news/v1"

SOURCE_BASELINES = {
    "oil": "OwenTanzer/oil-futures@e4b3308dcadbf553ec04ddb0143829d17beb493e rss_collect.py, mediaflow_classify.py",
    "ai_buildout": "OwenTanzer/ai-bubble-dashboard@dfc8f592211f13dd31c1db80ffd5e4b422ca3ae3 rss_collect.py, mediaflow_classify.py",
}

# Bounds from the build contract.
REQUEST_TIMEOUT_S = 20
MAX_RETRIES = 2
RETRY_BACKOFF_S = (2.0, 4.0)
MAX_CONCURRENT_SOURCES = 4
CYCLE_BUDGET_S = 180
INTERVAL_S = 900
MAX_RESPONSE_BYTES = 5_000_000
MAX_EXCERPT_CHARS = 2_000
LATEST_LIMIT = 200
LEASE_TTL_S = 900

# Public view generations (G3). Owner-approved defaults (2026-11-12) from the accepted G2 v2 design.
# CLOCK_EPSILON_S is an ASSUMED bound on the object store's server clock; it has not been validated
# for any provider, so expiry deletion stays refused until a deployment sets clock_bound_verified.
GEN_READER_GRACE_S = 45 * 60      # nominal minimum public lifetime after a generation stops being current
GEN_CLOCK_EPSILON_S = 120         # assumed store-clock error bound
GEN_CLOCK_SKEW_S = 120            # allowed producer/store elapsed disagreement
GEN_PAGE_CAP = 6300               # public page objects (5 x 1,260 at the pilot maximum). G6: counted as pages
                                  # reserved in the publish head (pinned, open and retiring incarnations, written
                                  # or not) plus public pages with no head record; it pauses and never evicts
GEN_WITHDRAW_TOMBSTONE_ATTEMPTS = 3   # G6: tombstone writes per publish-withdraw call; each has a recorded result
                                      # (a 412 re-reads the index first). Not a retry of an ambiguous write
GEN_INDEX_LEASE_S = 30 * 60       # how long the panel follows links from one index (from request start)
GEN_READER_DEADLINE_S = 20        # panel per-request deadline (index and pages)
GEN_PROXY_DEADLINE_S = 15         # Worker upstream deadline for News keys (below the panel's)


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    streams: tuple[str, ...]
    enabled: bool = False
    kind: str = "rss"


@dataclass(frozen=True)
class Stream:
    id: str
    label: str
    keywords: tuple[str, ...]
    exclude: str
    arc_rules: tuple[tuple[str, str], ...]  # (arc, pattern) evaluated in order
    arcs: tuple[str, ...]
    keyword_re: re.Pattern = field(init=False, repr=False, compare=False)
    exclude_re: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "keyword_re", re.compile("|".join(self.keywords), re.IGNORECASE))
        object.__setattr__(self, "exclude_re", re.compile(self.exclude, re.IGNORECASE))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# ── Pinned FEEDS maps (verbatim URLs) ───────────────────────────────────────
OIL_FEEDS = {
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Middle East Eye": "https://www.middleeasteye.net/rss",
    "BBC World": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Guardian World": "https://www.theguardian.com/world/rss",
    "France24 Middle East": "https://www.france24.com/en/middle-east/rss",
    "Mehr News": "https://en.mehrnews.com/rss",
    "OilPrice.com": "https://oilprice.com/rss/main",
    "EIA Today in Energy": "https://www.eia.gov/rss/todayinenergy.xml",
    "EIA Press Releases": "https://www.eia.gov/rss/press_rss.xml",
    "Japan PMO": "https://japan.kantei.go.jp/index-e2.rdf",
    "DW World": "https://rss.dw.com/rdf/rss-en-all",
    "RFE/RL": "https://www.rferl.org/api/epiqq",
    "Times of Israel": "https://www.timesofisrael.com/feed/",
    "Arab News PK": "https://www.arabnews.pk/rss.xml",
    "Gulf Today News": "https://www.gulftoday.ae/rssFeed/55/",
    "Gulf Today Business": "https://www.gulftoday.ae/rssFeed/52/",
    "gCaptain": "https://gcaptain.com/feed/",
    "Marine Insight": "https://www.marineinsight.com/feed/",
    "GNews: iran hormuz": "https://news.google.com/rss/search?q=iran+hormuz&hl=en-US&gl=US&ceid=US:en",
    "GNews: iran strait oil": "https://news.google.com/rss/search?q=iran+strait+oil&hl=en-US&gl=US&ceid=US:en",
    "GNews: IRGC missile": "https://news.google.com/rss/search?q=IRGC+missile&hl=en-US&gl=US&ceid=US:en",
    "GNews: hormuz tanker": "https://news.google.com/rss/search?q=hormuz+tanker&hl=en-US&gl=US&ceid=US:en",
    "GNews: iran nuclear deal": "https://news.google.com/rss/search?q=iran+nuclear+deal&hl=en-US&gl=US&ceid=US:en",
    "GNews: hormuz war risk insurance": "https://news.google.com/rss/search?q=hormuz+war+risk+insurance&hl=en-US&gl=US&ceid=US:en",
    "GNews: hormuz AIS tanker": "https://news.google.com/rss/search?q=hormuz+AIS+tanker&hl=en-US&gl=US&ceid=US:en",
    "GNews: brent wti iran hormuz": "https://news.google.com/rss/search?q=brent+wti+iran+hormuz&hl=en-US&gl=US&ceid=US:en",
    "GNews: OPEC spare capacity hormuz": "https://news.google.com/rss/search?q=OPEC+spare+capacity+hormuz&hl=en-US&gl=US&ceid=US:en",
    "GNews: CENTCOM iran hormuz": "https://news.google.com/rss/search?q=CENTCOM+iran+hormuz&hl=en-US&gl=US&ceid=US:en",
    "GNews: OFAC iran oil sanctions": "https://news.google.com/rss/search?q=OFAC+iran+oil+sanctions&hl=en-US&gl=US&ceid=US:en",
    "GNews: Kharg Jask oil terminal": "https://news.google.com/rss/search?q=Kharg+Jask+oil+terminal&hl=en-US&gl=US&ceid=US:en",
    "GNews: japan china iran oil": "https://news.google.com/rss/search?q=japan+china+iran+oil&hl=en-US&gl=US&ceid=US:en",
    "GNews: russia oil exports sanctions": "https://news.google.com/rss/search?q=russia+oil+exports+sanctions&hl=en-US&gl=US&ceid=US:en",
    "GNews: ukraine russian energy": "https://news.google.com/rss/search?q=ukraine+russian+oil+refinery+pipeline&hl=en-US&gl=US&ceid=US:en",
    "GNews: china iran oil imports": "https://news.google.com/rss/search?q=china+iran+oil+imports+sanctions&hl=en-US&gl=US&ceid=US:en",
    "GNews: china teapot refiners": "https://news.google.com/rss/search?q=china+teapot+refiners+iranian+crude&hl=en-US&gl=US&ceid=US:en",
    "GNews: china strategic petroleum reserve": "https://news.google.com/rss/search?q=china+strategic+petroleum+reserve+crude&hl=en-US&gl=US&ceid=US:en",
    "GNews: PLA navy hormuz escort": "https://news.google.com/rss/search?q=PLA+navy+hormuz+tanker+escort&hl=en-US&gl=US&ceid=US:en",
    "GNews: PLA navy malacca security": "https://news.google.com/rss/search?q=PLA+navy+malacca+strait+security&hl=en-US&gl=US&ceid=US:en",
    "Bing: iran hormuz": "https://www.bing.com/news/search?q=iran+hormuz&format=rss",
    "Bing: hormuz tanker": "https://www.bing.com/news/search?q=hormuz+tanker&format=rss",
    "Bing: IRGC missile": "https://www.bing.com/news/search?q=IRGC+missile&format=rss",
    "Bing: hormuz war risk": "https://www.bing.com/news/search?q=hormuz+war+risk+insurance&format=rss",
    "Bing: hormuz AIS tanker": "https://www.bing.com/news/search?q=hormuz+AIS+tanker&format=rss",
    "Bing: brent WTI iran": "https://www.bing.com/news/search?q=brent+WTI+iran&format=rss",
    "Bing: CENTCOM iran hormuz": "https://www.bing.com/news/search?q=CENTCOM+iran+hormuz&format=rss",
    "Bing: japan china iran oil": "https://www.bing.com/news/search?q=japan+china+iran+oil&format=rss",
    "Bing: russia oil exports": "https://www.bing.com/news/search?q=russia+oil+exports+sanctions&format=rss",
    "Bing: ukraine russian energy": "https://www.bing.com/news/search?q=ukraine+russian+oil+refinery+pipeline&format=rss",
    "Bing: china iran oil imports": "https://www.bing.com/news/search?q=china+iran+oil+imports+sanctions&format=rss",
    "Bing: china teapot refiners": "https://www.bing.com/news/search?q=china+teapot+refiners+iranian+crude&format=rss",
    "Bing: china strategic petroleum reserve": "https://www.bing.com/news/search?q=china+strategic+petroleum+reserve+crude&format=rss",
    "Bing: PLA navy hormuz escort": "https://www.bing.com/news/search?q=PLA+navy+hormuz+tanker+escort&format=rss",
    "Bing: PLA navy malacca security": "https://www.bing.com/news/search?q=PLA+navy+malacca+strait+security&format=rss",
    "Reddit r/iran": "https://www.reddit.com/r/iran/.rss",
    "Reddit r/worldnews": "https://www.reddit.com/r/worldnews/.rss",
    "Reddit r/energy": "https://www.reddit.com/r/energy/.rss",
}

AI_FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "Ars Technica": "http://feeds.arstechnica.com/arstechnica/index",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "CNBC Technology": "https://www.cnbc.com/id/19854910/device/rss/rss.html",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
    "EIA Today in Energy": "https://www.eia.gov/rss/todayinenergy.xml",
    "EIA Press Releases": "https://www.eia.gov/rss/press_rss.xml",
    "GNews: ai bubble": "https://news.google.com/rss/search?q=ai+bubble&hl=en-US&gl=US&ceid=US:en",
    "GNews: ai datacenter buildout": "https://news.google.com/rss/search?q=ai+datacenter+buildout&hl=en-US&gl=US&ceid=US:en",
    "GNews: hyperscaler capex": "https://news.google.com/rss/search?q=hyperscaler+capex&hl=en-US&gl=US&ceid=US:en",
    "GNews: nvidia gpu supply": "https://news.google.com/rss/search?q=nvidia+gpu+supply&hl=en-US&gl=US&ceid=US:en",
    "GNews: semiconductor supply chain": "https://news.google.com/rss/search?q=semiconductor+supply+chain&hl=en-US&gl=US&ceid=US:en",
    "GNews: data center power demand": "https://news.google.com/rss/search?q=data+center+power+demand&hl=en-US&gl=US&ceid=US:en",
    "GNews: ai credit default swap": "https://news.google.com/rss/search?q=ai+credit+default+swap&hl=en-US&gl=US&ceid=US:en",
    "GNews: cloud capex earnings": "https://news.google.com/rss/search?q=cloud+capex+earnings&hl=en-US&gl=US&ceid=US:en",
    "GNews: openai anthropic funding": "https://news.google.com/rss/search?q=openai+anthropic+funding&hl=en-US&gl=US&ceid=US:en",
    "GNews: ai chip export controls": "https://news.google.com/rss/search?q=ai+chip+export+controls&hl=en-US&gl=US&ceid=US:en",
    "GNews: power grid ai datacenter": "https://news.google.com/rss/search?q=power+grid+ai+datacenter&hl=en-US&gl=US&ceid=US:en",
    "GNews: tech corporate bond spreads": "https://news.google.com/rss/search?q=tech+corporate+bond+spreads&hl=en-US&gl=US&ceid=US:en",
    "GNews: ai stock valuation bubble": "https://news.google.com/rss/search?q=ai+stock+valuation+bubble&hl=en-US&gl=US&ceid=US:en",
    "Bing: ai bubble": "https://www.bing.com/news/search?q=ai+bubble&format=rss",
    "Bing: hyperscaler capex": "https://www.bing.com/news/search?q=hyperscaler+capex&format=rss",
    "Bing: nvidia gpu supply": "https://www.bing.com/news/search?q=nvidia+gpu+supply&format=rss",
    "Bing: data center power demand": "https://www.bing.com/news/search?q=data+center+power+demand&format=rss",
    "Bing: ai credit default swap": "https://www.bing.com/news/search?q=ai+credit+default+swap&format=rss",
    "Bing: openai anthropic funding": "https://www.bing.com/news/search?q=openai+anthropic+funding&format=rss",
    "Reddit r/hardware": "https://www.reddit.com/r/hardware/.rss",
    "Reddit r/investing": "https://www.reddit.com/r/investing/.rss",
    "Reddit r/energy": "https://www.reddit.com/r/energy/.rss",
    "Reddit r/artificial": "https://www.reddit.com/r/artificial/.rss",
}

# The fixture-backed first slice. Everything else stays disabled.
ENABLED_SOURCE_NAMES = frozenset({"OilPrice.com", "EIA Today in Energy", "TechCrunch", "Ars Technica"})


def _build_sources() -> dict[str, Source]:
    members: dict[str, list[str]] = {}
    urls: dict[str, str] = {}
    for stream_id, feeds in (("oil", OIL_FEEDS), ("ai_buildout", AI_FEEDS)):
        for name, url in feeds.items():
            if name in urls and urls[name] != url:
                raise ValueError(f"feed {name!r} has different URLs across streams")
            urls[name] = url
            members.setdefault(name, []).append(stream_id)
    out = {}
    for name, url in urls.items():
        src = Source(id=slug(name), name=name, url=url, streams=tuple(members[name]),
                     enabled=name in ENABLED_SOURCE_NAMES)
        if src.id in out:
            raise ValueError(f"duplicate source id {src.id}")
        out[src.id] = src
    return out


SOURCES: dict[str, Source] = _build_sources()

# ── Streams: keywords/exclusions from rss_collect.py, rules from mediaflow_classify.py ──
OIL = Stream(
    id="oil",
    label="Geopolitical Oil",
    keywords=(
        "iran", "hormuz", "irgc", "tehran", "persian gulf",
        "strait", "khamenei", "pezeshkian", "nuclear deal",
        "jcpoa", "enrichment", "sanctions", "arabian sea",
        "gulf of oman", "ballistic missile", "tanker seizure",
        "operation epic fury", "middle east", "mideast", "oil",
        "crude", "opec", "spr", "energy security", "shipping",
        "maritime", "vessel", "fujairah", "kuwait", "bahrain",
        "brent", "wti", "refinery", "refineries", "oil pipeline",
        "diesel", "fuel oil",
    ),
    exclude=r"world cup|football|sport|cricket|tennis|olympics",
    arc_rules=(
        ("KINETIC", r"\b(missile|drone|strike|attack|centcom|irgc|airbase|f-35|mq-9|barrage)\b"),
        ("STRAIT_SHIPPING", r"\b(hormuz|tanker|shipping|vessel|lng|ais|maritime|transit|ship)\b"),
        ("MARKET", r"\b(brent|wti|oil|crude|futures|price|opec|market|barrel)\b"),
        ("IEA_SUPPLY", r"\b(iea|eia|inventory|stockpile|spr|supply|production)\b"),
        ("DIPLOMATIC", r"\b(talks|deal|ministry|minister|diplomat|un |iaea|nuclear|sanction|ceasefire)\b"),
    ),
    arcs=("KINETIC", "DIPLOMATIC", "STRAIT_SHIPPING", "MARKET", "IEA_SUPPLY", "UNMAPPED"),
)

AI_BUILDOUT = Stream(
    id="ai_buildout",
    label="AI Buildout",
    keywords=(
        "artificial intelligence", "\\bai\\b", "openai", "anthropic claude", "nvidia",
        "amd\\b", "tsmc", "semiconductor", "\\bchip\\b", "\\bgpu\\b", "hbm memory",
        "data ?center", "hyperscaler", "capital expenditure", "\\bcapex\\b",
        "cloud demand", "cloud computing", "\\bcompute\\b", "training cluster",
        "power grid", "power constraint", "electricity demand", "megawatt", "gigawatt",
        "credit default swap", "credit spread", "bond issuance", "refinancing",
        "cost of capital", "interest rate", "\\bvaluation\\b", "overbuild",
        "\\bbubble\\b", "microsoft azure", "google cloud", "amazon aws", "\\bmeta\\b",
        "oracle cloud", "coreweave", "sam altman", "jensen huang",
    ),
    exclude=(r"potato chip|fish and chips|poker chip|chip shot|weather|forecast|"
             r"recipe|meta[- ]analysis|metaverse fashion"),
    arc_rules=(
        ("SEMIS", r"\b(nvidia|amd|tsmc|semiconductor|chip|gpu|hbm|foundry|asml|export control)\b"),
        ("DATACENTER", r"\b(data ?center|colocation|cooling|substation|construction|permitting)\b"),
        ("ENERGY", r"\b(power grid|electricity|megawatt|gigawatt|utility|turbine|nuclear power)\b"),
        ("HYPERSCALE", r"\b(microsoft|azure|google cloud|amazon|aws|meta|oracle cloud|hyperscaler|capex|capital expenditure)\b"),
        ("SOFTWARE", r"\b(software|saas|copilot|enterprise adoption|productivity|margin)\b"),
        ("CREDIT", r"\b(credit default swap|\bcds\b|bond|credit spread|refinanc|rating agency|cost of capital)\b"),
        ("SENTIMENT", r"\b(valuation|bubble|overbuild|investor sentiment|earnings expectation|short seller|selloff)\b"),
    ),
    arcs=("SEMIS", "HYPERSCALE", "SOFTWARE", "ENERGY", "DATACENTER", "CREDIT", "SENTIMENT", "UNMAPPED"),
)

STREAMS: dict[str, Stream] = {s.id: s for s in (OIL, AI_BUILDOUT)}


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def rules_digest(streams: dict[str, Stream] = STREAMS) -> str:
    return _digest({
        "version": RULES_VERSION,
        "streams": {s.id: {"keywords": list(s.keywords), "exclude": s.exclude,
                           "arc_rules": [list(r) for r in s.arc_rules], "arcs": list(s.arcs)}
                    for s in streams.values()},
    })


def config_digest(sources: dict[str, Source] = SOURCES, streams: dict[str, Stream] = STREAMS) -> str:
    return _digest({
        "schema": SCHEMA_VERSION, "adapter": ADAPTER_VERSION, "rules": rules_digest(streams),
        "sources": {s.id: [s.name, s.url, list(s.streams), s.enabled] for s in sources.values()},
        "bounds": [REQUEST_TIMEOUT_S, MAX_RETRIES, list(RETRY_BACKOFF_S), MAX_CONCURRENT_SOURCES,
                   CYCLE_BUDGET_S, MAX_RESPONSE_BYTES, MAX_EXCERPT_CHARS],
    })


def enabled_sources(sources: dict[str, Source] = SOURCES, streams=None,
                    extra: tuple[str, ...] = ()) -> list[Source]:
    """Enabled sources for the selected streams; `extra` enables named source ids explicitly."""
    wanted = set(streams or STREAMS)
    unknown = set(extra) - set(sources)
    if unknown:
        raise ValueError(f"unknown source ids: {sorted(unknown)}")
    return [s for s in sources.values()
            if (s.enabled or s.id in extra) and wanted.intersection(s.streams)]
