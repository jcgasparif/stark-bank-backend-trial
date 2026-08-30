import threading
from .config import Settings
from .store import Store
from .client import StarkClient
from .scheduler import run_for_24_hours
from .app import create_app


def main():
    """Start the local webhook server and optional invoice scheduler."""
    settings = Settings()
    store = Store(settings.database_path)
    client = StarkClient(settings, store)
    if settings.run_scheduler:
        # The local scheduler is optional; production uses GitHub Actions instead.
        threading.Thread(target=run_for_24_hours, args=(client,), daemon=True).start()
    # Start Flask after dependencies are ready so routes can use the same objects.
    create_app(settings, client, store).run(
        host=settings.webhook_host, port=settings.webhook_port
    )


if __name__ == "__main__":
    main()
