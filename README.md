# 接口版本兼容与自描述契约

本服务演示一套可落地的接口版本治理流程：多版本并存、废弃与下线管理、
异常归一化、由代码自动生成的确定性契约文档。

## 版本策略

版本以路径段标识：`/vN/...`。每个版本在 `app/versioning.py` 的
`VersionRegistry` 中登记生命周期元数据：

| 状态 | 含义 | 对外行为 |
| --- | --- | --- |
| `active` | 当前推荐版本 | 正常服务 |
| `deprecated` | 已废弃，仍可用 | 正常服务，响应带 `Deprecation` / `Sunset` / `Link: rel="successor-version"` 提示头 |
| `sunset` | 已下线 | 一律拒绝：`410` + `VERSION_SUNSET`，响应体带 `sunset_at` 与 `successor` |

规则：

- 到达 `sunset_at` 当天即视为下线（`VersionRegistry.is_sunset`）。
- 访问**未注册**的版本返回 `400` + `VERSION_UNKNOWN`，与路由不存在（404）明确区分。
- 字段级废弃在 `VersionInfo.deprecated_fields` 登记，并在模型字段上标注
  `deprecated=True`，二者都会进入契约文档。
- 当前实例：v0 已下线，v1 已废弃（2026-12-31 下线，后继 v2），v2 活跃。

## 兼容范围

- 旧版本在线期间，其请求/响应模型保持冻结：旧调用方发送的字段继续被接受，
  返回的字段集不增不减（见 `tests/test_version_compat.py`）。
- 新增字段、调整校验只发生在新版本模型中（`app/schemas/v2.py`）。

## 错误归一化

所有错误响应统一为：

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "request validation failed",
    "details": [{"loc": ["body", "name"], "msg": "...", "type": "..."}],
    "api_version": "v2"
  }
}
```

错误码（`app/errors.py: ErrorCode`）：`VALIDATION_FAILED`(422)、`NOT_FOUND`(404)、
`BUSINESS_RULE_VIOLATION`(409/422)、`VERSION_UNKNOWN`(400)、`VERSION_SUNSET`(410)、
`INTERNAL_ERROR`(500)。

- 请求校验失败、业务异常、版本拒绝、未捕获异常都收敛到该结构。
- 未捕获异常只记录服务端日志，对外不泄露异常类型、堆栈或内部细节。

## 自描述契约文档

- `GET /contract`：由路由与 Pydantic 模型实时生成，包含各版本生命周期、
  端点请求/响应模型、统一错误结构。
- `contracts/contract-<version>.json`：按版本分文件的离线快照。
  生成完全确定性（集合排序、`sort_keys`、无时间戳），多次生成逐字节一致。
- 历史保护：快照已存在且内容变化时拒绝覆盖（`SnapshotConflictError`），
  防止新版本悄悄改写历史契约；确需变更须显式放行。

```bash
uv run python scripts/generate_contracts.py                  # 生成/更新快照
uv run python scripts/generate_contracts.py --check          # 校验快照与代码一致（CI 用）
uv run python scripts/generate_contracts.py --diff           # 打印差异报告（不写盘）
uv run python scripts/generate_contracts.py --allow-update   # 放行仅含兼容变化的更新
uv run python scripts/generate_contracts.py --allow-breaking # 显式接受破坏性变更
```

## 契约差异与兼容性判定（版本治理）

新生成的契约会与已落盘的历史快照逐版本对比，产出**人可读、也可被程序解析**的
差异报告（`app/diff.py`）。每条变化点包含：

```json
{
  "kind": "request_field_removed",
  "category": "breaking",
  "path": "POST /v1/users request.nickname",
  "message": "请求字段 nickname 被移除：仍发送该字段的老调用方将不再被契约接受",
  "before": { "...": "变化前字段描述" },
  "after": null
}
```

判定原则是“仍按旧契约行事的老调用方会不会坏”：

| 变化 | 分类 |
| --- | --- |
| 新增端点、新增可选请求字段、响应新增字段 | 兼容 |
| 请求约束放宽（如 `maxLength` 调大）、字段标记/登记废弃、废弃信息变化 | 兼容 |
| 下线日期推迟、版本从 deprecated 变为 active | 兼容 |
| 新增错误码 | 兼容 |
| 移除端点、移除请求/响应字段 | **破坏** |
| 新增必填请求字段、可选字段变必填、请求类型收窄/约束收紧 | **破坏** |
| 响应字段由必有变可缺省、非空变可空（可能出现老调用方无法解析的新值） | **破坏** |
| 版本状态进入 `sunset`、下线日期提前或新增下线日期 | **破坏** |
| 删除错误码、统一错误外壳结构收窄 | **破坏** |

报告聚合计数（`breaking_change_count` / `compatible_change_count`），
并在服务上可直接获取：

- `GET /contract/diff`：历史快照 vs 当前代码生成契约的全量报告；
- `GET /contract/diff?version=v1`：只看某个版本，未知版本返回 `404`。

### 破坏性变更的处理规则

落盘门禁在 `app/snapshot.py` 的 `SnapshotStore.write_all(...)`：

1. 历史快照不存在（新版本首次落盘）：允许写入；
2. 内容一致：幂等，字节不变；
3. 仅含**兼容变化**：默认仍拒绝（历史快照的任何修改都必须是有意的），
   传 `allow_update=True`（脚本用 `--allow-update`）放行；
4. 含**破坏性变化**：抛 `BreakingChangeError`（同时是 `SnapshotConflictError`），
   异常对象携带完整差异报告；必须显式 `allow_breaking=True`
   （脚本用 `--allow-breaking`）才能写入。

## 并发安全与原子落盘

契约生成与快照写入在并发下与单次执行等价（`app/snapshot.py`）：

- **串行化**：进程内按快照目录复用一把锁；跨进程用目录内
  `.snapshots.lock` 上的 `flock` 排他锁，多线程/多进程同时跑也会排队；
- **整份校验**：锁内只生成一次契约，先对所有版本做历史保护与破坏性门禁，
  任一份不通过则**全部不写**，已存在快照保持原样；
- **原子替换**：每份快照先写同目录临时文件、`fsync` 后 `os.replace`，
  读侧任何时刻看到的都是完整的旧文件或完整的新文件，不会读到半截内容；
- **结果一致**：因为生成完全确定性且写入被串行化，无论多少并发，
  最终快照与单次生成逐字节一致（见 `tests/test_snapshot_concurrency.py`，
  含 16 线程并发、并发读写、5 进程跨进程并发场景）。

## 运行时字段级废弃提醒

字段级废弃不再只落在文档里：请求**实际用到**已废弃字段时，
响应会额外带一个结构化的 `Deprecation-Notice` 响应头（JSON 数组）：

```http
Deprecation-Notice: [{"field": "nickname", "direction": "request",
  "deprecated_since": "2026-01-15", "sunset_at": "2026-12-31",
  "replacement": "v2.display_name", "reason": "...请改用 v2 的 display_name"}]
```

- `direction` 为 `request`（调用方传了该字段）或 `response`
  （响应带出了该字段的非空实际值；显式 `null` 不算“还在用”）；
- 同一次请求的请求侧与响应侧都会记录，逐字段给出废弃日期、计划下线日期、
  建议迁移目标与说明，方便服务端自查“谁还在用废弃字段”；
- 只对**已废弃版本**生效；活跃版本（v2）不产生任何字段级提醒，
  也不改变响应体与原有 `Deprecation` / `Sunset` / `Link` 版本级提示头；
- 字段的下线计划与迁移建议在 `VersionInfo.deprecated_fields` 中用
  `FieldDeprecation` 登记；这些运行时元数据**不进入契约快照**，
  已落盘的历史契约保持逐字节不变。

## 验证方式

```bash
uv sync
uv run pytest          # 46 个用例，日志逐条列出业务场景与结果
uv run uvicorn main:app
```

测试覆盖（日志中可见各场景）：

- `tests/test_version_compat.py`：多版本并存、废弃提示头、字段集隔离；
- `tests/test_sunset.py`：下线版本一致拒绝、未知版本与 404 的区分；
- `tests/test_error_normalization.py`：跨版本校验错误结构一致、业务错误码、
  内部异常不泄露堆栈；
- `tests/test_contract_docs.py`：契约内容、生成可复现、快照防覆盖、
  `/contract` 与 `/contract/diff` 自描述端点；
- `tests/test_contract_diff.py`：兼容/破坏性变化逐条分类、破坏位置与说明、
  报告可解析且确定性；
- `tests/test_snapshot_concurrency.py`：破坏性变更默认拦截与显式放行、
  兼容变更门禁、整份 all-or-nothing、16 线程与跨进程并发写入逐字节一致、
  并发读写读到的始终是完整合法 JSON；
- `tests/test_runtime_deprecation.py`：请求/响应两侧废弃字段的结构化提醒内容、
  活跃版本无提醒、未使用/显式 null 不提醒、校验失败与下线拒绝不受影响。

本地快速验证新能力：

```bash
uv run python scripts/generate_contracts.py --diff   # 期望“无差异”
uv run pytest tests/test_contract_diff.py tests/test_snapshot_concurrency.py \
             tests/test_runtime_deprecation.py -v    # 期望全部通过
uv run uvicorn main:app                               # 启动后：
#   curl -s localhost:8000/contract/diff | python -m json.tool
#   curl -si -X POST localhost:8000/v1/users \
#     -H 'content-type: application/json' \
#     -d '{"name":"Bob","nickname":"bobby"}' | grep -i deprecation
```
