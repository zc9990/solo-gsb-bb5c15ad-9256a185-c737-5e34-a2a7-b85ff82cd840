"""POST /capacity 的端到端测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def base_payload(**overrides):
    """与 /evaluate 测试相同的三层小树。"""
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 40.0,
        "target_node": "b",
        "segments": [
            {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1, "rated_current": 20.0},
            {"id": "s2", "parent": "a", "child": "b", "resistance": 0.2, "rated_current": 10.0},
            {"id": "s3", "parent": "a", "child": "c", "resistance": 0.05, "rated_current": 15.0},
        ],
        "loads": [
            {"node": "a", "current": 2.0},
            {"node": "b", "current": 5.0},
            {"node": "c", "current": 8.0},
        ],
    }
    payload.update(overrides)
    return payload


def test_segment_capacity_on_target_path_with_tie():
    # s1、s2 均剩余 5A；目标路径外的 s3（剩余 7A）不应限制。
    resp = client.post("/capacity", json=base_payload())
    assert resp.status_code == 200
    data = resp.json()

    assert data["target_node"] == "b"
    assert data["max_added_current"] == 5.0
    assert data["limiting_segment_ids"] == ["s1", "s2"]
    assert data["limiting_node_ids"] == []


def test_voltage_limit_is_truncated_to_six_decimals():
    payload = base_payload()
    for segment in payload["segments"]:
        segment["rated_current"] = 100.0

    # b：(45.5 - 40) / (0.1 + 0.2) = 18.333333...
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["max_added_current"] == 18.333333
    assert data["limiting_segment_ids"] == []
    assert data["limiting_node_ids"] == ["b"]


def test_sibling_branch_voltage_is_included():
    payload = base_payload(target_node="b")
    for segment in payload["segments"]:
        segment["rated_current"] = 100.0
    # 放宽目标 b 的阈值后，兄弟节点 c 经公共段 s1 新增压降，最先达到 40V：
    # (46.1 - 40) / 0.1 = 61
    payload["loads"][1]["min_voltage"] = 20.0

    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["max_added_current"] == 61.0
    assert data["limiting_node_ids"] == ["c"]


def test_only_target_path_segments_add_current():
    # s2 当前 5A、额定 5A，但目标为 c；s2 不在 source -> c 路径上。
    payload = base_payload(target_node="c")
    payload["segments"][1]["rated_current"] = 5.0
    for segment in payload["segments"]:
        if segment["id"] != "s2":
            segment["rated_current"] = 100.0

    # s3：15 - 8 = 7；节点 c：(46.1 - 40) / (0.1 + 0.05) = 40.666...
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["max_added_current"] == 7.0
    assert data["limiting_segment_ids"] == ["s3"]
    assert data["limiting_node_ids"] == []


def test_exact_rated_current_gives_zero_increment():
    payload = base_payload()
    payload["segments"][1]["rated_current"] = 5.0  # s2 恰好在额定值

    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["max_added_current"] == 0.0
    assert data["limiting_segment_ids"] == ["s2"]


def test_zero_resistance_node_exactly_at_limit_does_not_constrain():
    payload = {
        "source_voltage": 48.0,
        "min_voltage": 48.0,
        "target_node": "t",
        "segments": [
            {"id": "zero", "parent": "source", "child": "t", "resistance": 0.0, "rated_current": 20.0},
        ],
        "loads": [],
    }

    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["max_added_current"] == 20.0
    assert data["limiting_segment_ids"] == ["zero"]
    assert data["limiting_node_ids"] == []


def test_baseline_over_current_returns_400_with_locations():
    payload = base_payload()
    payload["segments"][0]["rated_current"] = 14.0

    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "baseline_violation"
    assert any(
        item.get("type") == "over_current_segment" and item.get("segment_id") == "s1"
        for item in error["details"]
    )


def test_baseline_under_voltage_returns_400_with_locations():
    payload = base_payload()
    payload["segments"][0]["resistance"] = 1.0

    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "baseline_violation"
    assert any(
        item.get("type") == "under_voltage_node" and item.get("node") == "a"
        for item in error["details"]
    )


def test_unknown_target_node_rejected():
    resp = client.post("/capacity", json=base_payload(target_node="ghost"))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("ghost" in item["message"] for item in error["details"])


def test_source_target_node_rejected():
    resp = client.post("/capacity", json=base_payload(target_node="source"))
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_input"
    assert any("source" in item["message"] for item in error["details"])


def test_missing_target_node_rejected():
    payload = base_payload()
    del payload["target_node"]
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_invalid_tree_still_uses_existing_error():
    payload = base_payload()
    payload["segments"].append(
        {"id": "bad", "parent": "x", "child": "y", "resistance": 0.1, "rated_current": 10.0}
    )
    resp = client.post("/capacity", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_input"
