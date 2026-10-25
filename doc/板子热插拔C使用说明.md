# C 阶段使用说明

完成证据及冻结提交见[实施与验收记录](板子热插拔实施与验收记录.md)；支持范围见[支持矩阵](声明式规则支持矩阵.md)。以下路径中的 `demo@1.0.0` 和候选目录是占位示例，需要替换为实际输入。

## 暂存、门禁和开局

候选目录包含 `publish-manifest.json`、完整 `draft/` 依赖闭包，以及对应板子的 `execution.yaml`。manifest 必须逐项列出并校验所有依赖与执行配置。候选保持 `CANDIDATE_PENDING_HUMAN_REVIEW`，实验激活不生成或替代人工审核记录。

```bash
uv run --no-sync werewolf catalog stage \
  --root .runtime/catalog --board demo@1.0.0 \
  --workbench path/to/candidate --scenario path/to/full-game.yaml
uv run --no-sync werewolf catalog activate \
  --root .runtime/catalog --board demo@1.0.0
uv run --no-sync werewolf catalog status --root .runtime/catalog --board demo
uv run --no-sync werewolf play init \
  --catalog-root .runtime/catalog --board demo@1.0.0 \
  --all-scripted --output .runtime/demo-play --game-id demo-play
uv run --no-sync werewolf scenario run \
  --config .runtime/demo-play/game.yaml --scenario path/to/full-game.yaml \
  --output .runtime/demo-play/report.json --experimental-preview
```

`--scenario` 可重复提供。激活实际运行全部场景，并合并冻结包要求的技能覆盖；错误断言、必要步骤未消费、预算耗尽或缺少成功技能提交均不能激活。玩家技能的成功证据来自已确认外部请求、内部技能处置、账本及收据的对应关系。PASS 不算成功释放其他技能；HOST 与 AUTOMATIC 单独计数。

`FULL_GAME` 使用通用运行器，核对 FINISHED/CLOSED、归档 manifest 以及归档内实际路径中的最终快照。`COMMAND` 按顺序执行封闭主持命令与位置断言。玩家动作只按本人座位、本人身份、公开轮次/相位及授权技能投影匹配，目标和参数为声明数据；不能传入 Python、状态补丁或任意 shell。

断言校验预期值的类型；例如 `alive` 必须填写布尔值，不能用数字 `1` 代替 `true`。不存在的座位、未声明的状态或未知技能报告 `UNAVAILABLE`，即使使用 `NE` 也不能通过。存活玩家的空死亡原因和未判胜时的空获胜方仍是合法的 `null`。GAME 布尔状态按冻结声明及实际过期边界读取；到期记录删除后使用声明的初始值。命令、玩家 turn 和回合预算均会阻止场景无限运行。

## 版本和引用

同一个板子的新版本成为默认值后，已激活且未下架的旧版本仍可通过精确 `id@version` 选择。开局准备不占用引用；实际 START 持久取得引用并绑定局内身份。失败或取消启动清理本次引用，重试增加 generation，旧 generation 的清理不能释放新引用。

```bash
uv run --no-sync werewolf catalog retire --root .runtime/catalog --board demo@1.0.0
uv run --no-sync werewolf catalog unload --root .runtime/catalog --board demo@1.0.0
```

下架阻止新局选择，已有局继续使用自己的冻结规则。存在活动引用时不能卸载。结束局核验真实归档和最终快照后释放引用，并保留 CLOSED_ARCHIVE 记录，不能复用已结束的 game ID。同 ID/版本不能换内容，包括卸载后的再次暂存。

目录的 `catalog_id` 绑定规范绝对根路径的 SHA-256；不同目录不能交换引用。本接口不承诺任意移动目录后保持目录身份。局内 `CatalogAdmission` 还绑定板子版本、知识包身份、门禁身份、generation 和 game ID 派生的 lease ID。

## 等待选择的私有检查点

公开循环快照继续拒绝未完成的触发、收集、选择和边界。只有真实、无在途请求的 WAITING_CHOICE 才能创建私有检查点。在 moderator 命令循环中使用：

```text
checkpoint save
checkpoint verify checkpoint-<sha256>
```

停止旧会话后，从未加载对局的新 moderator 进程恢复：

```bash
uv run --no-sync werewolf moderator --config .runtime/demo-play/game.yaml --experimental-preview
```

```text
checkpoint restore checkpoint-<sha256>
```

也可不启动玩家会话，仅校验检查点：

```bash
uv run --no-sync werewolf checkpoint verify checkpoint-<sha256> \
  --config .runtime/demo-play/game.yaml
```

检查点校验独立的板子引用、执行模型摘要、完整执行制品摘要和知识包摘要，保留实际知识快照闭包。恢复先在独立状态和临时制品中验证，再轮换 session epoch、启动新 Gateway/玩家并绑定新会话。有效的较新公开快照、结束局或已归档结果会阻止旧检查点回滚。私有检查点不会复制进最终公开归档。

## 首夜流程及规则来源

新包可声明冻结的 `night_boundary_schedule`，要求首日警长选举与每夜公告，随后完成真实遗言、警徽、触发和判胜边界。同一夜只能产生一次黎明及公告；未公告首夜死者的警长参与资格必须有死亡、账本、收据和夜间来源证明。

夜间 AUTO 和玩家选择造成的派生死亡沿实际 occurrence、死亡事实、结算账本及收据追溯到当夜已关闭的末组。首夜警长前置流程与后续夜间均使用这条来源链；上一日放逐记录不能作为本夜来源。预算错误、未完成触发、遗言或警徽会阻止判胜。`NEXT_NIGHT_START` 状态保留到本次判胜和 FINISHED 最终快照，实际进入下一夜时过期。

两份目标板子的未决规则使用已获授权的实验变体，不能把脚本验收结果写成网易正式裁定。真实 Pi 模型行为由项目所有者本地验证，远端脚本验收只证明相应的配置和执行路径。
