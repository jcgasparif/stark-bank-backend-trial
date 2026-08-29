from datetime import datetime,timedelta,timezone
import random
from typing import Any
import starkbank
from .domain import DESTINATION,receipt_from
class StarkClient:
    def __init__(self,settings,store):
        settings.validate(); starkbank.user=starkbank.Project(environment=settings.environment,id=settings.project_id,private_key=settings.private_key); self.settings,self.store=settings,store
    def issue_batch(self,minimum=8,maximum=12):
        result=[]
        for _ in range(random.randint(minimum,maximum)):
            invoices=starkbank.invoice.create([starkbank.Invoice(amount=random.randint(self.settings.invoice_min_amount,self.settings.invoice_max_amount),name=random.choice(["Ana Silva","Bruno Costa","Carla Souza","Diego Oliveira"]),tax_id=str(random.randint(10000000000,99999999999)),due=datetime.now(timezone.utc)+timedelta(hours=3),expiration=10800,tags=["starkbank-trial"])])
            invoice=invoices[0]
            self.store.save_invoice(invoice.id,datetime.now(timezone.utc).isoformat()); result.append(invoice)
        return result
    def transfer_paid_invoice(self,invoice_id,event_invoice):
        now=datetime.now(timezone.utc).isoformat()
        if not self.store.claim(invoice_id,now): return None
        try:
            receipt=receipt_from(invoice_id,starkbank.invoice.payment(invoice_id),event_invoice)
            response=starkbank.transfer.create([starkbank.Transfer(amount=receipt.net_amount,external_id=f"starkbank-trial:{invoice_id}",**DESTINATION)])
            transfer=response[0]
            self.store.complete(invoice_id,receipt.net_amount,getattr(transfer,"id","")); return receipt.net_amount
        except Exception: self.store.release(invoice_id); raise
    def create_webhook(self,url): return starkbank.webhook.create(url=url,subscriptions=["invoice"])
