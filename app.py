import os
import logging
import bleach
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, render_template, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

from logic import fact_check, get_breaking_news, extract_claims

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verifyai")

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=512 * 1024,  # 512 KB request cap
    RATELIMIT_HEADERS_ENABLED=True,
)

# Render sits behind a proxy. Trust one forwarding hop so IP-based limits work.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
    strategy="fixed-window",
)

MAX_MESSAGE_LENGTH = 5000
MAX_CLAIMS_PER_REQUEST = 3
executor = ThreadPoolExecutor(max_workers=3)


def sanitize_input(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return bleach.clean(text[:MAX_MESSAGE_LENGTH], tags=[], attributes={}, strip=True).strip()


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.path == "/chat" else response.headers.get("Cache-Control", "public, max-age=60")
    return response


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "Request is too large. Keep your claim under 5,000 characters."}), 413


@app.errorhandler(429)
def rate_limited(_):
    return jsonify({
        "error": "Daily limit reached. You can submit up to 5 fact-check questions per day.",
        "code": "DAILY_LIMIT"
    }), 429


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
@limiter.exempt
def health():
    return jsonify({"status": "ok", "service": "VerifyAI"})


@app.route("/chat", methods=["POST"])
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
    cleaned_claims = [c.strip() for c in claims if isinstance(c, str) and c.strip()]
    if not cleaned_claims:
        return jsonify({"responses": [], "error": "No verifiable claim was detected."}), 400

    responses = []
    futures = [executor.submit(fact_check, claim) for claim in cleaned_claims]

    for claim, future in zip(cleaned_claims, futures):
        try:
            result = future.result(timeout=50)
            responses.append({"claim": claim, "result": result})
        except Exception:
            logger.exception("Fact-check failed")
            responses.append({
                "claim": claim,
                "result": {
                    "verdict": "UNCERTAIN",
                    "confidence": 50,
                    "explanation": "The evidence could not be evaluated reliably right now.",
                    "highlights": [],
                    "quotes": [],
                    "sources": [],
                    "trust_analysis": "Verification was incomplete."
                }
            })

    return jsonify({"responses": responses, "limit": {"daily": 5}})


@app.route("/breaking-news")
@limiter.limit("30 per minute")
def breaking_news():
    try:
        return jsonify(get_breaking_news())
    except Exception:
        logger.exception("Breaking news error")
        return jsonify([])


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    app.run(host="0.0.0.0", port=port, debug=debug)
