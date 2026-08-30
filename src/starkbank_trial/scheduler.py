import time


def run_for_24_hours(client, sleep=time.sleep):
    """Run eight local invoice batches, waiting three hours between batches."""
    for batch in range(8):
        # Each batch creates 8 to 12 invoices through the Stark client.
        client.issue_batch(8, 12)
        if batch < 7:
            # Skip the final wait because no ninth batch will run.
            sleep(10800)
