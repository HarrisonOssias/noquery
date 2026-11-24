import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from noquery import (
    AuthenticationError,
    NoqueryClient,
    NoqueryError,
    PlannerFailedError,
    PlannerRefusalError,
    PlannerUnavailableError,
    QueryValidationError,
)

ANSWER = {
    "type": "answer",
    "ir": {"version": 1, "measures": ["trips.count"], "dimensions": ["devices.model"]},
    "sql": "SELECT ...",
    "params": ["acme"],
    "columns": [
        {"key": "devices.model", "ref": "devices.model", "role": "dimension", "type": "string", "label": "Device model"},
        {"key": "trips.count", "ref": "trips.count", "role": "measure", "type": "number", "label": "Trips"},
    ],
    "rows": [{"devices.model": "X1", "trips.count": 12}],
    "truncated": False,
    "explanation": "Trips by device model.",
    "chart": {"type": "bar", "x": "devices.model", "y": ["trips.count"]},
    "durationMs": 7,
}


class StubHandler(BaseHTTPRequestHandler):
    """Plays the noquery server: records requests and replies from a per-path script."""

    replies = {}
    requests = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        StubHandler.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        status, payload = StubHandler.replies[self.path]
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _reply
    do_POST = _reply


class ClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), StubHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        StubHandler.requests = []
        StubHandler.replies = {}
        self.client = NoqueryClient(self.url + "/", token="secret-token-123456")
        self.auth = {"userId": "u1", "orgId": "acme", "roles": ["analyst"]}

    def test_ask_sends_auth_and_token_and_parses_the_answer(self):
        StubHandler.replies["/v1/ask"] = (200, ANSWER)
        result = self.client.ask("Trips by model?", auth=self.auth, timezone="America/Toronto")

        sent = StubHandler.requests[0]
        self.assertEqual(sent["headers"]["Authorization"], "Bearer secret-token-123456")
        self.assertEqual(sent["body"], {"auth": self.auth, "question": "Trips by model?", "timezone": "America/Toronto"})
        self.assertTrue(result.ok)
        self.assertEqual(result.rows, [{"devices.model": "X1", "trips.count": 12}])
        self.assertEqual(result.labeled_rows(), [{"Device model": "X1", "Trips": 12}])
        self.assertEqual(result.columns[1].role, "measure")
        self.assertEqual(result.duration_ms, 7)

    def test_follow_up_history_round_trips(self):
        StubHandler.replies["/v1/ask"] = (200, ANSWER)
        first = self.client.ask("Trips by model?", auth=self.auth)
        self.client.ask("Only X1", auth=self.auth, history=[first.to_turn("Trips by model?")])
        history = StubHandler.requests[1]["body"]["history"]
        self.assertEqual(history, [{"question": "Trips by model?", "result": {"kind": "query", "ir": ANSWER["ir"]}}])

    def test_clarify_and_unanswerable(self):
        StubHandler.replies["/v1/ask"] = (200, {"type": "clarify", "question": "Which 'active'?", "options": ["a", "b"]})
        result = self.client.ask("active devices", auth=self.auth)
        self.assertEqual((result.type, result.question, result.options), ("clarify", "Which 'active'?", ["a", "b"]))
        self.assertFalse(result.ok)

        StubHandler.replies["/v1/ask"] = (200, {"type": "unanswerable", "reason": "No battery data."})
        self.assertEqual(self.client.ask("battery?", auth=self.auth).reason, "No battery data.")

    def test_run_and_validate(self):
        StubHandler.replies["/v1/run"] = (200, {k: v for k, v in ANSWER.items() if k != "type"})
        result = self.client.run({"version": 1, "measures": ["trips.count"]}, auth=self.auth)
        self.assertEqual(result.type, "answer")
        self.assertEqual(StubHandler.requests[0]["body"]["ir"], {"version": 1, "measures": ["trips.count"]})

        StubHandler.replies["/v1/validate"] = (200, {"ok": False, "errors": [{"code": "unknown_field", "message": "Unknown field"}]})
        check = self.client.validate({"version": 1, "measures": ["x.y"]}, auth=self.auth)
        self.assertFalse(check.ok)
        self.assertEqual(check.errors[0]["code"], "unknown_field")

    def test_errors_map_to_exception_types(self):
        cases = [
            (401, {"error": {"code": "unauthorized", "message": "Missing or invalid service token."}}, AuthenticationError),
            (422, {"error": {"code": "invalid_query", "message": "bad", "errors": [{"code": "unknown_field", "message": "x"}]}}, QueryValidationError),
            (501, {"error": {"code": "planner_not_configured", "message": "no planner"}}, PlannerUnavailableError),
            (500, {"error": {"code": "internal", "message": "Internal error."}}, NoqueryError),
        ]
        for status, payload, kind in cases:
            StubHandler.replies["/v1/run"] = (status, payload)
            with self.subTest(status=status):
                with self.assertRaises(kind) as caught:
                    self.client.run({"version": 1}, auth=self.auth)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.code, payload["error"]["code"])
        self.assertEqual(caught.exception.__class__, NoqueryError)

    def test_planner_failures_and_foreign_services(self):
        StubHandler.replies["/v1/ask"] = (502, {"error": {"code": "planner_failed", "message": "Claude rejected the API credentials (401).", "reason": "authentication", "retryable": False}})
        with self.assertRaises(PlannerFailedError) as caught:
            self.client.ask("q", auth=self.auth)
        self.assertEqual((caught.exception.status, caught.exception.reason, caught.exception.retryable), (502, "authentication", False))

        StubHandler.replies["/v1/run"] = (401, {"message": "Not ours"})
        with self.assertRaises(NoqueryError) as caught:
            self.client.run({"version": 1}, auth=self.auth)
        self.assertIs(type(caught.exception), NoqueryError)
        self.assertIn("Is the noquery sidecar running", caught.exception.message)

    def test_planner_refusals(self):
        StubHandler.replies["/v1/ask"] = (422, {"error": {"code": "planner_refused", "message": "The model declined to plan this question (cyber)."}})
        with self.assertRaises(PlannerRefusalError) as caught:
            self.client.ask("q", auth=self.auth)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.code, "planner_refused")
        self.assertEqual(caught.exception.message, "The model declined to plan this question (cyber).")
        self.assertIn("declined", str(caught.exception))

    def test_validation_error_carries_reasons(self):
        StubHandler.replies["/v1/run"] = (422, {"error": {"code": "invalid_query", "message": "bad", "errors": [{"code": "aggregate_only", "message": "can only be counted"}]}})
        with self.assertRaises(QueryValidationError) as caught:
            self.client.run({"version": 1}, auth=self.auth)
        self.assertEqual(caught.exception.errors[0]["code"], "aggregate_only")


if __name__ == "__main__":
    unittest.main()
