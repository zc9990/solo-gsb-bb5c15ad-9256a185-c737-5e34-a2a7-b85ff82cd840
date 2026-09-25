"""直流配电树压降校核核心算法。

计算流程：
1. 校验输入构成以 ``source`` 为根的连通树；
2. 自叶向根累计每段承载的全部下游电流；
3. 自根向叶计算各段压降与各节点电压；
4. 汇总超额定电流线段与欠压节点。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence

ROOT_NODE = "source"
EPS = 1e-9
ROUND_DIGITS = 6


class InputValidationError(ValueError):
    """输入未通过业务校验（无效树、负载引用未知节点等）。"""

    def __init__(self, message: str, details: Optional[list[str]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass
class Segment:
    id: str
    parent: str
    child: str
    resistance: float
    rated_current: float


def _round(value: float) -> float:
    return round(value, ROUND_DIGITS)


def _validate_and_build_tree(segments_in: Sequence) -> tuple[list[Segment], dict[str, str]]:
    """校验树结构，返回线段列表与 child -> parent 映射。

    校验规则：
    - 线段 id 唯一（缺省自动生成 seg-<序号>）；
    - 不存在自环（父节点与子节点相同）；
    - 每个节点至多有一个父节点；
    - 根节点 source 不能作为子节点；
    - 所有父节点必须是 source 或某个子节点（连通性）；
    - 不存在环路。
    """
    segments: list[Segment] = []
    for index, s in enumerate(segments_in):
        seg_id = s.id or f"seg-{index + 1}"
        segments.append(
            Segment(
                id=seg_id,
                parent=s.parent,
                child=s.child,
                resistance=s.resistance,
                rated_current=s.rated_current,
            )
        )

    errors: list[str] = []

    seen_ids: set[str] = set()
    for seg in segments:
        if seg.id in seen_ids:
            errors.append(f"线段 id 重复: '{seg.id}'")
        seen_ids.add(seg.id)

    for seg in segments:
        if seg.parent == seg.child:
            errors.append(f"线段 '{seg.id}' 的父节点与子节点相同（'{seg.parent}'），存在自环")

    parent_of: dict[str, str] = {}
    for seg in segments:
        if seg.child in parent_of:
            errors.append(
                f"节点 '{seg.child}' 存在多个父节点（'{parent_of[seg.child]}' 与 '{seg.parent}'）"
            )
        else:
            parent_of[seg.child] = seg.parent

    if ROOT_NODE in parent_of:
        errors.append(f"根节点 '{ROOT_NODE}' 不能作为子节点")

    child_nodes = set(parent_of)
    for seg in segments:
        if seg.parent != ROOT_NODE and seg.parent not in child_nodes:
            errors.append(f"线段 '{seg.id}' 的父节点 '{seg.parent}' 未在树中定义，树不连通")

    if errors:
        raise InputValidationError(f"输入未构成以 '{ROOT_NODE}' 为根的连通树", errors)

    # 环路检测：沿父指针上溯必须到达根
    for seg in segments:
        visited: set[str] = set()
        node = seg.child
        while node != ROOT_NODE:
            if node in visited:
                raise InputValidationError(
                    f"输入未构成以 '{ROOT_NODE}' 为根的连通树",
                    [f"检测到环路，涉及节点 '{node}'"],
                )
            visited.add(node)
            node = parent_of[node]

    return segments, parent_of


def _aggregate_loads(loads_in: Sequence, known_nodes: set[str]) -> tuple[dict[str, float], dict[str, float]]:
    """汇总各节点负载电流（同节点多条负载累加）及节点级最低允许电压。"""
    errors: list[str] = []
    load_map: dict[str, float] = {}
    min_voltage_map: dict[str, float] = {}

    for load in loads_in:
        if load.node not in known_nodes:
            errors.append(f"负载节点 '{load.node}' 不存在于树中")
            continue
        load_map[load.node] = load_map.get(load.node, 0.0) + load.current
        if load.min_voltage is not None:
            previous = min_voltage_map.get(load.node)
            if previous is not None and abs(previous - load.min_voltage) > EPS:
                errors.append(f"节点 '{load.node}' 的最低允许电压被重复定义且不一致")
            else:
                min_voltage_map[load.node] = load.min_voltage

    if errors:
        raise InputValidationError("负载定义无效", errors)

    return load_map, min_voltage_map


def evaluate(source_voltage: float, min_voltage: float, segments_in: Sequence, loads_in: Sequence) -> dict:
    """执行压降校核，返回可 JSON 序列化的结果字典。"""
    segments, parent_of = _validate_and_build_tree(segments_in)

    children_map: dict[str, list[Segment]] = {}
    for seg in segments:
        children_map.setdefault(seg.parent, []).append(seg)

    nodes = set(parent_of) | {ROOT_NODE}
    load_map, min_voltage_override = _aggregate_loads(loads_in, nodes)

    # 自根向叶 BFS 排序（父节点一定先于子节点出现）
    order = [ROOT_NODE]
    queue: deque[str] = deque([ROOT_NODE])
    while queue:
        node = queue.popleft()
        for seg in children_map.get(node, []):
            order.append(seg.child)
            queue.append(seg.child)

    # 自叶向根累计每段承载的全部下游电流
    node_total = {node: load_map.get(node, 0.0) for node in nodes}
    seg_current: dict[str, float] = {}
    for node in reversed(order):
        for seg in children_map.get(node, []):
            seg_current[seg.id] = node_total[seg.child]
            node_total[node] += node_total[seg.child]

    # 自根向叶计算压降与节点电压
    node_voltage = {ROOT_NODE: source_voltage}
    seg_drop: dict[str, float] = {}
    for node in order:
        for seg in children_map.get(node, []):
            drop = seg_current[seg.id] * seg.resistance
            seg_drop[seg.id] = drop
            node_voltage[seg.child] = node_voltage[node] - drop

    segment_results = []
    over_current_segments = []
    for seg in segments:  # 保持输入顺序
        current = seg_current[seg.id]
        is_over = current > seg.rated_current + EPS
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(current),
                "rated_current": seg.rated_current,
                "resistance": seg.resistance,
                "voltage_drop": _round(seg_drop[seg.id]),
                "over_rated": is_over,
            }
        )
        if is_over:
            over_current_segments.append(
                {
                    "segment_id": seg.id,
                    "parent": seg.parent,
                    "child": seg.child,
                    "current": _round(current),
                    "rated_current": seg.rated_current,
                    "excess": _round(current - seg.rated_current),
                }
            )

    node_results = []
    under_voltage_nodes = []
    for node in order:  # BFS 顺序，根在前
        threshold = min_voltage_override.get(node, min_voltage)
        voltage = node_voltage[node]
        is_under = voltage < threshold - EPS
        node_results.append(
            {
                "node": node,
                "voltage": _round(voltage),
                "min_voltage": threshold,
                "load_current": _round(load_map.get(node, 0.0)),
                "under_voltage": is_under,
            }
        )
        if is_under:
            under_voltage_nodes.append(
                {
                    "node": node,
                    "voltage": _round(voltage),
                    "min_voltage": threshold,
                    "deficit": _round(threshold - voltage),
                }
            )

    return {
        "ok": not over_current_segments and not under_voltage_nodes,
        "source_voltage": source_voltage,
        "min_voltage": min_voltage,
        "total_source_current": _round(node_total[ROOT_NODE]),
        "segments": segment_results,
        "nodes": node_results,
        "over_current_segments": over_current_segments,
        "under_voltage_nodes": under_voltage_nodes,
    }
