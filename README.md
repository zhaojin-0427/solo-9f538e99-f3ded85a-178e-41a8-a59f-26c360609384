# OSM 洪水分析水域面 API

按 OSM 关系号提交**逐次递增修订**的完整节点经纬度与无序 way（含 `outer` /
`inner` 角色），服务按节点引用拼接 way（允许线段反向），生成一个外环、至多
一个内环的 GeoJSON Polygon，并管理"当前供数面"的状态机。

技术栈：**FastAPI + Shapely + SQLite（WAL 模式）**，可 `docker compose` 启动。

## 快速启动

```bash
docker compose up --build
# 打开交互文档（可直接上传、取面）
open http://localhost:8000/docs
```

本地无 Docker 时：

```bash
pip install -r requirements.txt
FLOOD_DB_PATH=./flood.db uvicorn app.main:app --reload
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/relations/{relation_id}/revisions` | 上传一个完整修订（修订号必须严格递增） |
| GET  | `/relations/{relation_id}/revisions/{revision}` | 按修订号读取**原始输入 + 几何 + 状态** |
| GET  | `/relations/{relation_id}/revisions` | 列出该关系全部修订号与状态 |
| GET  | `/relations/{relation_id}/current` | 取当前供数面（最新有效修订） |
| GET  | `/health` | 健康检查 |

### 上传请求体

```json
{
  "revision": 1,
  "nodes": [
    {"id": 1, "lat": 0.0, "lon": 0.0},
    {"id": 2, "lat": 0.0, "lon": 1.0},
    {"id": 3, "lat": 1.0, "lon": 1.0},
    {"id": 4, "lat": 1.0, "lon": 0.0}
  ],
  "ways": [
    {"id": 10, "role": "outer", "node_refs": [1, 2, 3, 4, 1]}
  ]
}
```

约束：每次提交为该修订的**完整快照**；最多 **8 条 way、32 个节点**；way
无序，拼接时可反向遍历；`node_refs` 闭合方式沿用 OSM 惯例（首尾节点相同）。

## 校验与状态机

以下情况修订标记为 `invalid`，**原始输入完整保留**，响应中返回错误码及相关
way id，当前面**停止供数**（`/current` 返回 409）：

- `missing_nodes`：way 引用了未提交的节点
- `broken_ring`：单 way 未闭合、way 间断环、外环拆成多个连通分量
- `fork`：三条以上 way 端点汇聚，或 way 端点落在另一条 way 内部（T 型分叉）
- `self_intersection`：环自交 / 节点重复访问（8 字形等）
- `inner_ring_violation`：内环触碰外环边界、越过外环或位于外环之外
- `missing_outer`：没有 `outer` 角色的 way

停止供数期间，**上一个有效面仍可按修订号读取**，响应里的
`last_valid_revision` 指向它；后续出现有效修订后 `/current` 自动恢复供数。

输入约束违例（超 8/32 上限、角色非法、修订号非递增等）返回 422 / 409，
**不落库**。

## 一致性、原子性与持久化

- SQLite 启用 **WAL + `synchronous=FULL`**：提交即 fsync，重启后数据可查；
  读不阻塞写，读者只能看到完整提交的修订。
- 每次写入在单个 `BEGIN IMMEDIATE` 事务内完成"递增检查 + 插入"，进程内
  再以锁串行化；存储失败整体回滚，**不会留下半份修订**。
- 原始输入、几何、状态同事务写入，因此并发上传与读取看到的同一修订三者
  必然一致。
- 数据库位于挂载卷 `/data/flood.db`（环境变量 `FLOOD_DB_PATH` 可改）。

## 自测

```bash
pip install -r requirements.txt
pip install pytest httpx
pytest -q
```

覆盖：单/多 way 拼接与反向、内环挖洞、缺节点、断环、分叉、T 型分叉、自交、
内环触碰/越界、状态机恢复、修订号冲突、上下限校验、重启恢复、事务原子性、
并发上传/读取、`/docs` 可用性。
