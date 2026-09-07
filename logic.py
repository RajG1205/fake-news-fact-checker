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
                        headlines.append({"title":title,"category":_infer_category(title),"url":url,"source":article.get("source",{}).get("name","News"),"publishedAt":article.get("publishedAt",""),"image":article.get("urlToImage") or ""})
                if headlines: return headlines[:20]
        except Exception as exc:
            logger.warning("NewsAPI failed: %s", exc)
    if feedparser is not None:
        for category, feeds in NEWS_FEEDS.items():
            for feed in feeds:
                try:
                    parsed = feedparser.parse(feed)
                    for entry in parsed.entries[:8]:
                        title = getattr(entry, "title", "").strip()
                        link = getattr(entry, "link", "#")
                        if title and not any(x["title"] == title for x in headlines):
                            img = ""
                            try:
                                media = getattr(entry, "media_content", None) or getattr(entry, "media_thumbnail", None) or []
                                if media and isinstance(media, list):
                                    img = media[0].get("url", "") or ""
                            except Exception:
                                pass
                            headlines.append({"title":title,"category":category,"url":link,"source":urlparse(link).netloc.replace("www.","") or "RSS","publishedAt":getattr(entry,"published",""),"image":img})
                except Exception:
                    continue
    return headlines[:20]