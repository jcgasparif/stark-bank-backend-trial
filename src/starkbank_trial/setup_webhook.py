import argparse
from .config import Settings
from .store import Store
from .client import StarkClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    settings = Settings()
    print(StarkClient(settings, Store(settings.database_path)).create_webhook(args.url))


if __name__ == "__main__":
    main()
