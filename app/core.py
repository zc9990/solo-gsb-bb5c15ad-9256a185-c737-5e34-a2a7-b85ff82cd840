"""直流配电树压降校核与新增容量计算核心算法。

计算流程：
1. 校验输入构成以 ``source`` 为根的连通树；
2. 自叶向根累计每段承载的全部下游电流；
3. 自根向叶计算各段压降与各节点电压；
4. 汇总超额定电流线段与欠压节点；
5. 对指定节点计算可新增的最大恒定负载电流。
"""

from __future__ import annotations

import math
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


class BaselineViolationError(ValueError):
    """基线工况已存在超额定或欠压，不能计算新增容量。"""

    def __init__(self, over_current_segments: list[dict], under_voltage_nodes: list[dict]):
        super().__init__("基线校核存在越限")
        self.over_current_segments = over_current_segments
        self.under_voltage_nodes = under_voltage_nodes


@dataclass
class Segment:
    id: str
    parent: str
    child: str
    resistance: float
    rated_current: float


@dataclass
class Network:
    """通过校验的配电树及其节点负载。"""

    segments: list[Segment]
    parent_of: dict[str, str]
    children_map: dict[str, list[Segment]]
    order: list[str]
    nodes: set[str]
    load_map: dict[str, float]
    min_voltage_override: dict[str, float]


@dataclass
class ElectricalState:
    """基线电气量（均保留未四舍五入的计算值）。"""

    node_total: dict[str, float]
    segment_current: dict[str, float]
    segment_drop: dict[str, float]
    node_voltage: dict[str, float]


def _round(value: float) -> float:
    return round(value, ROUND_DIGITS)


def _truncate_down(value: float) -> float:
    """向下截断到 6 位小数。容量增量不为负。"""
    scale = 10**ROUND_DIGITS
    return math.floor(value * scale) / scale


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


def _prepare_network(segments_in: Sequence, loads_in: Sequence) -> Network:
    """完成树、负载校验，并建立遍历与索引结构。"""
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

    return Network(
        segments=segments,
        parent_of=parent_of,
        children_map=children_map,
        order=order,
        nodes=nodes,
        load_map=load_map,
        min_voltage_override=min_voltage_override,
    )


def _compute_state(source_voltage: float, network: Network) -> ElectricalState:
    """计算基线工况下的线段电流、压降和节点电压。"""
    # 自叶向根累计每段承载的全部下游电流
    node_total = {node: network.load_map.get(node, 0.0) for node in network.nodes}
    segment_current: dict[str, float] = {}
    for node in reversed(network.order):
        for seg in network.children_map.get(node, []):
            segment_current[seg.id] = node_total[seg.child]
            node_total[node] += node_total[seg.child]

    # 自根向叶计算压降与节点电压
    node_voltage = {ROOT_NODE: source_voltage}
    segment_drop: dict[str, float] = {}
    for node in network.order:
        for seg in network.children_map.get(node, []):
            drop = segment_current[seg.id] * seg.resistance
            segment_drop[seg.id] = drop
            node_voltage[seg.child] = node_voltage[node] - drop

    return ElectricalState(
        node_total=node_total,
        segment_current=segment_current,
        segment_drop=segment_drop,
        node_voltage=node_voltage,
    )


def _build_evaluation(
    source_voltage: float,
    min_voltage: float,
    network: Network,
    state: ElectricalState,
) -> dict:
    """将电气计算结果整理为 /evaluate 响应。"""
    segment_results = []
    over_current_segments = []
    for seg in network.segments:  # 保持输入顺序
        current = state.segment_current[seg.id]
        is_over = current > seg.rated_current + EPS
        segment_results.append(
            {
                "segment_id": seg.id,
                "parent": seg.parent,
                "child": seg.child,
                "current": _round(current),
                "rated_current": seg.rated_current,
                "resistance": seg.resistance,
                "voltage_drop": _round(state.segment_drop[seg.id]),
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
    for node in network.order:  # BFS 顺序，根在前
        threshold = network.min_voltage_override.get(node, min_voltage)
        voltage = state.node_voltage[node]
        is_under = voltage < threshold - EPS
        node_results.append(
            {
                "node": node,
                "voltage": _round(voltage),
                "min_voltage": threshold,
                "load_current": _round(network.load_map.get(node, 0.0)),
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
        "total_source_current": _round(state.node_total[ROOT_NODE]),
        "segments": segment_results,
        "nodes": node_results,
        "over_current_segments": over_current_segments,
        "under_voltage_nodes": under_voltage_nodes,
    }


def evaluate(source_voltage: float, min_voltage: float, segments_in: Sequence, loads_in: Sequence) -> dict:
    """执行压降校核，返回可 JSON 序列化的结果字典。"""
    network = _prepare_network(segments_in, loads_in)
    state = _compute_state(source_voltage, network)
    return _build_evaluation(source_voltage, min_voltage, network, state)


def _root_path(node: str, parent_of: dict[str, str]) -> list[str]:
    """返回自 source 到指定节点的路径（含两端）。"""
    path: list[str] = []
    current = node
    while current != ROOT_NODE:
        path.append(current)
        current = parent_of[current]
    path.reverse()
    return [ROOT_NODE, *path]


def _candidate_from_margin(margin: float, coefficient: float) -> Optional[float]:
    """把电流 / 电压裕度转换为允许新增电流。

    ``coefficient`` 为线段电流增量系数 1，或节点与目标节点根路径的公共电阻和。
    系数为 0 时，新增负载不会改变该约束，因此不构成容量上限。
    """
    if coefficient == 0:
        return None

    candidate = margin / coefficient
    # 与基线校核相同的容差：极微小浮点误差视为恰好触限。
    if candidate < -EPS:
        return candidate
    if candidate < 0:
        return 0.0
    return candidate


def capacity(
    source_voltage: float,
    min_voltage: float,
    target_node: str,
    segments_in: Sequence,
    loads_in: Sequence,
) -> dict:
    """计算指定节点可新增的最大恒定负载电流及触限约束。"""
    network = _prepare_network(segments_in, loads_in)
    state = _compute_state(source_voltage, network)
    baseline = _build_evaluation(source_voltage, min_voltage, network, state)

    if not baseline["ok"]:
        raise BaselineViolationError(
            baseline["over_current_segments"],
            baseline["under_voltage_nodes"],
        )

    if target_node not in network.nodes:
        raise InputValidationError(
            "目标节点无效",
            [f"target_node '{target_node}' 不存在于树中"],
        )
    if target_node == ROOT_NODE:
        raise InputValidationError(
            "目标节点无效",
            [f"target_node 不能是根节点 '{ROOT_NODE}'"],
        )

    target_path = _root_path(target_node, network.parent_of)
    segment_by_child = {seg.child: seg for seg in network.segments}
    target_path_segments = [segment_by_child[node] for node in target_path if node != ROOT_NODE]
    target_path_set = set(target_path)

    # (允许新增电流, 类型, 标识)。
    constraints: list[tuple[float, str, str]] = []

    # 仅 source -> target 路径上的线段会增加电流。
    for seg in target_path_segments:
        margin = seg.rated_current - state.segment_current[seg.id]
        candidate = _candidate_from_margin(margin, 1.0)
        if candidate is not None:
            constraints.append((candidate, "segment", seg.id))

    # 任一节点的新增压降 = 该节点与目标节点根路径公共线段电阻之和 × 新增电流。
    for node in network.order:
        node_path = _root_path(node, network.parent_of)
        common_resistance = 0.0
        for node_ancestor, target_ancestor in zip(node_path, target_path):
            if node_ancestor != target_ancestor:
                break
            if node_ancestor != ROOT_NODE:
                common_resistance += segment_by_child[node_ancestor].resistance

        if common_resistance == 0:
            continue

        threshold = network.min_voltage_override.get(node, min_voltage)
        margin = state.node_voltage[node] - threshold
        candidate = _candidate_from_margin(margin, common_resistance)
        if candidate is not None:
            constraints.append((candidate, "node", node))

    # 目标是非 source 节点，路径上至少有一条额定值有限的线段，因此必有约束。
    min_delta = min(candidate for candidate, _, _ in constraints)
    limiting_segment_ids = [
        identifier
        for candidate, kind, identifier in constraints
        if kind == "segment" and abs(candidate - min_delta) <= EPS
    ]
    limiting_node_ids = [
        identifier
        for candidate, kind, identifier in constraints
        if kind == "node" and abs(candidate - min_delta) <= EPS
    ]

    return {
        "target_node": target_node,
        "max_added_current": _truncate_down(min_delta),
        "limiting_segment_ids": limiting_segment_ids,
        "limiting_node_ids": limiting_node_ids,
    }
