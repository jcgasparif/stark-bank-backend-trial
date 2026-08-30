from flask import Flask, jsonify, request
from .config import Settings
from .store import Store
from .client import StarkClient
from .service import process_webhook


def create_app(settings=None, client=None, store=None):
    """Create the local Flask app used for development and manual testing."""
    # Optional arguments make the app easy to test with fake dependencies.
    settings = settings or Settings()
    store = store or Store(settings.database_path)
    client = client or StarkClient(settings, store)
    app = Flask(__name__)

    @app.get("/health")
    def health():
        """Return a lightweight readiness response for the local server."""
        return jsonify(status="ok")

    @app.post("/webhooks/starkbank")
    def webhook():
        """Validate and process a webhook received by the local HTTP server."""
        signature = request.headers.get("Digital-Signature")
        if not signature:
            # Reject unsigned requests before parsing or changing any state.
            return jsonify(error="Digital-Signature header is required"), 400
        try:
            # Flask provides the raw signed body required by Stark Bank parsing.
            return (
                jsonify(
                    result=process_webhook(request.get_data(), signature, client, store)
                ),
                200,
            )
        except Exception:
            # Hide internal details from the caller while keeping the traceback locally.
            app.logger.exception("webhook failed")
            return jsonify(error="temporary processing failure"), 500

    return app
