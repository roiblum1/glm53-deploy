"""Block until the model answers a one-token completion through the gateway."""
import json
import os
import sys
import time
import urllib.request

url = os.environ["TARGET_URL"].rstrip("/") + "/v1/chat/completions"
body = json.dumps({
    "model": os.environ["MODEL"],
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 1,
}).encode()
headers = {"content-type": "application/json", **json.loads(os.environ.get("EXTRA_HEADERS") or "{}")}
if os.environ.get("API_KEY"):
    headers["authorization"] = "Bearer " + os.environ["API_KEY"]

deadline = time.time() + int(os.environ["READY_TIMEOUT_SECONDS"])
while True:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers), timeout=300) as resp:
            if resp.status == 200:
                print("model is ready", flush=True)
                sys.exit(0)
    except Exception as exc:  # connection refused, 4xx/5xx, TLS: all mean "not yet"
        print(f"not ready: {exc}", flush=True)
    if time.time() > deadline:
        sys.exit(f"{url} did not answer 200 within the ready timeout")
    time.sleep(15)
