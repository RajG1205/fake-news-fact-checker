"""Breaking-news retrieval service."""

# News retrieval will be moved here from the legacy logic module in the next
# refactor step. This module is deliberately explicit so imports do not silently
# fall back to fake or canned headlines.

def get_breaking_news():
    from app.services.fact_check_service import _legacy_get_breaking_news
    return _legacy_get_breaking_news()
