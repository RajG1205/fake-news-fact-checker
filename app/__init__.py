"""Flask application factory for VerifyAI."""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

import bleach
from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from app.services.fact_check_service import fact_check, extract_claims
from app.services.news_service import get_breaking_news

logger = logging.getLogger("verifyai")
MAX_MESSAGE_LENGTH = 5000
MAX_CLAIMS_PER_REQUEST = 3


def create_app(test_config=None):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.update(
        MAX_CONTENT_LENGTH=512 * 1024,
        RATELIMIT_HEADERS_ENABLED=True,
    )
    if test_config:
        app.config.update(test_config)

    # Trust proxy headers only when explicitly enabled for a known single-proxy deployment.
    if os.getenv("TRUST_PROXY_HEADERS", "false").lower() in {"1", "true", "yes"}:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=app.config.get("RATELIMIT_STORAGE_URI", os.getenv("RATELIMIT_STORAGE_URI", "memory://")),
        strategy="fixed-window",
    )
    executor = ThreadPoolExecutor(max_workers=3)

    def sanitize_input(value):
        if not isinstance(value, str):
            return ""
        return bleach.clean(value[:MAX_MESSAGE_LENGTH], tags=[], attributes={}, strip=True).strip()

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Cache-Control"] = "no-store" if request.path == "/chat" else response.headers.get("Cache-Control", "public, max-age=60")
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify({"error": "Request is too large. Keep your claim under 5,000 characters."}), 413

    @app.errorhandler(429)
    def rate_limited(_error):
        return jsonify({"error": "Daily limit reached. You can submit up to 5 fact-check questions per day.", "code": "DAILY_LIMIT"}), 429

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/health")
    @limiter.exempt
    def health():
        return jsonify({"status": "ok", "service": "VerifyAI"})

    @app.post("/chat")
    @limiter.limit("5 per day")
    @limiter.limit("12 per minute")
    def chat():
        data = request.get_json(silent=True) or {}
        message = sanitize_input(data.get("message", ""))
        if not message:
            return jsonify({"responses": [], "error": "Enter a claim to fact-check."}), 400
        if len(message) < 8:
            return jsonify({"responses": [], "error": "Please enter a more complete claim."}), 400

        claims = extract_claims(message)[:MAX_CLAIMS_PER_REQUEST]
        cleaned_claims = [item.strip() for item in claims if isinstance(item, str) and item.strip()]
        if not cleaned_claims:
            return jsonify({"responses": [], "error": "No verifiable claim was detected."}), 400

        futures = [executor.submit(fact_check, claim) for claim in cleaned_claims]
        responses = []
        for claim, future in zip(cleaned_claims, futures):
            try:
                result = future.result(timeout=50)
                responses.append({"claim": claim, "result": result})
            except Exception:
                logger.exception("Fact-check failed")
                responses.append({"claim": claim, "result": {
                    "verdict": "UNCERTAIN", "confidence": 0,
                    "explanation": "The evidence could not be evaluated reliably right now.",
                    "highlights": [], "quotes": [], "sources": [],
                    "trust_analysis": "Verification was incomplete."
                }})
        return jsonify({"responses": responses, "limit": {"daily": 5}})

    @app.get("/breaking-news")
    @limiter.limit("30 per minute")
    def breaking_news():
        try:
            result = get_breaking_news()
            return jsonify({"status": "ok", "articles": result})
        except Exception:
            logger.exception("Breaking news error")
            return jsonify({"status": "error", "articles": [], "message": "News feeds are temporarily unavailable."}), 502

    # The route and response contract above is intentionally small; register future blueprints here.
    return app
