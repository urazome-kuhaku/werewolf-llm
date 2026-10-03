# 首板子工作台验证与正式发布记录

板子：`classic_12_seer_witch_hunter_idiot@1.0.0`

主来源：网易《狼人杀·官方正版》12 人标准场规则页。补充来源保留在 `sources.yaml`，并明确标注为社区、转载或补充资料。

2026-10-01，项目所有者（本会话用户）明确批准“预女猎白可以板子发布了，没什么问题”。审核仅批准当前 manifest 闭包，不变更技能语义。实际审核记录保存在 `human-review.json`，正式发布结果保存在 `publish-result.json`。

## 2026-10-03 同版本修正记录

实测对局发现初版遗漏后，项目所有者明确授权：“那你修复一下吧 直接修改1.0.0板子就行 因为其实这也是我们初版没验证出来的问题，跑了实测对局以后才发现的”。本次只修正两个独立字段：`knife_rule.final_target_required` 保持 `false`，新增 `knife_rule.plan_confirmation_required: true`；`day_flow.vote.visibility_during_collection` 从 `public` 改为 `secret`，`reveal_after_close: ballots_and_totals` 保持不变。旧发布证据仍保留在 `.runtime/classic_12_correction_backup_20261003/`，实际替换前的活动包也保留在 `.runtime/classic_12_correction_active_moved_20261003/`。

旧/新哈希对应关系如下：旧候选 board `15f753f0de1fcc5550e3dbd216cb1e90d82c707c331434754b1f4287b4e2e340` → 新候选 board `5baa83ff307416afbc6372104ac3910f25e2e3dac816fd116dfcb12fa57d5dea`；旧正式 board `cc70ad6cfc849e1df40fe041a20cd93a4eef4943d0dfcef7144c0cd8d4e6df26` → 新正式 board `cd249018bf61707610752fefe3b46d49d2ab45096aec4d8466d580aebc6e0f46`；旧候选 canonical manifest `1aec24876a5f4d72e3575f12c35f1a8af51985ffe44bff48ed5beb2851a904da` → 新候选 canonical manifest `5de33314bc58d3f30bf01175bb57a8eb5758d26bc14210b2b204b5041f957928`；旧正式 manifest 文件 `e6e062f56e26de7e26e522cdcb6651f6fe7f895eabf28ac1cec735020b76340f` → 新正式 manifest 文件 `9c77ce2848b72362c21e20c95a901994f3e094cdb517897c0cd9e184b3981f75`；旧 compiled package `a8342b8c78205113348b077c084c5e0736d7837bbd4b452fbf31ab5972ded2c3` → 新 compiled package `cb68523105de8e9971705057d99f8c451927952bc9495ab9ac9d0525b24657f8`。新候选 manifest 文件自身 SHA-256 为 `8f65ea9bafcbdf59ad11b376f805c6c2c3ed1206d37688d4a4e3fc31c4724d5b`。

本次使用现有 `RulesetPublisher('vault').publish('official_12_20260928', ReviewRecord(...))`，由 `KnowledgePackageCompiler` 和 `CompiledKnowledgeStore` 重新生成正式输出；没有手工编辑 `vault/compiled/`。由于 compiled identity 已改变，已有游戏仍以各自冻结 snapshot 为准，不能假定会自动切换到新包；旧 compiled 备份保留用于需要旧 identity 的恢复。完整授权和哈希记录见 `correction-provenance.json`。

## 2026-10-01 原始发布门禁（历史记录）

以下门禁和哈希只对应 2026-10-01 的原始 `1.0.0` 发布，作为不可变历史证据保留；当前同版本修正的结论见后文“当前修正门禁”。

- `sources.yaml`：5 条记录均通过 `SourceEvidence` 严格 schema；每条有 HTTP(S) URL、检索方式、可复算 hash 和研究摘要。
- `claims.yaml`：18 条记录均通过 `RuleClaim` 严格 schema；全部 claim 引用已闭包到 source ID，未把旧的 `UNVERIFIED` 占位放入 canonical claims。
- `coverage.json`：16/16 machine-required items，100%，`blocking_requirement_ids` 为空；两条依赖当前不可复算原文的旧警长平票候选，以及 `single_resolution_phase` 这一系统调度决议，均显式标为 `required: false`/`DEFERRED`，不能误读为已被官方来源覆盖。当前采用的警长平票候选改由可复算的社区 HTML 支撑。
- `conflicts.json`：没有 `NEEDS_DECISION`、`UNVERIFIED`、`unresolved=true` 或非空 `unresolved_conflicts`；仍保留 8 个 `REVIEW_REQUIRED` 记录供人工/系统版本审核。
- `publish-manifest.json`（历史）：列出 13 个 Markdown 文件，每个文件都有实际 SHA-256；本次新增 board-owned `victory.role_groups` 后，canonical logical manifest digest 为 `1aec24876a5f4d72e3575f12c35f1a8af51985ffe44bff48ed5beb2851a904da`，manifest 文件自身 SHA-256 为 `29bef53520b5af5e4c893a048ade0ff18fdf25445a0582d8e2240a31904608fe`。该工作台输入 manifest 的 `CANDIDATE_PENDING_HUMAN_REVIEW` 状态作为原始发布历史保留。
- 角色动作契约回归：`tests/contract/test_workbench_role_action_contract.py` 已验证预言家使用 102 `SEER_INSPECT`、女巫资源使用 `witch_heal`/`witch_poison`，以及狼人使用 101 `WOLF_KILL`；动作目标数和资源 ID 与 `config/actions.yaml` 一致。狼人最终刀口提交要求团队协商凭证，团队讨论与最终提交授权保持分离；本次 `RulesetPublisher` 已执行该角色动作契约校验。本报告不对草稿生成流程是否自动加载 `config/actions.yaml` 作额外结论。
- `RulesetPublisher('vault').publish('official_12_20260928', ReviewRecord(...))`（历史）已通过真实来源、覆盖、冲突、文档闭包、角色动作契约和编译门禁；原始正式 board 已通过 `uv run werewolf rules validate vault/published/boards/classic_12_seer_witch_hunter_idiot/1.0.0/board.md`。
- 原始正式 compiled package identity 为 `a8342b8c78205113348b077c084c5e0736d7837bbd4b452fbf31ab5972ded2c3`，原始发布 manifest 文件 hash 为 `e6e062f56e26de7e26e522cdcb6651f6fe7f895eabf28ac1cec735020b76340f`；原始 `CompiledKnowledgeStore.load` 已确认 package identity 与 board ref，发布目录 13 份 Markdown 的 `reviewed_by`/`reviewed_at` 均已落为真实审核记录且无 pending。
- 历史隔离 preview 模拟仍保留作候选复测证据；原始正式发布证据和 compiled 目录已复制到 `.runtime/classic_12_correction_backup_20261003/`，没有将原始哈希当作当前修正结果。

## 当前修正门禁

- 当前 workbench manifest canonical logical digest 为 `5de33314bc58d3f30bf01175bb57a8eb5758d26bc14210b2b204b5041f957928`，manifest 文件 SHA-256 为 `8f65ea9bafcbdf59ad11b376f805c6c2c3ed1206d37688d4a4e3fc31c4724d5b`；新 review 记录已批准该 digest。
- 通过同一 `RulesetPublisher` API 重新生成正式包，当前 compiled package identity 为 `cb68523105de8e9971705057d99f8c451927952bc9495ab9ac9d0525b24657f8`，正式 manifest 文件 SHA-256 为 `9c77ce2848b72362c21e20c95a901994f3e094cdb517897c0cd9e184b3981f75`；正式 manifest 的 `documents_sha256` 为 `8a259321486725022775c9161a6c2c156b3350c3b7a0f640acecdec7023f893f`、逻辑 `manifest_sha256` 为 `6e764ac226379b695e6d4e4821422999bf96940df50e6573151ca45134168499`，compiled `manifest.json` 文件 SHA-256 为 `af0bb39f966688deb9b30412f25364bc4ed25a99da6744e8f4be3807f4e8ffc6`、逻辑 manifest 为 `4b9af25e9ab722ea0a7ec25ac9c557d220aa56fd9c18e2c8197e063347d3b814`；13 份 Markdown 的依赖闭包、来源、claims、coverage、conflicts、角色动作契约及 compiler gate 均通过。
- `uv run werewolf rules validate vault/published/boards/classic_12_seer_witch_hunter_idiot/1.0.0/board.md` 返回 `status=valid`、`dependency_closure=validated`、`role_count=6`、`mechanic_count=3`、`interaction_count=3`。
- `CompiledKnowledgeStore.load` 已通过当前 package identity 与 board ref 校验；当前 compiled output 是由 compiler/store 生成，未手工编辑 `vault/compiled/`。旧局仍使用各自 frozen snapshot，旧 compiled identity 的恢复依赖备份，不自动迁移。

## 来源真实性边界

`sources/*.md` 和 `sources.yaml` 的 `excerpt` 是研究摘要，不是网页或 PDF 的逐字引文。网易官方页、领域圈补充页和烂柯 PDF 的 `content_sha256` 分别对应 `sources/raw/source-netease-12-standard.html`、`sources/raw/source-langrensha-community.html` 和 `sources/raw/source-lanke-rulebook-secondary.pdf` 的实际原始响应字节，可用 `Get-FileHash -Algorithm SHA256 -LiteralPath <path>` 复算；本次原始响应抓取时间记录为 `2026-09-28T01:19:39Z`。多特页面本次直接 HTTPS 重取返回 404，领域圈警长页本次直接 HTTPS 重取未成功；这两条记录的 hash 只对应各自已保存的摘要 Markdown 文件（`sources/source-*.md`），检索方式标为 `saved_excerpt_sha256`，可用同一命令复算，不能视为未保存的完整网页 hash。依赖这两条摘要的警长平票 claim 状态为 `CONFLICTING`，coverage 中明确 deferred。摘要没有被当作原文，也没有为当前无法重取的页面编造 hash。

## 审核结论

人工审核已完成并作出 `APPROVED` 决定。`approved_manifest_sha256` 与候选完整文件闭包的 canonical logical manifest digest 一致；发布器在 staging 中写入审核元数据后生成正式 `vault/published/` 和 `vault/compiled/`。原始 draft、候选 manifest 和历史 preview 证据均保留。
