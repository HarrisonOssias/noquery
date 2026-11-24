"""One live ask() through the full stack: Python client -> sidecar -> Claude planner -> Postgres.

Makes one Claude API call (two if the first planned query fails validation), billed to your account.
Run it with the sidecar already started with the planner on; see run-live-ask.sh.
"""

import json
import os
import sys

from noquery import NoqueryClient

client = NoqueryClient(
    os.environ.get("NOQUERY_URL", "http://127.0.0.1:8417"),
    token=os.environ["NOQUERY_SERVICE_TOKEN"],
    timeout=180,
)

question = sys.argv[1] if len(sys.argv) > 1 else "How many trips per day in the last 7 days, by device model?"
auth = {"userId": "u-42", "orgId": "acme", "roles": ["analyst"]}

print("question:", question)
result = client.ask(question, auth=auth, timezone="America/Toronto")
print("type:", result.type)

if result.type == "answer":
    print("planned query:")
    print(json.dumps(result.ir, indent=2))
    print("explanation:", result.explanation)
    print("chart:", result.chart)
    print(f"rows ({len(result.rows)}{', truncated' if result.truncated else ''}):")
    for row in result.labeled_rows()[:10]:
        print("  ", row)
    print(f"took {result.duration_ms} ms")
elif result.type == "clarify":
    print("clarifying question:", result.question, result.options)
elif result.type == "unanswerable":
    print("reason:", result.reason)
else:
    print("errors:", result.errors)
