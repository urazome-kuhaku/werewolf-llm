# Werewolf Arena V1

Werewolf Arena 是一个由主持器、冻结知识库和多个玩家运行时组成的狼人杀实验框架。当前可以直接准备和运行的目标是已正式发布的“预女猎白”12 人板：4 狼、4 民、预言家、女巫、猎人、白痴各 1 人，采用屠边胜利。主持器保存唯一的权威状态，Pi 或脚本运行时只能通过座位绑定的请求提交发言和行动。

`classic_12_seer_witch_hunter_idiot@1.0.0` 已于 2026-10-01 经项目所有者批准并正式发布到 `vault/published/`，对应运行时编译包位于 `vault/compiled/classic_12_seer_witch_hunter_idiot@1.0.0`。2026-10-03 根据实测对局发现的初版遗漏已获项目所有者明确授权，使用同一版本号修正为新的发布闭包；修正授权、旧/新哈希和历史快照边界见 `vault/_workbench/official_12_20260928/correction-provenance.json`。已创建游戏只要保有完整的冻结 snapshot，就继续按该 snapshot 的原规则独立恢复，不会自动迁移到本次修正；只有依赖旧 compiled identity 且缺失完整冻结包的恢复路径，才需要修正前的 compiled package 备份。新 runtime loader 对旧 frozen package 中缺少的 `knife_rule.plan_confirmation_required` 按 `false` 读取，并保持原 package identity；删除 source 与 compiled 后仅凭完整 snapshot 的恢复已实测成功，但不据此声称全部历史局都已实测恢复。审核记录和发布结果分别保存在 `vault/_workbench/official_12_20260928/human-review.json` 与 `vault/_workbench/official_12_20260928/publish-result.json`。`play init` 仍是候选复测入口，会将工作台内容编译到独立的 preview 目录；这些 setup 继续使用 `--experimental-preview`，不会改写正式发布包。

老板子当前冻结规则的关键点是：暗牌且普通死亡/放逐不翻身份；首日有警长竞选，警长票权为 1.5 且默认最后发言；狼人需要在队伍讨论后形成刀口，并由 `knife_rule.plan_confirmation_required: true` 强制完成独立的计划确认，`knife_rule.final_target_required` 仍为 `false`；女巫不能自救，每晚最多使用一瓶药，只有在解药仍可用且板子有效规则允许时，才会在狼刀确认后看到当晚刀口；猎人被狼刀或放逐时进入玩家选择触发窗口，被毒死不触发；白痴只在被放逐时自动翻牌存活并失去投票权。白天投票按 `visibility_during_collection: secret` 在收集期间保持秘密，收集关闭后按 `reveal_after_close: ballots_and_totals` 统一公开选票和票数汇总；夜间首夜死亡和每天符合规则的放逐者可发表遗言；普通投票和平票重投进入板子定义的 PK，PK 再平票则无人放逐；胜负按冻结的屠边条件在结算边界检查。完整字段和来源见 `doc/老板子初版实现说明.md` 及板子 `board.md`。

## 环境与安装

项目使用 CPython 3.11 和 `uv` 管理依赖。请在仓库根目录执行：

```powershell
uv sync --locked
uv run werewolf --help
```

不要用 `pip` 或手工维护 `requirements.txt`。依赖变化时使用 `uv add` 或 `uv remove`，并同时保留 `pyproject.toml` 与 `uv.lock`。

## 知识库和诊断

知识源文件位于 `vault/_workbench/`，正式发布包是带版本的不可变快照。不要直接编辑 `vault/published/` 或 `vault/compiled/`；通常的板子变更应在 workbench 中产生新的语义版本，再经过审核和发布。若实测发现初版遗漏，只有在项目所有者明确授权、提供 correction provenance 并保留旧/新哈希与历史快照边界时，才能按同版本修正流程重新发布；这不改变 Publisher 对普通版本冲突的拒绝规则。`vault/compiled/` 是本地生成且被 gitignore 的运行时缓存；在新机器上可用既有 `KnowledgePackageLoader`、`KnowledgePackageCompiler` 和 `CompiledKnowledgeStore` 从 `vault/published/` 重建，已存在正式 Markdown 但缺少 compiled 时，重复调用 publisher 不会自动恢复它。

```powershell
# 校验配置包络
uv run werewolf config validate config/game.example.yaml

# 校验老板子及其依赖闭包
uv run werewolf rules validate vault/_workbench/official_12_20260928/draft/boards/classic_12_seer_witch_hunter_idiot/1.0.0/board.md

# 校验正式发布的老板子及其依赖闭包
uv run werewolf rules validate vault/published/boards/classic_12_seer_witch_hunter_idiot/1.0.0/board.md

# 只检查本机 Pi 可执行文件和版本
uv run werewolf pi doctor

# 对已完成归档做完整性校验
uv run werewolf archive verify <archive-directory>
```

`pi doctor` 只发现可执行文件、执行 `--version` 并检查支持的 `0.87.x` 版本范围；它不会登录 provider，也不能证明凭据、模型额度或真实对局可用。当前环境诊断记录为 executable、version、version-compatible 通过，Pi `0.87.1`；`auth: skipped_no_reauth` 只表示诊断没有重复认证，不是 provider 登录已通过的证据。

## 准备老板子实验目录

`play init` 要求输出目录不存在；每次实测都要新建一个目录，不要复用上一局的 `game.yaml`、active game 或 session。建议使用 `.runtime/`，避免把私密测试数据放在仓库的临时目录中。以下命令用毫秒时间戳生成全新目录。

```powershell
# 先做离线主持器检查：12 个座位全部使用确定性的脚本运行时
$scriptDir = ".runtime/classic-scripted-" + (Get-Date -Format "yyyyMMdd-HHmmssfff")
uv run werewolf play init --output $scriptDir --all-scripted
uv run werewolf play run --config "$scriptDir\game.yaml" --experimental-preview --max-rounds 20

# 3 个 Pi 玩家，其余座位使用脚本运行时
$pi3Dir = ".runtime/classic-pi3-" + (Get-Date -Format "yyyyMMdd-HHmmssfff")
uv run werewolf play init --output $pi3Dir --pi-seats 1,2,3 --provider github-copilot --model gpt-6-luna --reasoning medium

# 4 个 Pi 玩家，其余座位使用脚本运行时
$pi4Dir = ".runtime/classic-pi4-" + (Get-Date -Format "yyyyMMdd-HHmmssfff")
uv run werewolf play init --output $pi4Dir --pi-seats 1,2,3,4 --provider github-copilot --model gpt-6-luna --reasoning medium
```

Pi 登录后的默认值是 provider `github-copilot`、model `gpt-6-luna`、reasoning `medium`。用户要求使用其他 provider 时可以加 `--provider`，但本项目当前的 Pi 联调默认和验收约定仍是 `github-copilot/gpt-6-luna`。Pi 座位必须是 1 到 12 的不重复整数；`--all-scripted` 不能和 `--pi-seats` 同时使用。

执行 3/4 Pi 联调前，先在本机完成 Pi 安装和 `github-copilot` provider 登录，再确认 `pi --version` 属于 `0.87.x`。`play init` 只生成 setup，不会启动 Pi；真实联调从对应 setup 的 `play run` 开始：

```powershell
uv run werewolf play run --config "$pi3Dir\game.yaml" --experimental-preview --max-rounds 20
uv run werewolf play run --config "$pi4Dir\game.yaml" --experimental-preview --max-rounds 20
```

不要在同一目录中先后运行 3 Pi 和 4 Pi；每局都重新执行上面的目录生成和 `play init`。这些由 `play init` 生成的 setup 仍是候选 preview，因此 `--experimental-preview` 必须保留。正式发布包的启动配置见下一节，不使用这个标记。

`play init` 会在输出目录生成：

- `game.yaml`：12 个座位、运行时类型、provider、model、reasoning，以及 `preview/compiled` 和 `games` 路径；
- `setup-report.json`：源文件 digest、编译包 identity、动作契约校验结果和可复制的绝对配置路径；
- `preview/compiled/`：本次实验专用的编译知识包；
- `games/`：主持器的 active game、snapshot 和最终 archive 根目录。

报告中的 `launch` 是可从仓库根目录直接执行的 PowerShell 命令，配置路径已加引号，路径包含空格时也可以使用。也可以手工执行：

```powershell
uv run werewolf play run --config "$pi3Dir\game.yaml" --experimental-preview
```

## 使用正式发布包启动

正式发布本身不会改变 `play init` 的候选 preview 流程。需要直接运行正式知识时，保留现有 `game.yaml` 的 `game`、`players` 等配置，删除顶层 `play_setup` 段，并将 `paths` 改为指向正式 compiled 根目录（下面示例假定 `game.yaml` 位于仓库根目录）：

```yaml
paths:
  compiled_root: vault/compiled
  games_root: .runtime/formal-games
```

使用该配置运行时不加 `--experimental-preview`：

```powershell
uv run werewolf play run --config .\game.yaml --max-rounds 20
```

生成的 `game.yaml` 可以按测试机器修改。例如要指定 Pi 可执行文件和进程策略，在顶层加入：

```yaml
runtime:
  pi:
    executable: 'C:\Users\you\AppData\Local\nvm\v22.22.0\pi.cmd'
    compatible_version: '0.87.x'
    auto_compaction: true
    auto_retry: false
```

Pi 玩家自身的 `provider`、`model`、`reasoning` 仍写在对应的 `players` 项中。不要把 API key、token、`.env` 或私密 Pi 日志写入 YAML 或提交到仓库。未设置 `runtime.pi.executable` 时会使用环境变量 `WEREWOLF_PI_EXECUTABLE`，再回退到 PATH 中的 `pi`。

## 自动运行和真实 Pi 前提

自动入口会通过主持器命令边界驱动一局：

```powershell
uv run werewolf play run --config "$pi3Dir\game.yaml" --experimental-preview --max-rounds 20
```

`--max-rounds` 是保护上限，达到上限会以失败结束，不会伪造胜利。首日警长候选人可以显式指定：

```powershell
uv run werewolf play run --config "$pi3Dir\game.yaml" --experimental-preview --sheriff-candidates 1,2
```

省略时，自动运行从首日公告前由板子规则和当前状态计算出的全体合法参与座位中取前两个。资格计算会使用本首夜死亡来源、警长竞选的 PK/串行发言边界以及技能状态遮罩；其中包括有有效首夜死亡 provenance 但尚未公告的死者，不能把候选资格简化成“当前存活座位”。当前没有让玩家自主报名或退水的独立命令；需要其他候选人时使用 `--sheriff-candidates`。显式传入的座位仍会由主持器根据冻结状态校验。若候选中包含尚未公告的死者，仍按固定链路完成竞选和 `sheriff transfer`，再执行 `day announce`，然后处理遗言和必要的警徽；不要把 badge 命令提前到公告之前。

使用 Pi 前请先在本机完成 Pi 安装和 provider 登录，并确认 `pi --version` 属于 `0.87.x`。每局主持器启动一个绑定本局冻结知识的 loopback 知识网关，再为每个 Pi 座位签发不同的 bearer token、创建独立的 RPC 进程和 session 目录；启动时会加载 `extensions/werewolf_knowledge.ts`，知识读取和技能状态查询按座位隔离。`runtime.pi.compatible_version` 是可选的非空版本标记（示例 `0.87.x`），会随该座位的进程配置传递；`pi doctor` 负责检查本机可执行文件的当前支持范围。退出、异常和 Ctrl+C 会尝试关闭所有运行时进程。失败是否存在可恢复 snapshot 取决于最近一次完整一致边界：在边界之前失败可能没有新的 snapshot，不能把每种失败都当作自动可恢复。

本轮真实联调只预留 Pi 和脚本两类 harness。TUI harness 的接口目前保持在运行时抽象层，完整热插拔实现仍是后续范围。

## 人工主持模式

需要观察每一步时，启动长驻主持器：

```powershell
uv run werewolf moderator --config "$pi3Dir\game.yaml" --experimental-preview
```

命令循环是有状态的。`status` 用来确认当前队列和边界，`next` 只推进一个座位，失败的同一个请求用 `retry`；它不是用来跳过下面的固定顺序。每个边界只执行一次，命令返回的 `phase` 决定进入下一个已列出的分支。

老板子候选的人工主持顺序如下，照这条链路执行即可：

1. 建局：`new` → `next` → `next` → `start`；在 `PLAYER_PREPARE` 中反复 `prepare status` / `prepare next [seat]`，全部 ready 后执行顶层 `next`。
2. 每夜：`night open` → 反复 `night team next`（失败用 `night team retry`，需要新一轮时用 `night team again`）→ `night plan status` 确认当前代次为 `READY` → `night plan next`（失败用 `night plan retry`；`night team plan status/next/retry` 是等价别名）→ `night advance` → `night open` → 反复 `night action next [seat]`（失败用 retry）→ `night advance` → `night open` → `night auto-resolve` 或 `night resolve <json-file>`。`night plan` 产生当前夜冻结的团队共识；只有它完成并满足板子 `knife_rule.available_after_window` 后，`night advance` 才进入夜间行动。
3. 夜间结算后先看 `phase`。没有触发窗口时执行 `victory night-check`；若为 `TRIGGER_ACTION`，执行 `trigger open` → `trigger pending`；对每个玩家选择请求反复执行 `trigger next/retry [seat]`，请求全部提交后再执行一次 `trigger resolve <json-file>` 或板子支持的 `trigger auto-resolve`，最后 `trigger finish`。自动触发没有玩家请求时也要先推进空请求队列，再执行 resolver。夜间来源的 `trigger finish` 会回到 `DAY_ANNOUNCE`，随后再执行 `victory night-check`。
4. 首日警长：`victory night-check` 为 `ONGOING` 后，先执行 `sheriff start <candidate...>`，再完成 `sheriff speech`、`sheriff vote` 和必要的 PK，最后 `sheriff transfer`。这一步发生在死亡公告之前；合法候选可以包括有本首夜死亡 provenance 但尚未公告的死者。`sheriff transfer` 实际进入 `DAY_SPEECH`；随后先执行 `day announce` 提交死亡公告，再完成必要的警徽移交和遗言，最后才 `day speech open` 打开普通白天发言。
5. 普通白天：公告后，反复执行 `last-words status` / `last-words next [seat]`，再 `day speech open` → `day speech next/retry` → `day speech close` → `day vote open` → `day vote next/retry [seat]` → `day vote collect` → `day vote confirm`。平票时改走 `day pk speech ...` 和 `day pk vote ...`，仍按同一顺序完成 PK。
6. 白天放逐：`day confirm-exile <seat|none>` 后分两条。若没有玩家选择型触发，先排空遗言，再按需完成 `sheriff badge open` → `next` → `resolve` → `finish`，最后 `victory check`；无徽流程直接 `day finish`，再 `victory check`。若有 `TRIGGER_ACTION`，先 `trigger open`，再用 `trigger next/retry` 提交所有玩家请求（自动触发则推进空请求队列），然后执行一次 `trigger resolve <json-file>` 或 `trigger auto-resolve`；之后排空遗言、如需完成警徽。白天来源的触发有 badge 时由 `sheriff badge finish` 进入 `VICTORY_CHECK`，无 badge 时才执行 `trigger finish` 进入 `VICTORY_CHECK`，最后执行 `victory check`。白天放逐的固定关系是“触发结算 → 遗言 → badge（如需）→ 胜负”。
7. 胜负与归档：`victory check` 返回 `ONGOING` 时自动进入下一夜，不要再执行顶层 `next`；返回胜利时进入 `FINISHED`，再执行 `finish` 生成归档。可在一致边界执行 `save`，但 `finish` 只能在 `FINISHED` 执行。

首日和每个白天公告都遵守一个顺序约束：先让 `day announce` 提交公告，再处理适用的 badge 和遗言。首日 `sheriff transfer` 已进入 `DAY_SPEECH`，仍要先提交公告；如果当前警长需要移交，先完成 badge，再排空遗言。警徽 guard 不能阻止死亡公告；`sheriff badge status` 只表示后续边界是否待处理，不能把 badge 命令提前到公告之前。

| 阶段 | 主持命令 | 说明 |
| --- | --- | --- |
| 建局 | `new` → `next` → `next` → `start` | `new` 后两次 `next` 依次进入 `RULESET_READY`、`ASSIGNED`；`start` 启动座位运行时并进入 `PLAYER_PREPARE`。 |
| 身份准备 | `prepare status`、`prepare next [seat]` | 按 status 反复执行，直到所有座位收到知识回执；再执行顶层 `next` 进入首夜狼队讨论。 |
| 首夜狼队讨论与计划 | `night open`、`night team next`、`night team retry`、`night team again`、`night plan status`、`night plan next`、`night plan retry`、`night advance` | `night open` 打开当前夜间窗口。每个狼人完成提案后，必要时用 `team again` 开始下一轮讨论；队列耗尽后先用 `night plan status` 确认 `READY`，再执行 `night plan next`（失败用 `night plan retry`，`night team plan ...` 为别名）。计划完成后才可 `night advance` 进入 `NIGHT_ACTION`；新一轮讨论会使旧计划失效。 |
| 首夜行动 | `night open`、`night action next [seat]`、`night action retry [seat]`、`night advance` | `night open` 打开行动窗口；按 status 消耗全部合法座位，再用 `night advance` 进入 `NIGHT_RESOLVE`。 |
| 夜间结算 | `night open`、`night auto-resolve` 或 `night resolve <json-file>` | 老板子可以用自动结算；手工主持其他明确结算时使用严格 JSON resolution 文件。结算后没有触发就走 `victory night-check`；有 `TRIGGER_ACTION` 就先完成 trigger，再用 `trigger finish` 回到 `DAY_ANNOUNCE`，不要执行顶层 `next`。 |
| 首日警长 | `sheriff start <candidate...>`、`sheriff speech next/retry`、`sheriff vote open`、`sheriff vote next/retry [seat]`、`sheriff vote collect`、`sheriff vote confirm`、`sheriff transfer` | `victory night-check` 为 `ONGOING` 后、死亡公告前执行。候选由 helper 计算时可以包含首夜已死但尚未公告的座位；完成候选发言、投票和必要 PK 后才 `transfer`。平票时会进入 `SHERIFF_ELECTION_PK_SPEECH` / `SHERIFF_ELECTION_PK`，重复同类 speech/vote 命令完成 PK。 |
| 白天公告和遗言 | `day announce [content]`、`last-words status`、`last-words next [seat]`、`last-words retry [seat]` | `day announce` 是公告边界，必须先提交，再处理适用的 badge 和遗言。首日 `sheriff transfer` 已进入 `DAY_SPEECH`，仍要先提交公告；若当前警长需要移交，先完成 badge，再排空遗言。没有待处理边界时才进入普通白天发言。 |
| 警徽移交 | `sheriff badge status`、`sheriff badge open`、`sheriff badge next`、`sheriff badge retry`、`sheriff badge resolve`、`sheriff badge finish` | 当前警长死亡或失去投票资格且板子启用移交时执行。先完成死亡公告，白天放逐来源还要先完成遗言；`resolve` 提交当前警长的决定，由板子规则决定移交给冻结候选或撕徽；`finish` 只推进这个边界。 |
| 白天发言和放逐 | `day speech open`、`day speech next/retry`、`day speech close`；`day vote open`、`day vote next/retry [seat]`、`day vote collect`、`day vote confirm`；`day confirm-exile <seat\|none>` | 不要给普通 `day speech open` 强行传全体座位，默认队列会保留板子规定的警长最后发言。平票会自动进入 `day pk speech` / `day pk vote` 分支；放逐投票确认后必须显式确认座位或 `none`。 |
| 白天收尾 | `day finish` | 普通白天在 `day confirm-exile <seat\|none>` 后，先排空遗言；没有 badge 时执行 `day finish` 进入 `VICTORY_CHECK`。如果触发或 badge 流程已经把 phase 转成 `VICTORY_CHECK`，不要再次执行 `day finish`，直接执行 `victory check`。 |
| 出局触发 | `trigger status`、`trigger open`、`trigger next/retry [seat]`、`trigger resolve <json-file>`、`trigger auto-resolve`、`trigger finish` | 只在当前 phase 为 `TRIGGER_ACTION` 时执行。先 `open`，再用 `next/retry` 消耗所有玩家选择请求；请求提交完成后，二选一执行一次 `resolve` 或板子支持的 `auto-resolve`，不能把 `next` 与 `auto-resolve` 当成替代步骤。猎人是玩家选择窗口；白痴的放逐效果是板子定义的 `AUTOMATIC` 触发，可推进空请求队列后自动结算。夜间来源：结算后 `trigger finish` 回到 `DAY_ANNOUNCE`，再公告；白天放逐来源：先完成触发结算、遗言和必要 badge，badge 完成后已进入 `VICTORY_CHECK` 就直接 `victory check`；没有 badge 时才 `trigger finish` 再 `victory check`。 |
| 胜负和下一轮 | `victory night-check`、`victory check` | 从夜间进入 `DAY_ANNOUNCE` 后先用 `victory night-check`；ONGOING 才继续白天，已胜利则直接进入 `FINISHED`。白天放逐/触发完成后在 `VICTORY_CHECK` 用 `victory check`；ONGOING 会自动进入下一夜的 `NIGHT_TEAM_CHAT`，不需要再执行顶层 `next`；胜利时转 `FINISHED`。 |
| 归档 | `save`、`finish` | `save` 只创建当前一致边界 snapshot。只有 phase 已是 `FINISHED` 才能 `finish`，它会创建最终 snapshot 并归档；成功归档后 session 和网关关闭。已是 `FINISHED` 时直接执行 `finish`，不要再执行顶层 `next`。 |

真实命令会根据板子是否存在警长竞选、遗言或触发技能而出现不同队列。`night auto-resolve` 和 `trigger auto-resolve` 是老板子候选的有界适配：它们根据当前已经提交并通过协议校验的请求生成并提交结算，不替玩家补交缺失的行动或选择，也不替玩家打开或推进行动窗口；调用前应按当前 `status` 完成对应窗口和请求。它们不代表任意新板子已经有通用规则解释器。触发窗口内，玩家运行时可通过自己的 `get_skill_status` 查询技能状态，主持器仍会在提交前重新校验身份、窗口、目标、资源和 session。

## 输出、私密数据和归档

`play run` 的标准输出是 JSON 行，包含公共阶段、公共发言和最终摘要；它不会把狼队频道、身份、私密技能结果或 token 打到公共输出。成功结束的最终 JSON 行包含 `status`、`phase`、`run_status`、`winner`、`archive_id`、`archive_path` 和 `snapshot_id`，其中 `archive_path` 是后续 `archive verify` 的输入。人工主持器的 `status` 默认是公开投影，只有明确输入 `status --private` 才显示主持器私密状态。

每局 active 数据位于 `<setup>/games/active/<game_id>/`：

- `private/gm.md`：主持器私密投影；
- `private/channels/wolves.md`：狼队私密频道投影；
- `private/runtime_refs.json`：座位运行时引用和投递游标；
- `ruleset/`：本局冻结知识快照；
- `snapshots/`：一致性 snapshot，其中包含公开投影和经允许的私密投影。

成功 `finish` 后，最终不可变归档位于 `<setup>/games/archive/<timestamp>_<game_id>/`，可用 `archive verify` 校验。未完成的 active 目录和已经生成的一致 snapshot 应保留，便于排查 timeout、Pi 进程退出或 provider 错误；如果失败发生在下一致边界之前，可能没有可恢复的最新 snapshot，也不会自动 resume。归档不会保存 API key；Pi stderr 只做有界、脱敏的进程诊断，不应被当作对局公屏。

## 验收矩阵与当前边界

以下矩阵把生产 CLI、离线 Pi 协议和真实 Pi 整局分开记录。`.runtime/` 只保留本机证据路径，不把私密身份提示、模型思考、token 或私密日志复制进文档。

| 验收层 | 状态 | 主证据 | 结论 |
| --- | --- | --- | --- |
| 12 座位脚本整局 | 已通过 | `.runtime/cli-ascii-final-20261001`；344/344 行 JSON 可解析，ASCII 输出按 `utf-8-sig` 恢复中文；最终 `FINISHED`、`CLOSED`、`winner=wolf` | 生产 `play init` / `play run` / `archive verify` 链路可用 |
| 3/4 座位离线 Pi 协议 | 已通过 | 真实 RPC 解析 + fake 外部进程；覆盖 bootstrap、board、个人 role、`skillstatus`、`publicspeech`、authoritative skill/vote 和 cleanup | 证明 `PiRuntime` 协议边界，不等于真实模型整局 |
| 真实 3 Pi 整局 | 已通过 | `.runtime/classic-live-pi-boundaryfinal-3-20261001-01`，seed `20260930`，Pi seats `1,2,3`，`github-copilot/gpt-6-luna`/`medium`；相邻 `.run.exitcode=0`、`.run.stderr=0` bytes、`.run.stdout` 为 350/350 行有效 JSONL；最终 `FINISHED`、`CLOSED`、`winner=wolf`、round 4/day 5；归档 `games/archive/20261001T051656Z_classic-play-001` 的 `archive verify` 返回 `status=valid`、101 files；结束后无对应 Pi 残留进程 | 真实整局按严格主持器流程正常 `finish` 并通过归档校验；夜/日来源分流和归档排除在该局收口，finish 失败 rollback 由回归测试覆盖 |
| 真实 4 Pi 整局 | 已通过 | `.runtime/classic-live-pi-boundaryfinal-4-20261001-01`，seed `20260930`，Pi seats `3,5,7,8`，其余为 `DemoRuntime`；相邻 `.run.exitcode=0`、`.run.stderr=0` bytes、`.run.stdout` 为 340/340 行有效 JSONL；最终 `FINISHED`、`CLOSED`、`winner=wolf`、round 4/day 4；归档 `games/archive/20261001T052941Z_classic-play-001` 的 `archive verify` 返回 `status=valid`、86 files；结束后无匹配 CLI/Pi 残留进程 | 4Pi 真实整局与归档收口完成 |
| 最新全量 gate | 已通过 | `uv run pytest`：1032 passed、11 skipped、1 warning（55.85s）；`uv run ruff check .` 通过；`uv run ruff format --check .`：251 files formatted；`uv run mypy src`：102 source files 通过 | 当前代码与文档收口的全量检查通过 |

旧的真实 Pi 尝试曾在夜间 trigger 后于 `finish` 失败；原因是把所有 `TRIGGER_ACTION` 都套用了白天 badge guard。修复后的全新 3Pi setup 已完成整局：夜间 `trigger finish` 回到 `DAY_ANNOUNCE`，随后通过 `victory night-check` 收口并归档。4Pi 也已在独立 setup 完成 round 4/day 4 并通过归档校验；旧失败目录只保留为修复背景。

真实 3Pi 可公开汇总的 response/action code 为 seat1 `102×1, 201×2, 202×2`、seat2 `104×1, 201×3, 202×2, 299×1`、seat3 `101×2, 201×4`；ready 均为 1，public speech 次数为 `6/4/9`。三个独立 session 的 `get_board`、`get_role`、`get_skill_status`、`search_rules` 均成功且工具错误为 0。这里的 `104` 是女巫解药，`103` 未发生；这些只作为真实 Pi 响应汇总，不展开私密目标、身份提示或 session 原文，也不据此声称所有技能都在真实局发生。主代理核对的是 session 记录和退出结果。两次真实 Pi 整局合计覆盖预言家、女巫、狼人、村民、白痴和猎人六类角色：3Pi 为 seat1 预言家、seat2 女巫、seat3 狼人；4Pi 为 seat3 狼人、seat5 村民、seat7 白痴、seat8 猎人。4Pi 的猎人选择型 trigger 返回 `PASS`，未出现 `105` 开枪事件；没有白痴自动翻牌的结论，因此不作该结论。

脚本整局的归档复核命令如下，报告位于 `.runtime/cli-ascii-final-20261001.archive.stdout`，返回 `kind=archive`、`status=valid`、86 个文件：

```powershell
uv run werewolf archive verify .runtime/cli-ascii-final-20261001/games/archive/20261001T030726Z_classic-play-001
```

曾有一局真实 Pi 运行到 `day5` 并进入 `FINISHED`，但 `finish` 返回失败且没有 archive；该目录只作为修复背景，不能作为成功 CLI 验收证据。真实 3/4 Pi 必须同时满足最终 JSON、归档 manifest、`archive verify` 和无残留 Pi session 的证据要求。

### 角色和能力覆盖

| 能力 | 已确认的范围 | 本轮不作的结论 |
| --- | --- | --- |
| 女巫毒药 | 冻结 action contract 中 `103 poison` 正确，离线协议和主持器校验覆盖；本次 3Pi action code 未出现 `103` | 不声称真实 LLM 已在整局使用毒药 |
| 女巫解药 | 冻结 action contract 中 `104 heal` 正确，资源和窗口校验覆盖；3Pi 有一条真实 Pi `104` response，表示女巫解药；严格主持器流程整局成功 | 不公开目标或身份信息，不把未出现的技能写作已发生 |
| 猎人 | 冻结 trigger/action contract 中 `105 hunter` 正确，玩家选择窗口可由协议验证；4Pi 曾收到选择型 trigger 并返回 `PASS`，未出现 `105` 开枪事件 | 不声称真实局已发生猎人开枪 |
| 白痴 | 放逐效果由引擎按 `AUTOMATIC` trigger 处理 | 不声称真实局已发生白痴自动翻牌 |


### 当前边界与非目标

- 老板子 `classic_12_seer_witch_hunter_idiot@1.0.0` 已完成真实人工审核并正式发布；`play init` 仍是隔离编译和启动准备步骤，生成的 setup 继续使用 `--experimental-preview` 做候选复测，不会发布知识或改写正式 compiled store。
- 正式运行可使用不含 `play_setup` 的 `game.yaml`，将 `paths.compiled_root` 指向 `vault/compiled`；`play run` 不需要 `--experimental-preview`。
- 本版本正式支持的运行时是 Pi 和确定性的脚本 harness；本轮没有新增角色专属分支。任意板子技能解释器、运行时真正热插拔和 TUI harness 仍未完成，完整任意板子自动生成也仍未完成。
- `classic_resolution` 是老板子范围的有界结算器，依赖冻结的 action contract、effective rules 和 trigger grants；新增板子需要先完成对应知识审核和契约验证。
- 自动入口默认从板子定义的首日合法参与资格中选前两个警长候选，当前没有自主报名/退水模型。
- `pi doctor` 的通过只代表本机可执行文件和版本可用；真实模型认证、额度、网络延迟和多 Pi 全局体验仍需在用户机器上实测。
- 详细的脚本整局、离线 Pi 协议和真实 3/4 Pi 状态见上面的验收矩阵；两次真实 Pi 归档均已校验通过。

## 开发检查

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

测试按 `tests/unit/`、`tests/contract/`、`tests/integration/` 和 `tests/scenarios/` 分层。涉及 Pi 的测试使用进程协议或确定性 runtime；不要把真实 token、私密日志或 `.runtime/` 生成数据加入提交。
