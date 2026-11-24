"""Calls a running noquery sidecar from Python.

Start the sidecar first (see README), then:
    NOQUERY_SERVICE_TOKEN=... python examples/sidecar/example.py
"""

import os

from noquery import NoqueryClient, PlannerUnavailableError, QueryValidationError

client = NoqueryClient(
    os.environ.get("NOQUERY_URL", "http://127.0.0.1:8417"),
    token=os.environ["NOQUERY_SERVICE_TOKEN"],
)

# Who is asking. In a real app this comes from your own login/session, never from user input.
auth = {"userId": "u-42", "orgId": "acme", "roles": ["analyst"]}

print("health:", client.health())

# 1. What can this user ask about?
catalog = client.catalog(auth)
for entity in catalog["entities"]:
    measures = ", ".join(m["ref"] for m in entity["measures"])
    print(f"{entity['name']}: {measures}")

# 2. Run a query object directly (no LLM involved).
result = client.run(
    {
        "version": 1,
        "measures": ["trips.count", "trips.total_distance_km"],
        "dimensions": ["devices.model"],
        "time": {"field": "trips.started_at", "range": "last_7_days"},
        "orderBy": [{"field": "trips.count", "dir": "desc"}],
    },
    auth=auth,
)
print("\n" + result.explanation)
for row in result.labeled_rows():
    print(row)

# 3. Invalid queries raise, with the reasons.
try:
    client.run({"version": 1, "measures": ["devices.count"], "dimensions": ["devices.serial"]}, auth=auth)
except QueryValidationError as error:
    print("\nrejected:", [e["message"] for e in error.errors])

# 4. Ask in plain English (needs the planner; skipped when the sidecar runs with NOQUERY_PLANNER=off).
try:
    question = "How many trips per day in the last 7 days, by device model?"
    answer = client.ask(question, auth=auth, timezone="America/Toronto")
    if answer.type == "answer":
        print("\n" + answer.explanation)
        print("chart:", answer.chart)
        for row in answer.rows[:5]:
            print(row)
        # A follow-up that builds on the previous answer.
        follow_up = client.ask("Only the X1 model", auth=auth, timezone="America/Toronto", history=[answer.to_turn(question)])
        print("\nfollow-up:", follow_up.explanation)
    elif answer.type == "clarify":
        print("\nclarify:", answer.question, answer.options)
    else:
        print("\n" + answer.type + ":", answer.reason or answer.errors)
except PlannerUnavailableError:
    print("\nask() skipped: the sidecar is running without a planner.")
