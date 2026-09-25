# 直流配电树压降校核 API

基于 FastAPI 的直流配电树校核服务：输入以 `source` 为根的配电树（电源电压、线段电阻与额定电流、各节点恒定负载、最低允许电压），自叶向根累计每段承载的全部下游电流，再自根向叶计算压降与节点电压，返回各段电流、各节点电压以及超额定电流 / 欠压位置；并可计算指定节点可新增的最大恒定负载电流。

## 快速启动（单条命令）

```bash
docker compose up --build
```

容器启动时会自动安装依赖（`pip install -r requirements.txt`）并启动服务。

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8000` | 宿主机映射端口，如 `PORT=9000 docker compose up --build` |

## API 地址

- 服务地址：`http://localhost:8000`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`GET http://localhost:8000/health`
- 校核接口：`POST http://localhost:8000/evaluate`
- 新增容量接口：`POST http://localhost:8000/capacity`

## 请求格式

`POST /evaluate`，Content-Type: `application/json`

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `source_voltage` | number | > 0 | 电源电压 (V) |
| `min_voltage` | number | ≥ 0 | 全局最低允许电压 (V) |
| `segments` | array | 至少 1 条 | 线段列表，须构成以 `source` 为根的连通树 |
| `segments[].id` | string | 可选，全局唯一 | 线段标识，缺省自动生成 `seg-<序号>` |
| `segments[].parent` | string | 必填 | 父节点名称，根节点固定为 `source` |
| `segments[].child` | string | 必填 | 子节点名称 |
| `segments[].resistance` | number | ≥ 0 | 线段电阻 (Ω) |
| `segments[].rated_current` | number | > 0 | 线段额定电流 (A) |
| `loads` | array | 可空 | 各节点恒定负载；同一节点多条负载自动累加 |
| `loads[].node` | string | 必填 | 节点名称（须存在于树中，可为 `source`） |
| `loads[].current` | number | ≥ 0 | 恒定负载电流 (A) |
| `loads[].min_voltage` | number | 可选，≥ 0 | 该节点最低允许电压，覆盖全局值 |

### 请求示例

```bash
curl -X POST http://localhost:8000/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ]
  }'
```

### 响应示例（200）

```json
{
  "ok": true,
  "source_voltage": 48.0,
  "min_voltage": 40.0,
  "total_source_current": 15.0,
  "segments": [
    {"segment_id": "s1", "parent": "source", "child": "a", "current": 15.0, "rated_current": 20.0, "resistance": 0.1, "voltage_drop": 1.5, "over_rated": false},
    {"segment_id": "s2", "parent": "a", "child": "b", "current": 5.0, "rated_current": 10.0, "resistance": 0.2, "voltage_drop": 1.0, "over_rated": false},
    {"segment_id": "s3", "parent": "a", "child": "c", "current": 8.0, "rated_current": 15.0, "resistance": 0.05, "voltage_drop": 0.4, "over_rated": false}
  ],
  "nodes": [
    {"node": "source", "voltage": 48.0, "min_voltage": 40.0, "load_current": 0.0, "under_voltage": false},
    {"node": "a", "voltage": 46.5, "min_voltage": 40.0, "load_current": 2.0, "under_voltage": false},
    {"node": "b", "voltage": 45.5, "min_voltage": 40.0, "load_current": 5.0, "under_voltage": false},
    {"node": "c", "voltage": 46.1, "min_voltage": 40.0, "load_current": 8.0, "under_voltage": false}
  ],
  "over_current_segments": [],
  "under_voltage_nodes": []
}
```

- `segments[].current`：该段承载的全部下游电流（自叶向根累计）。
- `nodes[].voltage`：自根向叶逐段扣除 `电流 × 电阻` 压降后的节点电压。
- `over_current_segments`：超额定电流的线段（含 `excess` 超出量）。
- `under_voltage_nodes`：欠压节点（含 `deficit` 欠压差值）。
- `ok`：无超额定且无欠压时为 `true`。

## 新增容量接口

`POST /capacity`，Content-Type: `application/json`

请求体与 `/evaluate` 相同，额外增加一个字段：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `target_node` | string | 必填 | 目标节点，须为已有的非 `source` 节点 |

计算规则：

1. 先按 `/evaluate` 的算法计算基线工况；若基线已存在超额定或欠压，返回 `400` 并在 `error.details` 中列出越限位置。
2. 在 `target_node` 上叠加恒定负载电流 `ΔI` 后，`source -> target_node` 路径上的线段电流增加 `ΔI`。
3. 任一节点的新增压降 = 该节点与目标节点两条根路径的公共线段电阻之和 × `ΔI`（兄弟支路经公共段同样受影响）。
4. 最大 `ΔI` 取所有线段额定余量与所有节点电压余量约束的最小值，结果向下截断至 6 位小数；恰好触限视为达到上限，零电阻线段 / 零公共电阻节点不构成电压约束。

### 请求示例

```bash
curl -X POST http://localhost:8000/capacity \
  -H "Content-Type: application/json" \
  -d '{
    "source_voltage": 48.0,
    "min_voltage": 40.0,
    "target_node": "b",
    "segments": [
      {"id": "s1", "parent": "source", "child": "a", "resistance": 0.1,  "rated_current": 20.0},
      {"id": "s2", "parent": "a",      "child": "b", "resistance": 0.2,  "rated_current": 10.0},
      {"id": "s3", "parent": "a",      "child": "c", "resistance": 0.05, "rated_current": 15.0}
    ],
    "loads": [
      {"node": "a", "current": 2.0},
      {"node": "b", "current": 5.0},
      {"node": "c", "current": 8.0}
    ]
  }'
```

### 响应示例（200）

```json
{
  "target_node": "b",
  "max_added_current": 5.0,
  "limiting_segment_ids": ["s1", "s2"],
  "limiting_node_ids": []
}
```

- `max_added_current`：目标节点可新增的最大恒定负载电流 (A)，向下截断至 6 位小数。
- `limiting_segment_ids`：达到额定电流上限的线段标识（可能多个并列）。
- `limiting_node_ids`：达到最低允许电压的节点标识（可能多个并列）。

### 基线越限响应（400）

```json
{
  "error": {
    "code": "baseline_violation",
    "message": "基线校核存在越限，无法计算新增容量",
    "details": [
      {"type": "over_current_segment", "segment_id": "s1", "parent": "source", "child": "a", "current": 15.0, "rated_current": 14.0, "excess": 1.0},
      {"type": "under_voltage_node", "node": "b", "voltage": 32.0, "min_voltage": 40.0, "deficit": 8.0}
    ]
  }
}
```

## 错误说明

所有错误均返回统一结构：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数校验失败",
    "details": [{"field": "source_voltage", "message": "电源电压必须为正数（> 0）"}]
  }
}
```

| 状态码 | code | 触发场景 |
| --- | --- | --- |
| `422` | `validation_error` | 非正电源电压、负电阻、负负载电流、非正额定电流、字段缺失或类型错误等 |
| `400` | `invalid_input` | 无效树（不连通、存在环路、节点多父节点、`source` 作为子节点、线段 id 重复）、负载引用不存在的节点，或 `/capacity` 的 `target_node` 不是已有非 `source` 节点 |
| `400` | `baseline_violation` | 仅 `/capacity`：基线工况已存在超额定电流或欠压节点，`details` 列出越限位置 |

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --reload --port 8000
```

## 项目结构

```
app/
  main.py    # FastAPI 应用与错误处理
  core.py    # 树校验 + 电流累计 + 压降计算
  models.py  # 请求 / 响应模型
tests/
  test_evaluate.py
  test_capacity.py
Dockerfile
docker-compose.yml
requirements.txt
```
