from noquery import Column, Result


def answer():
    return Result(
        type="answer",
        ir={"version": 1, "measures": ["trips.count"], "dimensions": ["devices.model"]},
        sql="SELECT 1",
        params=["acme"],
        columns=[
            Column(key="devices.model", ref="devices.model", role="dimension", type="string", label="Device model"),
            Column(key="trips.count", ref="trips.count", role="measure", type="number", label="Trips", format="integer"),
        ],
        rows=[{"devices.model": "X1", "trips.count": 12}],
        truncated=True,
        explanation="Trips by device model.",
        chart={"type": "bar", "x": "devices.model", "y": ["trips.count"]},
        duration_ms=7,
    )


def test_answer_round_trips_through_the_http_shape():
    result = answer()
    payload = result.to_dict()
    assert payload["durationMs"] == 7 and payload["truncated"] is True
    assert payload["columns"][0] == {"key": "devices.model", "ref": "devices.model", "role": "dimension", "type": "string", "label": "Device model"}
    assert payload["columns"][1]["format"] == "integer"
    assert Result.from_dict(payload) == result


def test_other_outcomes_round_trip():
    clarify = Result(type="clarify", question="Which?", options=["A", "B"])
    assert clarify.to_dict() == {"type": "clarify", "question": "Which?", "options": ["A", "B"]}
    assert Result.from_dict(clarify.to_dict()) == clarify
    unanswerable = Result(type="unanswerable", reason="No data.")
    assert unanswerable.to_dict() == {"type": "unanswerable", "reason": "No data."}
    invalid = Result(type="invalid", errors=[{"code": "unknown_field", "message": "x"}], ir={"version": 1})
    assert invalid.to_dict() == {"type": "invalid", "errors": [{"code": "unknown_field", "message": "x"}], "ir": {"version": 1}}
    assert Result(type="invalid").to_dict() == {"type": "invalid", "errors": []}


def test_helpers():
    result = answer()
    assert result.ok is True
    assert result.labeled_rows() == [{"Device model": "X1", "Trips": 12}]
    assert result.to_turn("Trips by model") == {"question": "Trips by model", "result": {"kind": "query", "ir": result.ir}}
    assert Result(type="clarify", question="Which?", options=["A"]).to_turn("q")["result"] == {"kind": "clarify", "question": "Which?", "options": ["A"]}
    assert Result(type="unanswerable", reason="No.").to_turn("q")["result"] == {"kind": "unanswerable", "reason": "No."}
