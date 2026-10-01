# 首板子工作台验证记录

候选：`classic_12_seer_witch_hunter_idiot@1.0.0`

主来源：网易《狼人杀·官方正版》12 人标准场规则页。补充来源保留在 `sources.yaml`，并明确标注为社区、转载或补充资料。

## 已完成的机器门禁

- `sources.yaml`：5 条记录均通过 `SourceEvidence` 严格 schema；每条有 HTTP(S) URL、检索方式、可复算 hash 和研究摘要。
- `claims.yaml`：18 条记录均通过 `RuleClaim` 严格 schema；全部 claim 引用已闭包到 source ID，未把旧的 `UNVERIFIED` 占位放入 canonical claims。
- `coverage.json`：16/16 machine-required items，100%，`blocking_requirement_ids` 为空；两条依赖当前不可复算原文的旧警长平票候选，以及 `single_resolution_phase` 这一系统调度决议，均显式标为 `required: false`/`DEFERRED`，不能误读为已被官方来源覆盖。当前采用的警长平票候选改由可复算的社区 HTML 支撑。
- `conflicts.json`：没有 `NEEDS_DECISION`、`UNVERIFIED`、`unresolved=true` 或非空 `unresolved_conflicts`；仍保留 8 个 `REVIEW_REQUIRED` 记录供人工/系统版本审核。
- `publish-manifest.json`：列出 13 个 Markdown 文件，每个文件都有实际 SHA-256；本次新增 board-owned `victory.role_groups` 后，canonical logical manifest digest 为 `1aec24876a5f4d72e3575f12c35f1a8af51985ffe44bff48ed5beb2851a904da`，manifest 文件自身 SHA-256 为 `29bef53520b5af5e4c893a048ade0ff18fdf25445a0582d8e2240a31904608fe`。候选仍为 `CANDIDATE_PENDING_HUMAN_REVIEW`。
- 角色动作契约回归：`tests/contract/test_workbench_role_action_contract.py` 已验证预言家使用 102 `SEER_INSPECT`、女巫资源使用 `witch_heal`/`witch_poison`，以及狼人使用 101 `WOLF_KILL`；动作目标数和资源 ID 与 `config/actions.yaml` 一致。狼人最终刀口提交要求团队协商凭证，团队讨论与最终提交授权保持分离。该项目前是静态契约回归，草稿生成和发布器尚未自动加载 `config/actions.yaml` 做跨模型门禁；自动门禁仍需后续接入。
- 在临时 vault 副本中使用 `pending-human-review` 的模拟 `ReviewRecord` 运行 `RulesetPublisher` 成功；该模拟没有写入真实 `vault/published` 或真实 `vault/compiled`。
- 该临时模拟生成的 compiled package identity 为 `ddf6f94f80c248a7f976a2ec97a387aa36b004b9a1424b8d4c927ed1cbb0fa96`，临时发布 manifest 文件 hash 为 `ac2ccf8865886f1e4a262e4ac6cbbe43023a1ab9821676f71141e64ac6626cab`；两者只证明候选在隔离副本可编译，不是正式发布记录。

## 来源真实性边界

`sources/*.md` 和 `sources.yaml` 的 `excerpt` 是研究摘要，不是网页或 PDF 的逐字引文。网易官方页、领域圈补充页和烂柯 PDF 的 `content_sha256` 分别对应 `sources/raw/source-netease-12-standard.html`、`sources/raw/source-langrensha-community.html` 和 `sources/raw/source-lanke-rulebook-secondary.pdf` 的实际原始响应字节，可用 `Get-FileHash -Algorithm SHA256 -LiteralPath <path>` 复算；本次原始响应抓取时间记录为 `2026-09-28T01:19:39Z`。多特页面本次直接 HTTPS 重取返回 404，领域圈警长页本次直接 HTTPS 重取未成功；这两条记录的 hash 只对应各自已保存的摘要 Markdown 文件（`sources/source-*.md`），检索方式标为 `saved_excerpt_sha256`，可用同一命令复算，不能视为未保存的完整网页 hash。依赖这两条摘要的警长平票 claim 状态为 `CONFLICTING`，coverage 中明确 deferred。摘要没有被当作原文，也没有为当前无法重取的页面编造 hash。

## 仍需用户人工审核

正式发布仍被显式 `ReviewRecord(decision="APPROVED")` 门禁拦截。审核人需要逐项确认：

1. 普通投票平票的 PK/再次平票处理是否适用于目标主持版本。
2. 警长竞选平票、警徽移交/撕徽及警长票型的来源优先级。
3. 女巫在解药未使用时获知刀口的条件和时序。
4. 社区来源和暂时无法重取的页面是否仍可作为本语义版本的补充证据。
5. 审核后将全部草稿文档的 `reviewed_by` 与 `reviewed_at` 从 `pending-human-review` 替换为真实审核记录，并重新计算 `publish-manifest.json` 的文档 SHA-256 与 canonical logical manifest digest。

在真实人工审核记录提供前，不得把该候选复制到 `vault/published`，也不得把占位身份当成真实审批。
