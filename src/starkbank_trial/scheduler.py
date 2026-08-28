import time
def run_for_24_hours(client,sleep=time.sleep):
    for batch in range(8):
        client.issue_batch(8,12)
        if batch<7: sleep(10800)
