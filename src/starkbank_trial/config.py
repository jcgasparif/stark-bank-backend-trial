from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("STARK_ENVIRONMENT","sandbox")
    project_id: str = os.getenv("STARK_PROJECT_ID","")
    private_key: str = os.getenv("STARK_PRIVATE_KEY","")
    database_path: Path = Path(os.getenv("DATABASE_PATH","./data/starkbank.sqlite3"))
    webhook_host: str = os.getenv("WEBHOOK_HOST","0.0.0.0")
    webhook_port: int = int(os.getenv("WEBHOOK_PORT","8080"))
    invoice_min_amount: int = int(os.getenv("INVOICE_MIN_AMOUNT","1000"))
    invoice_max_amount: int = int(os.getenv("INVOICE_MAX_AMOUNT","5000"))
    run_scheduler: bool = os.getenv("RUN_SCHEDULER","true").lower()=="true"
    def validate(self):
        if self.environment not in {"sandbox","production"}: raise ValueError("ambiente inválido")
        if not self.project_id or not self.private_key: raise ValueError("credenciais ausentes")
