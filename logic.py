import json
import logging
import os
import re
import hashlib
import random
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verifyai.logic")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "").strip()

try:
    import httpx
except ImportError:
    httpx = None
try:
    import requests
except ImportError:
    requests = None
try:
    from groq import Groq
except ImportError:
    Groq = None
try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None
try:
    import feedparser
except ImportError:
    feedparser = None

PRIMARY_MODEL = "openai/gpt-oss-120b"
FALLBACK_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b"]
MAX_SOURCES = 24
MAX_CLAIM_LENGTH = 1000
_CACHE_MAX_ENTRIES = 500
fact_check_cache = {}

TRUSTED_NEWS = [
    "reuters.com", "bbc.com", "apnews.com", "nytimes.com", "theguardian.com", "aljazeera.com",
    "dw.com", "hindustantimes.com", "washingtonpost.com", "bloomberg.com", "france24.com",
    "npr.org", "wsj.com", "cnbc.com", "factcheck.org", "snopes.com", "politifact.com",
    "fullfact.org", "who.int", "cdc.gov", "nasa.gov", "nature.com", "thehindu.com",
    "indianexpress.com", "time.com", "cnn.com", "forbes.com", "india.gov.in", "pmindia.gov.in",
    "eci.gov.in", "pib.gov.in"
]

NEWS_FEEDS = {
    "World": ["https://feeds.bbci.co.uk/news/world/rss.xml", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"],
    "Science": ["https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"],
    "Technology": ["https://feeds.bbci.co.uk/news/technology/rss.xml"],
    "Health": ["https://feeds.bbci.co.uk/news/health/rss.xml"],
    "Economy": ["https://www.cnbc.com/id/100003114/device/rss/rss.html"],
}

STOP_WORDS = {
    "a","an","the","is","are","was","were","be","been","being","in","on","at","to","for","with","by","about","against","between","into","through","during","before","after","above","below","from","up","down","of","off","over","under","again","further","then","once","here","there","when","where","why","how","all","any","both","each","few","more","most","other","some","such","no","nor","not","only","own","same","so","than","too","very","s","t","can","will","just","don","should","now","did","does","do","has","have","had"
}

groq_client = None
tavily_client = None
if GROQ_API_KEY and Groq is not None:
    try:
        if httpx is not None:
            groq_client = Groq(api_key=GROQ_API_KEY, http_client=httpx.Client(headers={"User-Agent": "VerifyAI/1.0"}))
        else:
            groq_client = Groq(api_key=GROQ_API_KEY)
    except Exception as exc:
        logger.warning("Groq initialization failed: %s", exc)
if TAVILY_API_KEY and TavilyClient is not None:
    try:
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
    except Exception as exc:
        logger.warning("Tavily initialization failed: %s", exc)
LIVE_MODE = tavily_client is not None
logger.info("VerifyAI live mode=%s", LIVE_MODE)


def safe_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                return None
    return None


def normalize_verdict(verdict):
    if not isinstance(verdict, str):
        return "UNCERTAIN"
    v = verdict.strip().upper()
    if v in {"TRUE", "FALSE", "MISLEADING", "UNCERTAIN"}:
        return v
    if "MISLEAD" in v:
        return "MISLEADING"
    if "FALSE" in v:
        return "FALSE"
    if "TRUE" in v:
        return "TRUE"
    return "UNCERTAIN"


def trusted_domain(url):
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
        return any(host == d or host.endswith("." + d) for d in TRUSTED_NEWS) or host.endswith(".gov") or host.endswith(".edu")
    except Exception:
        return False


def relevant_source(content, claim):
    claim_words = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", claim) if len(w) > 2 and w.lower() not in STOP_WORDS}
    if not claim_words:
        return True
    source_words = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", content or "")}
    return len(claim_words & source_words) >= min(2, len(claim_words))


def extract_claims(text):
    parts = re.split(r"[.!?\n]+", text or "")
    claims = [p.strip() for p in parts if len(p.strip()) >= 8]
    return claims[:3] if claims else [text.strip()]


def _claim_is_time_sensitive(claim):
    c = claim.lower()
    patterns = [
        r"\b(current|currently|today|now|present|this year|latest|serving|in office)\b",
        r"\b(president|prime minister|pm|chief minister|minister|ceo|governor|leader)\b",
        r"\b(won|wins|lost|elected|appointed|resigned|arrested|died|launched)\b",
    ]
    return any(re.search(p, c) for p in patterns)


def _authoritative_queries(claim):
    q = [claim, f"fact check {claim}"]
    c = claim.lower()
    if any(x in c for x in ["prime minister", " pm ", "pm of india", "president of india", "chief minister"]):
        q += [f"site:pmindia.gov.in {claim}", f"site:india.gov.in {claim}"]
    elif _claim_is_time_sensitive(claim):
        q += [f"official government {claim}", f"official source {claim}"]
    return q


def search_sources(claim):
    if tavily_client is None:
        return []
    sources, seen = [], set()
    for query in _authoritative_queries(claim):
        try:
            result = tavily_client.search(query=query, max_results=8, search_depth="advanced")
            for item in result.get("results", []):
                url = (item.get("url") or "").strip()
                if not url or url in seen:
                    continue
                content = item.get("content") or ""
                title = item.get("title") or "Source evidence"
                if trusted_domain(url) or relevant_source(content, claim):
                    seen.add(url)
                    sources.append({"title": title, "url": url, "content": content})
        except Exception as exc:
            logger.warning("Tavily query failed: %s", exc)
    # Put authoritative domains first for time-sensitive claims.
    if _claim_is_time_sensitive(claim):
        sources.sort(key=lambda s: 0 if trusted_domain(s["url"]) else 1)
    return sources[:MAX_SOURCES]


def _mock_knowledge_result(claim):
    c = claim.lower()
    if "moon landing" in c or "nasa fake" in c:
        return {"verdict":"FALSE","confidence":99,"explanation":"The Apollo Moon landings are extensively documented and supported by independent physical and observational evidence.","highlights":["Apollo landing sites have been photographed by later lunar orbiters","Lunar samples have been studied by laboratories worldwide","Retroreflectors placed on the Moon remain usable for lunar laser ranging"],"quotes":[],"trust_analysis":"Consistent with NASA documentation and independent scientific evidence.","sources":[]}
    if "vaccine" in c and "autism" in c:
        return {"verdict":"FALSE","confidence":99,"explanation":"High-quality epidemiological evidence does not support a causal relationship between routine vaccination and autism.","highlights":["Large population studies have found no causal link","The original 1998 Wakefield paper was retracted","Major public-health agencies do not support the claim"],"quotes":[],"trust_analysis":"Consistent with major public-health evidence reviews.","sources":[]}
    if "5g" in c and "covid" in c:
        return {"verdict":"FALSE","confidence":99,"explanation":"5G radio signals do not transmit viruses, and COVID-19 transmission occurred independently of 5G availability.","highlights":["Radio waves cannot carry biological viruses","COVID-19 spread in places without 5G coverage","Public-health agencies have rejected the claim"],"quotes":[],"trust_analysis":"Consistent with established virology and telecommunications science.","sources":[]}
    if "flat earth" in c:
        return {"verdict":"FALSE","confidence":100,"explanation":"The Earth is an oblate spheroid, supported by direct astronomical, geodetic, and satellite observations.","highlights":["Satellite navigation depends on an Earth-centered orbital model","Earth's curved shadow is observed during lunar eclipses","Independent observations confirm Earth's shape"],"quotes":[],"trust_analysis":"Supported by basic astronomy, geodesy, and space observations.","sources":[]}
    return None


def _mock_result_for_unknown(claim):
    # Never invent a verdict. Unknown claims must remain uncertain.
    return {"verdict":"UNCERTAIN","confidence":20,"explanation":"There is not enough reliable evidence available to determine whether this claim is true or false.","highlights":["No verified evidence was sufficient for a definitive conclusion","The system will not guess when evidence is inadequate","Review authoritative sources before relying on this claim"],"quotes":[],"trust_analysis":"Insufficient evidence for a reliable assessment.","sources":[],"fallback":True}


def _cache_result(key, result):
    if len(fact_check_cache) >= _CACHE_MAX_ENTRIES:
        fact_check_cache.pop(next(iter(fact_check_cache)))
    fact_check_cache[key] = result


def fact_check(claim):
    if not isinstance(claim, str) or not claim.strip():
        return get_fallback_response(claim, "UNCERTAIN", "Invalid or empty claim.")
    claim = claim.strip()[:MAX_CLAIM_LENGTH]
    key = hashlib.sha256(claim.lower().encode()).hexdigest()
    if key in fact_check_cache:
        return fact_check_cache[key]

    try:
        sources = search_sources(claim) if LIVE_MODE else []
        if not sources and not LIVE_MODE:
            known = _mock_knowledge_result(claim)
            result = known or _mock_result_for_unknown(claim)
            _cache_result(key, result)
            return result
        if not sources:
            result = _mock_knowledge_result(claim) or _mock_result_for_unknown(claim)
            _cache_result(key, result)
            return result

        context = "\n".join(
            f"SOURCE {i}: {s['title']}\nURL: {s['url']}\nCONTENT: {s['content'][:900]}"
            for i, s in enumerate(sources[:10], 1)
        )
        sensitive = _claim_is_time_sensitive(claim)
        prompt = f"""
You are a rigorous professional fact checker. Determine whether the CLAIM is supported, contradicted, misleading, or not established by the supplied evidence.

CLAIM: {claim}
TIME-SENSITIVE: {sensitive}

EVIDENCE:
{context}

Rules:
1. Never guess. If the evidence does not directly establish the claim, return UNCERTAIN.
2. A source mentioning the topic is NOT evidence that the claim is true.
3. For current or time-sensitive claims, prefer authoritative current sources and verify the exact office-holder/status/date stated in the claim.
4. If sources conflict, reflect that conflict and lower confidence rather than choosing arbitrarily.
5. TRUE requires evidence that directly supports the central assertion.
6. FALSE requires evidence that directly contradicts the central assertion.
7. MISLEADING is only for claims that contain a substantially true element but materially misrepresent context.
8. Do not use prior knowledge when the provided evidence is insufficient.
9. Do not state that a source supports the claim unless its content actually supports it.

Return JSON only:
{{"verdict":"TRUE|FALSE|MISLEADING|UNCERTAIN","confidence":0,"explanation":"2-3 sentences","highlights":["fact 1","fact 2","fact 3"],"quotes":["short evidence snippet"],"trust_analysis":"one sentence"}}
"""
        data = None
        for model in [PRIMARY_MODEL] + FALLBACK_MODELS:
            try:
                response = groq_client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role":"system","content":"You are a strict evidence-grounded fact checker. Never invent facts or sources."},
                        {"role":"user","content":prompt},
                    ],
                    temperature=0,
                    response_format={"type":"json_object"},
                    timeout=30,
                )
                candidate = safe_json(response.choices[0].message.content)
                if isinstance(candidate, dict) and candidate.get("verdict"):
                    data = candidate
                    break
            except Exception as exc:
                logger.warning("Groq model %s failed: %s", model, exc)

        if not data:
            result = _mock_knowledge_result(claim) or {**_mock_result_for_unknown(claim), "sources": sources[:8], "trust_analysis": "Live sources were retrieved, but the analysis model was unavailable; no verdict was fabricated."}
        else:
            verdict = normalize_verdict(data.get("verdict"))
            confidence = max(0, min(100, int(data.get("confidence", 50))))
            # Safety gate: positive verdicts require multiple relevant sources.
            relevant_count = sum(1 for s in sources if relevant_source(s.get("content", ""), claim))
            authoritative_count = sum(1 for s in sources if trusted_domain(s.get("url", "")))
            if verdict == "TRUE" and relevant_count < 2:
                verdict, confidence = "UNCERTAIN", min(confidence, 55)
            if sensitive and verdict in {"TRUE", "FALSE"} and authoritative_count == 0:
                verdict, confidence = "UNCERTAIN", min(confidence, 55)
            result = {
                "verdict": verdict,
                "confidence": confidence,
                "explanation": str(data.get("explanation") or "Insufficient evidence for a reliable conclusion.").strip(),
                "highlights": data.get("highlights") if isinstance(data.get("highlights"), list) else [],
                "quotes": data.get("quotes") if isinstance(data.get("quotes"), list) else [],
                "trust_analysis": str(data.get("trust_analysis") or "Evidence quality assessed from retrieved sources.").strip(),
                "sources": sources[:8],
            }
        _cache_result(key, result)
        return result
    except Exception as exc:
        logger.exception("Fact check failed: %s", exc)
        result = _mock_knowledge_result(claim) or {**_mock_result_for_unknown(claim), "sources": sources[:8] if "sources" in locals() else [], "trust_analysis": "The verification pipeline encountered an error; no verdict was fabricated."}
        _cache_result(key, result)
        return result


def get_fallback_response(claim, verdict="UNCERTAIN", explanation=""):
    return {"verdict":normalize_verdict(verdict),"confidence":50,"explanation":explanation or "The claim could not be reliably evaluated.","highlights":[],"quotes":[],"trust_analysis":"System fallback evaluation.","sources":[],"fallback":True}


def _infer_category(title, default="World"):
    lowered = title.lower()
    if any(w in lowered for w in ["tech", "ai", "chip", "cyber", "software", "data", "google", "apple", "microsoft", "robot"]): return "Technology"
    if any(w in lowered for w in ["health", "vaccine", "fda", "cancer", "hospital", "disease", "virus", "medical", "doctor", "medicine"]): return "Health"
    if any(w in lowered for w in ["market", "economy", "inflation", "bank", "rate", "stocks", "finance", "trade", "gdp", "dollar", "crypto"]): return "Economy"
    if any(w in lowered for w in ["climate", "space", "nasa", "planet", "orbit", "energy", "science", "study", "research", "physics"]): return "Science"
    return default


def get_breaking_news():
    headlines = []
    if NEWS_API_KEY and requests is not None:
        try:
            resp = requests.get(f"https://newsapi.org/v2/top-headlines?language=en&pageSize=20&apiKey={NEWS_API_KEY}", headers={"User-Agent":"VerifyAI/1.0"}, timeout=6)
            if resp.status_code == 200:
                for idx, article in enumerate(resp.json().get("articles", [])):
                    title = (article.get("title") or "").strip()
                    url = article.get("url") or "#"
                    if title and title != "[Removed]" and not any(x["title"] == title for x in headlines):
                        headlines.append({"title":title,"category":_infer_category(title),"url":url,"source":article.get("source",{}).get("name","News"),"publishedAt":article.get("publishedAt","")})
                if headlines: return headlines[:20]
        except Exception as exc:
            logger.warning("NewsAPI failed: %s", exc)
    if feedparser is not None:
        for category, feeds in NEWS_FEEDS.items():
            for feed in feeds:
                try:
                    parsed = feedparser.parse(feed)
                    for entry in parsed.entries[:3]:
                        title = getattr(entry, "title", "").strip()
                        link = getattr(entry, "link", "#")
                        if title and not any(x["title"] == title for x in headlines):
                            headlines.append({"title":title,"category":category,"url":link,"source":urlparse(link).netloc.replace("www.","") or "RSS","publishedAt":getattr(entry,"published","")})
                except Exception:
                    continue
    return headlines[:20]