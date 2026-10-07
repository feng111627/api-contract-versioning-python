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
  防止新版本悄悄改写历史契约；确需变更须显式 `allow_update`。

## 契约差异与兼容性治理（`app/diff.py`）

新生成的契约会与磁盘上的历史快照逐字段比对，产出逐条变化点的差异报告
（`Change`：`kind` / `severity` / `location` / `message` / `old` / `new`），
每条都明确归类：

| 级别 | 判定立场：老调用方会不会坏 |
| --- | --- |
| `compatible` | 老请求仍成立、或老调用方会忽略的变化 |
| `breaking` | 老请求可能被拒、或老调用方按原约定解析响应会失败 |

典型判定规则（完整规则见 `app/diff.py` 模块文档）：

- 请求侧：新增**可选**字段、必填变可选、约束放宽（如 `minLength` 变小）、
  枚举新增取值、服务端不再接受某字段 → 兼容；新增**必填**字段、约束收紧、
  枚举删除取值、字段类型收窄 → 破坏。
- 响应侧：新增字段 → 兼容（老调用方忽略）；删除字段、类型改变、
  新增可空、枚举出现新取值 → 破坏。
- 端点：新增兼容、删除破坏；版本变为 `sunset` 或下线日期提前 → 破坏；
  废弃标记、后继版本、废弃日期等提示性元数据变化 → 兼容。
- 历史快照不存在（首次落盘）不产生破坏性变更。

### 破坏性变更处理规则

写盘走两级门禁（`app/docs.py: write_snapshots`，所有版本**先统一校验、
再统一写盘**，任一条不过就一个文件都不动）：

1. 快照已存在且内容不同 → 默认 `SnapshotConflictError`，
   须 `allow_update=True` 才允许更新；
2. 差异里含破坏性变更 → 即使 `allow_update` 也抛 `BreakingContractChangeError`，
   必须再显式 `allow_breaking=True` 放行。

服务入口：

- `GET /contract/diff`：全部版本的结构化差异报告
  （`compatible_overall` + 每版本 `breaking_count` / `compatible_change_count` / `changes`）。
- `GET /contract/diff/v1`：单版本 JSON 报告；
  `GET /contract/diff/v1?format=text`：人可读文本报告（含破坏原因与旧值/新值）。

## 运行时字段级废弃提醒（`app/deprecation.py`）

废弃不再只写在契约里：一次请求只要**实际用到**已废弃字段
（请求体里传了，或响应体里带出了），响应就会带结构化提醒：

- `Deprecation-Info`：JSON 数组（ASCII 安全、排序确定），每个条目包含
  `field`、`direction`（`request`/`response`）、`locations`（`$.a.b[0]` 风格定位）、
  `deprecated_since`、`sunset_at`（计划下线日期）、`successor_field`（建议迁向）、
  `reason`（人话说明）。
- `Deprecation-Fields`：本次命中的废弃字段名汇总，便于快速判断。
- 只对登记了字段废弃信息的版本生效；**活跃版本永远不产生提醒**。
- 提醒只落在响应头，不改变响应体；未使用废弃字段的请求不附带提醒。

字段废弃信息在 `VersionInfo.field_deprecations`（`FieldDeprecation`）登记，
同时进入契约的 `version.deprecated_field_details`，是文档与运行时的同一事实来源。

## 并发安全

- 同一快照目录的生成/比对/写入由目录级锁串行化，不同目录互不阻塞；
- 写盘采用「同目录临时文件 → flush/fsync → `os.replace` 原子替换」，
  外部任何时刻读到的都是完整文件，不会出现半截内容；
- 生成是纯确定性计算，串行化 + 原子写保证多份并发写入的最终结果
  与单次生成**逐字节一致**；并发下历史门禁同样生效（要么全拒、磁盘原样，
  要么显式放行后整批收敛）。

```bash
uv run python scripts/generate_contracts.py
uv run python scripts/generate_contracts.py --check                 # CI 校验，漂移时打印差异
uv run python scripts/generate_contracts.py --allow-update          # 接受兼容变更
uv run python scripts/generate_contracts.py --allow-update --allow-breaking  # 显式放行破坏
```

## 验证方式

```bash
uv sync
uv run pytest          # 62 个用例，日志逐条列出业务场景与每个用例的通过情况
uv run uvicorn main:app
```

测试覆盖（日志中可见各场景）：

- `tests/test_version_compat.py`：多版本并存、废弃提示头、字段集隔离；
- `tests/test_sunset.py`：下线版本一致拒绝、未知版本与 404 的区分；
- `tests/test_error_normalization.py`：跨版本校验错误结构一致、业务错误码、
  内部异常不泄露堆栈；
- `tests/test_contract_docs.py`：契约内容、生成可复现、快照防覆盖、
  `/contract` 自描述端点；
- `tests/test_contract_diff.py`：兼容/破坏逐条分类、破坏性变更默认被拦、
  两级放行规则、`/contract/diff` 端点（JSON + 文本）；
- `tests/test_runtime_deprecation.py`：请求/响应两方向废弃提醒的内容、
  下线时间与迁向、活跃版本无提醒、响应体不回归；
- `tests/test_concurrency.py`：并发生成逐字节一致、并发写入收敛一致、
  写入过程中读到的始终是完整 JSON、并发下历史保护与显式放行。

本地手工验证运行时提醒：

```bash
uv run uvicorn main:app &
curl -s -i -X POST localhost:8000/v1/users \
  -H 'content-type: application/json' \
  -d '{"name":"Bob","nickname":"bobby"}' | grep -i deprecation
# 可见 Deprecation / Sunset / Link 版本级头，以及 Deprecation-Info / Deprecation-Fields
curl -s 'localhost:8000/contract/diff/v1?format=text'
```
