# 预女猎白首板子人工审核清单

本清单不代表已经批准。机器工作台的结构、引用闭包、覆盖率和发布 manifest 已完成；正式发布仍要求真实 `ReviewRecord`。

## 需要审核人确认的规则

### 普通投票平票

网易官方 12 人标准场页面没有完整冻结普通投票平票后的 PK、重投和再次平票处理。候选暂存为 `pk_then_no_exile_on_retie`，证据来自可复算的社区 HTML，需确认目标主持版本是否采用。

### 警长竞选与警徽

主来源确认首日有警长并列出 1.5 票；可复算的社区 HTML 补充首次平票 PK、再次平票无警长，以及死亡或退选后的传徽/撕徽。其同时给出2票面杀口径，不能覆盖主来源票型。多特攻略和另一领域圈页仅保存摘要，旧交叉 claim 保持冲突状态，需确认是否冻结到本语义版本。

### 女巫刀口信息

主来源没有完整说明女巫获知刀口的时序。当前仅保留社区来源的条件化候选：解药未使用时由主持告知刀口。需确认实际主持流程和私有信息边界。

### 屠边终局安全约束

网易页面明确狼人杀死全部神职或全部平民即屠边，但没有说明“至少一名狼人存活”的实现防护，也没有说明与好人同时满足条件时的判定优先级。当前机器字段保留该防护并把优先级留给主持版本决议，不能当作官方逐字规则。

### 夜间统一结算边界

网易页面列出角色夜间行动，但没有冻结实现层的单一 `NIGHT_RESOLVE` 阶段。当前字段是系统调度决议，已在 `claims.yaml` 标为 `CONFLICTING`、在 `coverage.json` 标为 deferred；需由运行时和主持版本共同确认。

### 其他实现字段

以下字段目前属于系统/主持实现选择，不能从来源摘要自动推成官方条款：`night_windows` 的依赖和顺序、`day_flow.vote.reveal_after_close`、`knife_rule.final_target_required`、投票公开粒度，以及请求阶段和统一结算阶段的写入边界。它们已集中记录在 `conflicts.json` 的 `conflict-runtime-default-fields`，发布前需与运行时契约逐项核对。

### 证据新鲜度

多特 URL 当前直接 HTTPS 重取返回 404，领域圈警长页当前直接 HTTPS 重取未成功。工作台保留此前已获取正文的 hash 和检索方法，但审核人应在批准前重新抓取、保存正文或明确接受其失效状态。

这两条来源的 `content_sha256` 只对应工作台保存的摘要 Markdown 文件，不能用来定位未保存的网页原文；因此其警长平票主张在 `claims.yaml` 中是 `CONFLICTING`，在 `coverage.json` 中不是 required 满足项。

## 审核操作

审核人提供真实身份、UTC 日期和 `APPROVED` 决策后，必须：

1. 更新所有草稿文档的 `reviewed_by` 和 `reviewed_at`。
2. 重新计算 `publish-manifest.json` 每个 Markdown 文件的 SHA-256。
3. 新增 board-owned `victory.role_groups` 后，canonical logical manifest digest 为 `1aec24876a5f4d72e3575f12c35f1a8af51985ffe44bff48ed5beb2851a904da`；文档或证据发生变化时重新计算。
4. 只在真实 `ReviewRecord` 的 `approved_manifest_sha256` 与 manifest 一致后运行发布器。
