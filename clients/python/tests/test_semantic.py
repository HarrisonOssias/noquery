import re

import pytest

from noquery import check_semantics, draft_semantics, humanize, merge_semantics

GRAPH = {
    "tables": [
        {
            "name": "customers",
            "columns": [
                {"name": "id", "kind": "integer", "dbType": "integer", "nullable": False, "primaryKey": True},
                {"name": "full_name", "kind": "string", "dbType": "text", "nullable": False},
                {"name": "billing_ref", "kind": "string", "dbType": "text", "nullable": True},
                {"name": "lifetime_value", "kind": "number", "dbType": "numeric", "nullable": True},
                {"name": "signed_up_on", "kind": "date", "dbType": "date", "nullable": False},
            ],
            "foreignKeys": [],
        }
    ]
}


def test_humanize():
    assert humanize("started_at") == "Started at"
    assert humanize("deviceModel") == "Device model"
    assert humanize("__weird--name__") == "Weird name"
    assert humanize("") == ""


def test_draft_hides_sensitive_columns_and_builds_measures():
    entity = draft_semantics(GRAPH)["entities"]["customers"]
    assert entity["dimensions"]["full_name"]["policy"] == "hidden"
    assert entity["dimensions"]["id"] == {"type": "number", "column": "id", "policy": "aggregate_only", "description": "Primary key"}
    assert entity["dimensions"]["signed_up_on"]["timeStorage"] == "local"
    assert entity["defaultTime"] == "signed_up_on"
    assert entity["measures"]["count"] == {"agg": "count", "label": "Number of customers", "format": "integer"}
    assert entity["measures"]["total_lifetime_value"] == {"agg": "sum", "column": "lifetime_value", "label": "Total Lifetime value"}


def test_draft_accepts_custom_sensitive_patterns_as_strings_or_compiled():
    by_string = draft_semantics(GRAPH, sensitive_patterns=["BILLING"])["entities"]["customers"]
    assert by_string["dimensions"]["billing_ref"]["policy"] == "hidden"
    assert "policy" not in by_string["dimensions"]["full_name"]
    by_regex = draft_semantics(GRAPH, sensitive_patterns=[re.compile("^lifetime")])["entities"]["customers"]
    assert by_regex["dimensions"]["lifetime_value"]["policy"] == "hidden"
    assert "total_lifetime_value" not in by_regex["measures"]


def test_draft_does_not_mutate_the_graph():
    before = repr(GRAPH)
    draft_semantics(GRAPH)
    assert repr(GRAPH) == before


def test_merge_does_not_mutate_its_inputs(fleet):
    before = repr(fleet)
    overlay = {"entities": {"trips": {"label": "Journey", "dimensions": {"note": None}}, "patients": None}}
    merged = merge_semantics(fleet, overlay)
    assert repr(fleet) == before
    assert merged["entities"]["trips"]["label"] == "Journey"
    assert "note" not in merged["entities"]["trips"]["dimensions"]
    assert "patients" not in merged["entities"]
    assert "note" in fleet["entities"]["trips"]["dimensions"]


def test_merge_patches_existing_fields_and_adds_new_ones(fleet):
    merged = merge_semantics(
        fleet,
        {
            "entities": {
                "devices": {
                    "dimensions": {"serial": {"policy": "visible"}, "firmware": {"type": "string", "column": "firmware"}},
                    "measures": {"count": {"label": "Units"}, "retired_count": {"sql": "count(*) FILTER (WHERE ${TABLE}.status = 'retired')"}},
                }
            }
        },
    )
    devices = merged["entities"]["devices"]
    assert devices["dimensions"]["serial"] == {"type": "string", "column": "serial", "policy": "visible"}
    assert devices["dimensions"]["firmware"] == {"type": "string", "column": "firmware"}
    assert devices["measures"]["count"] == {"agg": "count", "label": "Units"}
    assert "retired_count" in devices["measures"]
    assert check_semantics(merged) == []


def test_merge_rejects_incomplete_additions(fleet):
    with pytest.raises(ValueError, match='Overlay adds entity "ghosts" without a table.'):
        merge_semantics(fleet, {"entities": {"ghosts": {"label": "Ghosts"}}})
    with pytest.raises(ValueError, match='New dimension in "trips" needs a type.'):
        merge_semantics(fleet, {"entities": {"trips": {"dimensions": {"colour": {"column": "colour"}}}}})


def test_merge_drops_joins_to_removed_entities_and_honours_remove_joins(fleet):
    without_devices = merge_semantics(fleet, {"entities": {"devices": None}})
    assert without_devices["joins"] == []
    removed = merge_semantics(fleet, {"removeJoins": [{"from": "trips", "to": "devices"}]})
    assert [(j["from"], j["to"]) for j in removed["joins"]] == [("devices", "organizations")]
    replaced = merge_semantics(fleet, {"joins": [{"from": "trips", "to": "devices", "on": [["device_id", "id"]], "relationship": "one_to_one"}]})
    assert [j["relationship"] for j in replaced["joins"] if j["from"] == "trips"] == ["one_to_one"]


def test_check_semantics_accepts_the_fleet_model(fleet):
    assert check_semantics(fleet) == []


def test_check_semantics_reports_every_problem():
    model = {
        "entities": {
            "bad name": {
                "table": "",
                "defaultTime": "label",
                "dimensions": {
                    "label": {"type": "string", "column": "label"},
                    "nothing": {"type": "string"},
                    "9lives": {"type": "string", "column": "x"},
                    "shape": {"type": "geometry", "column": "shape"},
                },
                "measures": {
                    "label": {"agg": "count"},
                    "empty": {},
                    "total": {"agg": "sum"},
                    "median": {"agg": "median", "column": "x"},
                },
            }
        },
        "joins": [{"from": "bad name", "to": "missing", "on": [], "relationship": "sideways"}],
    }
    messages = {(i["path"], i["message"]) for i in check_semantics(model)}
    assert ("entities.bad name", "Entity names must be identifiers.") in messages
    assert ("entities.bad name", "Entity has no table.") in messages
    assert ("entities.bad name.dimensions.nothing", "Dimension needs a column or sql.") in messages
    assert ("entities.bad name.dimensions.9lives", "Field names must be identifiers.") in messages
    assert ("entities.bad name.dimensions.shape", "Dimension type must be one of string, number, boolean, time.") in messages
    assert ("entities.bad name.measures.label", "A measure and a dimension share this name.") in messages
    assert ("entities.bad name.measures.empty", "Measure needs an agg, or sql that is an aggregate.") in messages
    assert ("entities.bad name.measures.total", '"sum" needs a column or sql.') in messages
    assert ("entities.bad name.measures.median", 'Unknown agg "median".') in messages
    assert ("entities.bad name.defaultTime", "defaultTime must name a time dimension.") in messages
    assert ("joins[0]", 'Unknown entity "missing".') in messages
    assert ("joins[0]", "Join needs at least one column pair.") in messages
    assert ("joins[0]", "Relationship must be one of many_to_one, one_to_one, one_to_many.") in messages


def test_check_semantics_rejects_names_with_a_trailing_newline():
    model = {"entities": {"trips\n": {"table": "trips", "dimensions": {}, "measures": {"count": {"agg": "count"}}}}}
    assert check_semantics(model)[0]["message"] == "Entity names must be identifiers."
