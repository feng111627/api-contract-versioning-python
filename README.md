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

```bash
uv run python scripts/generate_contracts.py          # 生成/更新快照
uv run python scripts/generate_contracts.py --check  # 校验快照与代码一致（CI 用）
```

## 验证方式

```bash
uv sync
uv run pytest          # 20 个用例，日志逐条列出业务场景与结果
uv run uvicorn main:app
```

测试覆盖（日志中可见各场景）：

- `tests/test_version_compat.py`：多版本并存、废弃提示头、字段集隔离；
- `tests/test_sunset.py`：下线版本一致拒绝、未知版本与 404 的区分；
- `tests/test_error_normalization.py`：跨版本校验错误结构一致、业务错误码、
  内部异常不泄露堆栈；
- `tests/test_contract_docs.py`：契约内容、生成可复现、快照防覆盖、
  `/contract` 自描述端点。
