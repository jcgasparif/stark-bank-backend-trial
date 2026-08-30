from flask import Flask, jsonify, request
from .config import Settings
from .store import Store
from .client import StarkClient
from .service import process_webhook


def create_app(settings=None, client=None, store=None):
    settings = settings or Settings()
    store = store or Store(settings.database_path)
    client = client or StarkClient(settings, store)
    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.post("/webhooks/starkbank")
    def webhook():
        signature = request.headers.get("Digital-Signature")
        if not signature:
            return jsonify(error="Digital-Signature header is required"), 400
        try:
            return (
                jsonify(
                    result=process_webhook(request.get_data(), signature, client, store)
                ),
                200,
            )
        except Exception:
            app.logger.exception("webhook failed")
            return jsonify(error="temporary processing failure"), 500

    return app
