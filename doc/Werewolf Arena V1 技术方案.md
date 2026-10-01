# Werewolf Arena V1 技术方案

> 文档状态：开发基线（2026-09-27 反向评审通过）  
> 对应 PRD：`Werewolf Arena V1 — 多模型 AI 狼人杀框架 PRD v1.0`  
> 技术方案版本：v1.0  
> 编写日期：2026-09-27  
> 运行平台：Windows 本地单机  
> Python：CPython 3.11  
> 项目管理：uv  

---

## 1. 方案结论

V1 采用“单个 Python 主进程 + 每座位一个独立 Pi RPC 子进程 + 一个仅监听回环地址的规则查询 HTTP 服务”的进程模型。

Python 主进程是唯一权威状态写入者，负责状态机、消息授权、行动窗口、基础合法性校验、主持人裁定、存档和公开/私有记录生成。每个 AI 玩家拥有独立 Pi Session，只接收该座位有权看到的增量事件，不接触 `state.json`、其他玩家 Session 或任何共享频道文件。

首个板子选定为网易官方规则页中的“12人标准场”（暗牌、带警长）的项目内候选版本 `classic_12_seer_witch_hunter_idiot@1.0.0`。官方明确的板子规则先落成机器可校验的知识库数据；官方没有覆盖的字段必须保持 `NEEDS_DECISION`，在工作台取得证据或主持人决议并重新发布前不得伪装成冻结值。游戏代码不得散落硬编码某个民间版本。

V1 的三个优先闭环为：

1. 规则快照与游戏内自主查询闭环；
2. 独立 Pi 持久会话与结构化响应闭环；
3. Python 受控消息分发、主持裁定和一致存档闭环。

### 1.1 已冻结的产品/技术决策

| 编号 | 决策 |
|---|---|
| TD-01 | Python 固定为 `>=3.11,<3.12`，使用 uv 管理解释器、虚拟环境、依赖、锁文件和命令执行。 |
| TD-02 | V1 仅实现 `PiRuntime`；测试使用 `ScriptedRuntime`，但它不作为正式玩家 Harness。 |
| TD-03 | 每座位一个 Pi RPC 子进程和一个独立 Session 目录，不共享上下文。 |
| TD-04 | Pi Extension 到 Python `KnowledgeService` 使用 `127.0.0.1` 上的短生命周期 HTTP/1.1，不使用 Socket/WebSocket。 |
| TD-05 | 游戏内状态只在 Python 内存中变更；完整昼夜循环结束自动做正式快照，整局结束再生成时间戳归档。 |
| TD-06 | 白天发言和狼队讨论严格串行；秘密投票及无依赖的夜间行动可以并行收集。 |
| TD-07 | 狼队记录生成 GM 私有 Markdown，但该文件不是玩家通信媒介，也不向 Pi 暴露。 |
| TD-08 | `PAUSED`、`WAITING_GM`、`FAILED` 是运行控制状态，不混入游戏阶段枚举。 |
| TD-09 | 模型输出只有通过请求关联、Schema 校验和行动合法性校验后才能提交到权威状态。 |
| TD-10 | V1 不承诺从最后一条事件恢复；只允许从最近一次完整昼夜快照恢复并重建玩家会话。 |
| TD-11 | 知识库必须包含独立的“规则生产工作台”：输入板子名称即可联网调研、提取规则、对比来源、生成知识包草稿和验证报告。 |
| TD-12 | 联网调研只发生在规则创作/更新阶段；游戏只能查询经过审核发布的不可变快照，不能边玩边联网补规则。 |
| TD-13 | 所有 `GameState` 修改经单一提交器串行化；模型调用、HTTP 和磁盘 I/O 不持锁，提交时用 revision + window/request 状态再次校验。 |
| TD-14 | `kind=action` 的载荷统一为 `actions[]`；动作数量由板子快照的 `min_actions/max_actions` 决定，首板子普通窗口和女巫窗口均为单项，官方规则不启用同夜双药。 |

### 1.2 Pi 0.87.1 已验证技术基线

2026-09-27 已在目标 Windows 环境验证 Pi `0.87.1`：实际安装入口为 `pi.cmd`，RPC `get_state` 可成功启动；CPython 3.11 使用 argv 直接执行 `pi.cmd --version` 返回 0；本机 Pi 自带 TypeScript Extension 通过 `--extension` 可直接加载。`pi --help` 也确认了下列 CLI 开关。实现冻结如下：

- 启动模式：`--mode rpc`，stdin/stdout 使用严格 JSONL；
- 会话隔离：每座位使用独立 `--session-dir` 和 `--session-id`；禁止 `--no-session`；
- 工具隔离：`--no-builtin-tools`，仅通过 `--tools` 开启自研知识工具；
- Extension：`--no-extensions --extension <absolute .ts path>`，显式扩展仍可加载；Pi 使用 `jiti`，V1 不需要单独编译 TypeScript；
- 资源隔离：`--no-skills --no-prompt-templates --no-themes --no-context-files --no-approve`，Werewolf Skill 由 Python 展开进生成的 System Prompt；
- 命令响应：`prompt success=true` 只表示接受；最终等待 `agent_settled`；
- 最终文本：以最后一个 assistant `message_end.message` 为权威，不用 delta 拼接结果做提交；
- `agent_end` 不是最终 settle，自动重试、压缩、steer 或 follow-up 仍可能继续；
- `abort` 会等待 Session idle 后响应；存在队列时先 `clear_queue` 再 `abort`；
- 正常关闭：先关闭 stdin 请求有序退出，超时后才 terminate/kill；Windows 必须使用 Job Object 覆盖整个 Pi/Node 进程树，不能只终止 `pi.cmd` 启动器。

程序不得硬编码当前用户目录。Pi 可执行文件按 `config.runtime.pi.executable`、环境变量 `WEREWOLF_PI_EXECUTABLE`、`shutil.which("pi")` 的优先级解析，并保存规范化绝对路径。适配器先支持 `0.87.x`；其它版本必须通过 `pi doctor` 和协议 contract fixture 后才能放行。

尚未完成的是模型提供商认证：当前 `pi --offline --list-models` 返回无可用模型。开始真实模型联调前，至少配置一个 provider，并通过 `pi auth check --provider <name> --json` 与两轮 RPC Prompt 测试。

---

## 2. 首板子规则基线

本节的主来源是网易《狼人杀-官方正版》规则页中 `App配置名称：12人标准场` 的“暗牌局·本局有警长”段落：<https://langrensha.163.com/wanfa/guize/2017/10/18/26899_719311.html>（检索日期：2026-09-28）。该来源优先级高于其他网络资料。补充资料 <https://www.langrensha.net/strategy/2021050801.html> 仅用于记录未被官方页覆盖的候选流程，不能覆盖或改写官方规则；其关于警长、PK、遗言等内容与官方页发生冲突时，保留为独立变体或 `NEEDS_DECISION`。

### 2.1 板子身份

`classic_12_seer_witch_hunter_idiot@1.0.0`：

- 牌局类型：暗牌局，玩家出局或死亡后不翻开身份牌；
- 狼人阵营：4 名普通狼人；
- 神职阵营：预言家、女巫、猎人、白痴各 1 名；
- 平民阵营：4 名平民；
- 好人阵营 = 神职阵营 + 平民阵营；
- 警长是附加标志，不占角色名额；首日宣布死者前从所有玩家中投票选出，警长白天最后发言，投票按 1.5 票计；
- 狼人获胜条件采用屠边：全部神职死亡，或全部平民死亡，且至少一名狼人存活；
- 好人获胜条件：全部狼人死亡。

所有规则均带 `ruleset_version=1.0.0`。对局创建后复制为只读规则快照；维护 Vault 的后续改动不会影响已开始的对局。

### 2.2 首板子默认细则

下表把官方来源明确的规则与尚未被官方来源覆盖的实现项分开。知识库 `1.0.0` 只有标为“官方”的字段可以直接进入发布候选；标为“未覆盖”或“部分覆盖”的字段必须由工作台继续取证或生成 `NEEDS_DECISION`，不能用“经典规则通常如此”静默补齐。

| 规则项 | 证据状态 | `1.0.0` 目标值 |
|---|---|---|
| 牌局与人数 | 官方 | 暗牌；4 狼、预言家/女巫/猎人/白痴各 1、4 民。 |
| 狼队夜杀 | 官方部分 | 官方只明确狼人每晚可以杀死一人；狼队是否逐人讨论、是否必须一致、空刀和提交时序未说明，保留为 `NEEDS_DECISION`。 |
| 狼队归票 | 系统实现 | `wolf_coordinator_seat` 只能表示一次运行中的提交协调者，不是板子角色或官方规则。固定 seed 的选择可用于稳定收集请求，但不能写入知识库规则。 |
| 刀口形成 | 官方部分 | `WOLF_KILL` 是夜间统一结算输入；团队共识、归票和无一致意见时的处理未被官方页定义，保留为 `NEEDS_DECISION`。 |
| 女巫信息 | 未覆盖 | 官方页没有明确女巫何时、在何种药剂资源状态下获知狼刀；`witch.sees_wolf_target` 及其时序不能直接冻结。 |
| 女巫用药 | 官方 | 女巫有解药和毒药两瓶药；解药救当晚狼刀目标，毒药毒杀一名玩家；每晚最多使用一瓶药；不可自救。 |
| 药剂次数 | 官方语义 | 解药、毒药各一瓶，用过即消耗；同一夜不得同时提交两种药。 |
| 预言家结果 | 官方 | 只返回目标是好人还是狼人：`GOOD` 或 `WEREWOLF`，不返回具体角色。 |
| 猎人开枪 | 官方 | 仅当猎人被狼人杀害或被投票放逐时开枪；女巫毒杀不能开枪。其它死亡原因不因“非毒杀”自动获得资格。 |
| 白痴放逐 | 官方 | 白痴被投票放逐时翻牌并免除本次放逐，继续发言但失去投票权；之后仍需被击杀才死亡。因非投票原因死亡不能发动技能，立即死亡。 |
| 警长 | 官方部分 | 首日宣布死者前由全体玩家竞选投票产生；白天最后发言；投票按 1.5 票计。退选、警长竞选平票、狼人自爆和警徽移交等未被官方页说明，保留为 `NEEDS_DECISION`。 |
| 发言顺序 | 官方部分 | 警长最后发言是官方规则；首日竞选发言起点、警长左右方向、后续轮换和无警长降级路径未被官方页说明，保留为 `NEEDS_DECISION`。 |
| 投票可见性 | 未覆盖 | 官方页没有说明收集期的隐私、公布时点、弃票编码；不得直接冻结现有“先秘密收集后公开明细”假设。 |
| 平票/PK | 未覆盖 | 官方页没有说明平票候选、PK 发言和重投资格；补充资料的 PK 规则只能作为独立低优先级候选，不能直接改写首板子。 |
| 遗言 | 官方 | 夜间死亡只有首夜有遗言；所有白天死亡的玩家都有遗言。 |
| 胜负 | 官方 | 狼人屠边：杀死所有神职或所有平民；好人杀死全部狼人。 |

“大狼”在本文中只表示其他板子定义的特殊狼人角色，不等于 `wolf_coordinator_seat`。本首板子没有大狼；临时提交协调者只承担运行时调度职责，不改变角色、技能、阵营或知识查询结果。

### 2.3 夜间窗口依赖

首板子用数据定义以下逻辑依赖：

```text
狼队夜杀提交
  -> 待结算 `WOLF_KILL`
      -> 女巫窗口（是否可见刀口由已发布字段决定；当前未决）

预言家查验 ---------------------> 夜间统一结算
女巫动作 -----------------------> 夜间统一结算
狼人刀口 -----------------------> 夜间统一结算
```

上图表达运行时依赖，不把狼队讨论、归票或女巫信息误写成官方规则。预言家窗口与狼队收集没有信息依赖，可在代码层并行；V1 为降低主持复杂度暂按 `night_windows` 顺序串行打开。女巫窗口在狼队请求形成后打开是实现候选，只有对应板子字段发布后才可作为硬约束。所有死亡、救治、毒杀均在 `NIGHT_RESOLVE` 由主持人一次确认，行动请求本身不立即修改生死状态。

---

## 3. 总体架构

### 3.1 一级子系统划分

项目不是“游戏引擎附带几篇规则 Markdown”，而是两个并列的一等子系统：

1. **规则知识子系统**：板子调研、证据提取、知识建模、验证发布、版本编译、对局快照、Pi 动态读取；
2. **狼人杀游戏子系统**：身份、阶段、消息、行动、主持裁定、会话和存档。

两个子系统通过不可变 `RulesetPackage/Snapshot` 连接。知识子系统不知道真实座位身份和对局秘密；游戏子系统不能修改规则包，也不能在对局中调用联网调研功能。

知识内容模型、Vault 物理结构、编译索引、启动卡、Reading Skill 和 Pi 工具协议详见独立设计：[Werewolf Arena V1 知识库技术设计](./Werewolf%20Arena%20V1%20知识库技术设计.md)。该文档是知识子系统实现的规范性设计；本方案第 9 节保留跨系统约束和摘要。

### 3.2 进程拓扑

```text
┌──────────────────────── Python 3.11 主进程 ────────────────────────┐
│ ModeratorShell                                                    │
│     │                                                             │
│ GameManager ─ StateMachine ─ ActionValidator ─ Referee            │
│     │                  │                         │                 │
│ MessageRouter          └──── GameState（内存唯一写者）             │
│     │                                             │               │
│ PlayerSessionManager ───── Runtime Registry       │               │
│     │                                             │               │
│ Knowledge HTTP Gateway (127.0.0.1:随机端口)       GameFileStore    │
└─────┬──────────────┬──────────────┬────────────────┬───────────────┘
      │ stdio JSONL  │ stdio JSONL  │ stdio JSONL    │
 ┌────▼─────┐   ┌────▼─────┐   ┌────▼─────┐          │
 │Pi seat 01│   │Pi seat 02│ … │Pi seat 12│          │
 │独立Session│   │独立Session│   │独立Session│          │
 │规则Extension──HTTP───────HTTP──────┘               │
 └──────────┘   └──────────┘   └──────────┘          │
                                                      ▼
                                      active game / snapshots / archive
```

### 3.3 信任边界

- 可信：Python 主进程、项目自研 Pi Extension、人工审核过的规则快照；
- 半可信：Pi RPC 程序及配置；其输出必须校验；
- 不可信：模型文本、公共发言内容、模型填写的座位号/频道/角色、迟到 RPC 事件；
- 私密：真实身份、未公开行动、投票收集过程、狼队消息、GM 裁定中间数据；
- 公开：由 Python 创建且标记为 `PUBLIC` 的已提交事件。

### 3.4 核心不变量

1. 只有 `GameManager.commit_*` 系列方法能修改 `GameState`。
2. 模型输出中的 `player_id`、`channel`、`role_id` 永远不作为授权依据。
3. 一个行动窗口最多提交一个终态结果；`request_id` 和 `window_id` 均需匹配。
4. 已关闭、已取消或旧 `session_epoch` 的回包只能记录诊断，不能提交。
5. 当前串行发言未成功提交前，不创建下一位玩家的请求。
6. 事件可见性在创建时由 Python 固化；路由时再次按座位计算，不接受模型指定 `audience`。
7. 投递事件只有在该次任务成功提交后才推进玩家游标；失败重送必须保持同一事件集合和顺序。
8. `public.md` 只能从公开事件投影生成，不能从完整日志过滤生成。
9. 知识查询只访问当前对局规则快照，不能访问实时游戏状态。
10. 任何文件落盘都采用同目录临时文件、刷新、原子替换；归档在完整校验后才改名为最终目录。

---

## 4. 项目与依赖管理

### 4.1 uv 约定

仓库根目录必须包含：

```text
.python-version     # 内容为 3.11
pyproject.toml
uv.lock
```

初始化和日常命令统一如下：

```powershell
uv python pin 3.11
uv sync --locked
uv run werewolf --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

新增/删除依赖必须通过 `uv add` / `uv remove`，禁止手工维护 `requirements.txt`，禁止在项目环境中直接运行 `pip install`。CI 和正式演示均使用 `uv sync --locked`，确保锁文件没有漂移。

### 4.2 `pyproject.toml` 设计

```toml
[project]
name = "werewolf-arena"
version = "0.1.0"
requires-python = ">=3.11,<3.12"
dependencies = [
  "aiohttp>=3.10,<4",
  "pydantic>=2.9,<3",
  "pyyaml>=6,<7",
  "rich>=13,<15",
  "typer>=0.12,<1",
]

[build-system]
requires = ["hatchling>=1.26,<2"]
build-backend = "hatchling.build"

[project.scripts]
werewolf = "werewolf.cli:app"

[dependency-groups]
dev = [
  "mypy>=1.11,<2",
  "pytest>=8,<9",
  "pytest-asyncio>=0.24,<2",
  "pytest-cov>=5,<8",
  "ruff>=0.8,<1",
]
```

具体补丁版本由 `uv.lock` 冻结。依赖上限用于避免 V1 在未验证情况下跨主版本升级。TypeScript Extension 不引入第三方 npm 依赖：只使用 Pi 提供的 Extension API、TypeBox 和运行时原生 `fetch`。Pi `0.87.1` 已确认通过 `jiti` 直接加载 `.ts`，因此 V1 不需要安装 npm、TypeScript 编译器或额外 Node 项目；Pi 自身及其 Node 运行时属于外部运行依赖。

首次初始化使用 `uv init --package --python 3.11`，再用 `uv add` / `uv add --dev` 加入上表依赖并生成 `uv.lock`；已有锁文件后的日常安装才使用 `uv sync --locked`。不得手工复制一个与实际依赖不一致的 TOML 片段。

---

## 5. 代码与数据目录

```text
Werewolf/
├── .python-version
├── .gitignore
├── pyproject.toml
├── uv.lock
├── README.md
├── config/
│   ├── game.example.yaml
│   ├── players.example.yaml
│   ├── actions.yaml
│   └── logging.yaml
├── prompts/
│   ├── global.md
│   ├── skill.md
│   ├── roles/
│   └── phases/
├── vault/
│   ├── _workbench/             # 调研任务、证据、草稿和冲突报告；不供玩家查询
│   ├── _schemas/               # Board/Role/Mechanic/Interaction 等 Schema
│   ├── published/
│   │   ├── boards/
│   │   ├── roles/
│   │   ├── mechanics/
│   │   ├── interactions/
│   │   ├── glossary/
│   │   ├── sources/
│   │   ├── tests/
│   │   └── manifests/
│   └── compiled/               # 可重建的只读运行包和索引
├── extensions/
│   └── werewolf_knowledge.ts
├── src/werewolf/
│   ├── cli.py
│   ├── moderator_shell.py
│   ├── config.py
│   ├── domain/
│   │   ├── enums.py
│   │   ├── models.py
│   │   ├── responses.py
│   │   └── errors.py
│   ├── game/
│   │   ├── manager.py
│   │   ├── state_machine.py
│   │   ├── scheduler.py
│   │   ├── message_router.py
│   │   ├── action_validator.py
│   │   └── referee.py
│   ├── knowledge/
│   │   ├── models.py
│   │   ├── loader.py
│   │   ├── repository.py
│   │   ├── compiler.py
│   │   ├── indexes.py
│   │   ├── reading_plan.py
│   │   ├── bootstrap.py
│   │   ├── snapshot.py
│   │   ├── service.py
│   │   ├── receipts.py
│   │   └── http_gateway.py
│   ├── ruleset_workbench/
│   │   ├── models.py
│   │   ├── orchestrator.py
│   │   ├── search.py
│   │   ├── fetcher.py
│   │   ├── extractor.py
│   │   ├── conflict_analyzer.py
│   │   ├── generator.py
│   │   ├── validator.py
│   │   ├── publisher.py
│   │   └── providers/
│   │       ├── base.py
│   │       ├── agent_reach.py
│   │       └── evidence_bundle.py
│   ├── runtime/
│   │   ├── base.py
│   │   ├── manager.py
│   │   ├── pi_rpc.py
│   │   ├── pi_protocol.py
│   │   ├── pi_doctor.py
│   │   └── scripted.py
│   ├── prompts/
│   │   ├── composer.py
│   │   └── observation.py
│   └── persistence/
│       ├── store.py
│       ├── renderer.py
│       └── recovery.py
├── games/
│   ├── active/
│   └── archive/
├── .runtime/
│   └── players/
└── tests/
    ├── unit/
    ├── contract/
    ├── integration/
    ├── scenarios/
    └── fixtures/
```

模块不得反向依赖：`domain` 不依赖其他业务包；`game` 只依赖 `domain` 和抽象接口；`runtime/pi_*` 可以依赖 Pi 协议实现，但 `GameManager` 不得导入它；`persistence` 不参与规则判定。

仓库卫生是开工 P0：提交 `.python-version`、`pyproject.toml`、`uv.lock`、源码、测试、已发布知识和必要的脱敏调研产物；`.gitignore` 至少排除 `.venv/`、`.runtime/`、`games/active/`、`games/archive/`、运行日志、`.env`、覆盖率/测试缓存、Python 缓存及可重建的 `vault/compiled/`。座位 System Prompt、Pi Session、HTTP token、原始 RPC debug、真实身份和 GM/狼队记录只能落在这些不入库目录。若需要共享一局，使用显式脱敏导出流程，不能直接提交运行目录。

---

## 6. 领域模型与协议

### 6.1 通用约定

- 所有模型使用 Pydantic v2，`extra="forbid"`；
- 所有枚举落盘使用稳定大写字符串；
- 时间戳使用带时区的 RFC 3339 UTC；目录名使用 Windows 安全格式 `YYYYMMDDTHHMMSSZ`；
- `event_id`、`state_revision` 使用单局递增整数；
- 跨进程标识统一使用 Python 3.11 标准库 UUIDv4；需要排序时依赖独立时间戳和单局序号，不自行实现 UUIDv7；
- `schema_version` 独立于应用版本，V1 为 `1`；
- V1 只读取 `schema_version=1`；遇到其它版本明确拒绝并提示迁移，不在加载时做隐式兼容或静默丢字段；
- 座位号从 1 开始，内部仍使用 `SeatNo` 值对象校验 `1..seat_count`。
- `game_id`、board/role/package ID 只允许 ASCII 小写字母、数字、短横线和下划线，并设置长度上限；所有由 ID 派生的路径都必须 `resolve()` 后验证仍位于预期根目录内，不能把配置值直接拼成路径。

### 6.2 关键枚举

```python
class GamePhase(StrEnum):
    CREATED = "CREATED"
    RULESET_READY = "RULESET_READY"
    ASSIGNED = "ASSIGNED"
    PLAYER_PREPARE = "PLAYER_PREPARE"
    NIGHT_TEAM_CHAT = "NIGHT_TEAM_CHAT"
    NIGHT_ACTION = "NIGHT_ACTION"
    NIGHT_RESOLVE = "NIGHT_RESOLVE"
    DAY_ANNOUNCE = "DAY_ANNOUNCE"
    DAY_SPEECH = "DAY_SPEECH"
    VOTE = "VOTE"
    VOTE_PK_SPEECH = "VOTE_PK_SPEECH"
    VOTE_PK = "VOTE_PK"
    DAY_RESOLVE = "DAY_RESOLVE"
    TRIGGER_ACTION = "TRIGGER_ACTION"
    VICTORY_CHECK = "VICTORY_CHECK"
    FINISHED = "FINISHED"

class RunStatus(StrEnum):
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_GM = "WAITING_GM"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    CLOSED = "CLOSED"

class Channel(StrEnum):
    PUBLIC = "PUBLIC"
    TEAM = "TEAM"
    PRIVATE = "PRIVATE"
    GM_ONLY = "GM_ONLY"
```

阶段和运行状态分离后，任意游戏阶段均可暂停或等待主持人，不需要创建大量 `PAUSED_DURING_*` 状态。

### 6.3 `GameState`

```text
GameState
├── schema_version: int
├── game_id: str
├── state_revision: int
├── created_at / updated_at: datetime
├── phase: GamePhase
├── run_status: RunStatus
├── round_no: int
├── day_no: int
├── ruleset: RulesetRef
│   ├── board_id / version
│   ├── snapshot_id
│   └── manifest_sha256
├── rng: RandomStateRef
│   ├── seed
│   └── draw_count
├── players: dict[SeatNo, PlayerState]
├── events: list[GameEvent]
├── delivery_cursors: dict[SeatNo, DeliveryCursor]
├── current_queue: TurnQueue | None
├── action_windows: dict[WindowId, ActionWindow]
├── action_requests: dict[RequestId, ActionRequest]
├── resolutions: list[ActionResolution]
├── pending_resolution: ResolutionDraft | None
├── knowledge_receipts: list[KnowledgeReceipt]
├── vote_state: VoteState | None
├── moderator_audit: list[ModeratorOperation]
├── winner: VictoryResult | None
└── last_snapshot: SnapshotRef | None
```

`PlayerState` 至少包含：座位、真实角色、阵营、存活状态、死亡原因、当前投票权、技能资源、Runtime 引用、`session_epoch`、准备阶段查询证明、当前请求、已确认事件游标。真实身份字段永远不进入公开投影。

### 6.4 游戏事件

```python
class GameEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    event_id: int
    game_id: str
    state_revision: int
    round_no: int
    phase: GamePhase
    created_at: datetime
    event_type: EventType
    channel: Channel
    actor_seat: int | None
    audience: tuple[int, ...]       # 由 Python 固化
    payload: EventPayload           # 判别联合类型
    public_projection: PublicPayload | None
    correlation_id: str | None
```

事件创建规则：

- `PUBLIC`：`audience` 是事件创建时有资格接收的全部座位；死亡玩家是否继续旁观由板子策略决定；
- `TEAM`：`audience` 来自 `TeamMembershipPolicy`，玩家不能传入队伍 ID；
- `PRIVATE`：通常只有一个座位；
- `GM_ONLY`：`audience=()`，只供主持状态、审计和私有 Markdown；
- 同一事实需要不同可见内容时创建不同事件，例如预言家查验请求为 GM_ONLY，查验结果为该预言家 PRIVATE；
- 不允许先创建完整敏感事件再靠渲染器删字段。

### 6.5 玩家请求与响应

发送给 Runtime 的内部请求：

```python
class TurnRequest(BaseModel):
    schema_version: Literal[1]
    request_id: str
    logical_request_id: str
    attempt_no: int
    game_id: str
    session_epoch: int
    phase: GamePhase
    expected_kind: ResponseKind
    action_window: ActionWindowView | None
    observation: Observation
    output_schema: dict[str, Any]
    deadline: Deadline
```

模型最终输出统一为：

```json
{
  "schema_version": 1,
  "request_id": "g001-r1-vote-seat04-a1",
  "kind": "action",
  "speech": null,
  "actions": [
    {
      "action_code": 201,
      "targets": [7],
      "parameters": {},
      "reason_public": null
    }
  ],
  "ready": null,
  "error": null
}
```

响应使用判别联合：`speech`、`actions`、`ready`、`error` 只能出现一个。`kind=action` 时 `actions` 至少一项；动作数量由板子快照的 `min_actions/max_actions` 校验。首板子普通窗口和女巫药剂窗口均为单项，跳过则显式提交单项 `PASS`，不能用空数组表达；其它板子若允许多动作，必须由其已发布快照显式开启。模型可以回填 `request_id`，但服务器仍以“当前给该 Runtime 的待完成请求”绑定座位和 Session；任何不匹配均拒绝。

`ready` 必须附带 `knowledge_receipts`，证明当前 Session 在本次准备阶段实际成功调用过当前板子的 `get_board` 和本人角色的 `get_role`。证明由 Extension 返回并由 HTTP 服务记录，不能只相信模型自报。

### 6.6 行动窗口与三段式生命周期

```text
ActionWindow OPEN
  -> Player action bundle REQUESTED
  -> Python basic validation VALIDATED / REJECTED
  -> GM resolution CONFIRMED / OVERRIDDEN / CANCELLED
  -> Authorized result events PUBLISHED
  -> ActionWindow CLOSED
```

`ActionRequest` 记录玩家“想做什么”，其中含一个不可变 `requested_actions` 元组；`ActionResolution` 逐项记录主持人确认“实际发生什么”，并带同一 bundle ID；`GameEvent` 记录“谁被允许知道什么”。三者不得复用同一对象。若某个未来板子允许同一窗口提交多个动作，bundle 必须整体通过基础校验后才进入待裁定状态，任一项非法则整个请求拒绝，避免只提交半份意图；首板子女巫窗口不启用多动作。

`ActionWindow` 必须包含：允许角色/座位、阶段、行动码集合、`min_actions/max_actions`、同一 action code 是否可重复、跨动作目标约束、是否允许 PASS、打开和关闭时间、依赖的已确认 Resolution、可向玩家透露的上下文、是否允许并发、每座位最大提交数。首板子的女巫窗口为 `min_actions=1,max_actions=1`，允许 `WITCH_HEAL`、`WITCH_POISON` 或单独 `PASS`；`WITCH_HEAL` 与 `WITCH_POISON` 不能在同一夜同时提交。其它板子的多动作上限必须由其规则快照单独定义。

---

## 7. 状态机与调度

### 7.1 状态转移

所有转移由显式表驱动，非法转移抛出 `InvalidTransition`：

```text
CREATED
  -> RULESET_READY
  -> ASSIGNED
  -> PLAYER_PREPARE
  -> NIGHT_TEAM_CHAT
  -> NIGHT_ACTION
  -> NIGHT_RESOLVE
  -> DAY_ANNOUNCE
  -> DAY_SPEECH
  -> VOTE
       -> VOTE_PK_SPEECH -> VOTE_PK   （仅首次平票）
  -> DAY_RESOLVE
       -> TRIGGER_ACTION              （按需：猎人等）
  -> VICTORY_CHECK
       -> FINISHED
       -> NIGHT_TEAM_CHAT             （下一完整昼夜）
```

每个转移定义 `guard`、`on_exit`、`on_enter`：

- `guard` 只检查是否满足完成条件，不修改状态；
- `on_exit` 关闭旧窗口、提交已确认事件；
- `on_enter` 创建下一阶段队列/窗口，但不自动替主持人做复杂裁定；
- 任何异常不得半途更新 `phase`，先在状态副本上构造 `StatePatch`，验证后一次提交并递增 `state_revision`。

`GameManager` 内部只有一个状态提交入口和一个 `asyncio.Lock`。Runtime、Gateway、CLI 输入和文件写入都在锁外等待；进入提交临界区后按 `base_revision`、`session_epoch`、request/window 是否仍有效重新校验，再由纯 reducer 生成并一次替换新状态。并发投票回包因此可以并行等待模型，但 ballot 合并、游标确认、Resolution 和阶段切换必须逐个串行提交。revision 不匹配时重新读取最新状态并重做校验，不能覆盖别的任务刚提交的票。

### 7.2 串行发言调度器

`SerialTurnScheduler` 同时用于公屏和狼队：

1. 根据板子/主持人输入生成不可变 `TurnQueue`；
2. 取队首座位并创建唯一 `TurnRequest`；
3. `MessageRouter.peek_delivery(seat)` 返回授权且未确认的增量事件；
4. Runtime 成功返回并通过 Schema 校验；
5. 对于 speech，由 Python 创建相应频道事件；
6. 在同一个内存提交中确认事件投递游标、提交发言、完成队首；
7. 之后才能激活下一位。

如果失败、超时或主持人暂停，队首保持不变。`retry` 创建新的物理 `request_id` 和递增的 `attempt_no`，但沿用 `logical_request_id`，旧请求进入 `SUPERSEDED`，迟到回包不可提交。

### 7.3 狼队调度

- 成员资格由 `TeamMembershipPolicy` 根据真实角色、存活状态、板子可见关系计算；
- 讨论队列按座位升序；
- 第 N 位只收到自己此前已确认的公共/私有增量，以及当前夜第 1..N-1 位已提交的狼队发言；
- 所有狼人发言后再收集刀口偏好；
- `wolf_coordinator_seat` 是运行时提交协调机制，使用 `random.Random(seed)` 的项目级封装选择并写入 GM 审计；它不是官方板子角色，也不代表官方要求随机归票；
- 只有在已发布板子字段确认团队偏好、共识和最终目标规则后，协调者才可提交最终目标；未决时只能生成候选请求并等待主持人；
- 狼队频道关闭后不再接受该窗口请求。

### 7.4 投票调度

首板子官方页没有规定投票收集可见性、弃票编码或平票 PK；以下是保持状态一致性的运行时候选实现，只有在 `投票可见性` 和 `平票/PK` 字段取得来源或主持人决议后才能作为首板子规则发布。

秘密投票采用“观察快照屏障”：

1. 打开投票窗口时生成 `observation_revision`；
2. 为每名投票者从同一公开事件上界生成观察；
3. 并发调用 Runtime，但不发布任何投票事件；
4. 每票独立验证后写入内存 `VoteState.ballots` 的私有区域；
5. 全部完成或主持人处理超时后，锁定窗口；
6. 统计并等待主持人确认；
7. 一次性创建包含明细和合计的 PUBLIC 结果事件。

若已发布的板子规则启用 PK，则创建新的 PK 发言队列和投票窗口；候选是否重投、再次平票如何处理必须由该板子字段决定。补充资料提出的“候选不能重投、二次平票无人放逐”只作为低优先级候选，不能冒充官方 12 人标准场规则。白痴被放逐时产生 `IDIOT_REVEALED` 结果而非死亡结果，并更新其 `can_vote=False`。

---

## 8. 消息权限与增量同步

### 8.1 可见性矩阵

| 数据/事件 | PUBLIC | TEAM | PRIVATE | GM_ONLY |
|---|---:|---:|---:|---:|
| 白天公告、已确认公屏发言 | 是 | - | - | GM 同时可见 |
| 投票收集中的单票 | 否 | 否 | 可选仅回执本人 | 是 |
| 投票结束后的明细/合计 | 是 | - | - | 是 |
| 狼队发言、偏好、归票过程 | 否 | 授权狼人 | 否 | 是 |
| 预言家请求目标 | 否 | 否 | 可回执本人 | 是 |
| 预言家阵营结果 | 否 | 否 | 仅预言家 | 是 |
| 女巫所见刀口 | 否 | 否 | 仅女巫 | 是 |
| 未结算夜间行动 | 否 | 仅规则明确允许者 | 行动本人 | 是 |
| 真实身份、完整资源状态 | 否 | 否 | 只给本人必要摘要 | 是 |
| 主持人裁定理由 | 默认否 | 否 | 按板子生成摘要 | 是 |

### 8.2 游标模型

每位玩家不是只持有一个简单最大 `event_id`，而是持有：

```text
DeliveryCursor
├── committed_event_id
├── in_flight_request_id
├── in_flight_event_ids
└── session_epoch
```

`peek_delivery` 不推进游标；只有任务成功提交后执行 `ack_delivery`。重试会收到同一批事件，Prompt 中的每条事件包含稳定 `event_id`，并要求模型将其视为重送而非新事实。切换/重建 Session 时递增 `session_epoch`，旧 Session 的任何结果作废。

### 8.3 Prompt 注入防护

- 固定 System 指令明确：`<player_message>` 中内容都是游戏内不可信声明；
- Observation 采用结构化标签包裹事件，不把玩家文本拼到系统指令段；
- 工具端不根据自然语言授权，只根据 HTTP token 对应的游戏/座位/规则快照授权；
- 模型无 Shell、任意读文件、任意写文件、任意 HTTP 工具；只加载规则查询工具；
- 公屏中的“忽略系统”“读取某路径”等内容允许作为游戏发言保存，但不会改变工具能力。

---

## 9. 规则知识生产、发布与游戏内查询

> 本节描述跨系统主流程。知识实体、目录、Schema、有效角色合成、索引、启动与重读协议的完整规范见 [Werewolf Arena V1 知识库技术设计](./Werewolf%20Arena%20V1%20知识库技术设计.md)。

### 9.1 目标与边界

知识系统分为两个权限和生命周期完全不同的平面：

| 平面 | 用途 | 是否联网 | 是否可写 Vault | 使用者 |
|---|---|---:|---:|---|
| 规则生产平面 `RulesetWorkbench` | 根据板子名调研、提取、对比、生成和发布知识 | 是 | 只能写工作区；发布需确认 | 主持人/规则维护 Agent |
| 对局消费平面 `KnowledgeService` | 查询已发布规则快照 | 否 | 否 | AI 玩家 |

正常使用路径应当是：主持人只提供“预女猎白”“狼美人骑士”等自然语言板子名，规则生产工作台负责把常见规则查全。主持人不需要先回答整套标准规则；只有多个可信来源存在实质冲突、无法判断所需变体时，系统才生成少量、带证据的定向问题。

规则生产工作台不是自动裁判，也不能直接修改正在进行对局的快照。任何联网内容都先落入隔离工作区并视为不可信输入。

### 9.2 端到端入库流程

```text
板子名称/别名
  -> 需求规范化
  -> 多来源搜索与网页取证
  -> 原始证据归档
  -> 原子规则主张提取
  -> 同义合并与版本/变体聚类
  -> 规则冲突分析
  -> 板子完整性覆盖检查
  -> 生成 Vault 草稿包
  -> 交叉验证与知识问答测试
  -> 主持人查看差异/处理剩余冲突
  -> 发布不可变版本
  -> 对局创建规则快照
```

各步骤定义如下：

1. **规范化**：把用户输入映射为候选规范名、常见别名、人数和可能的角色组合；不能仅靠名称猜测后直接发布。
2. **搜索计划**：根据覆盖矩阵生成查询，包括板子组成、角色技能、夜间顺序、狼队可见性、投票/PK、遗言、胜负、特殊交互和常见变体。
3. **多来源取证**：搜索至少 3 个独立来源；关键规则至少需要 2 个独立来源支持，或 1 个明确标识为权威/官方的来源。
4. **证据固化**：保存 URL、标题、发布者、访问时间、内容摘要、引用片段、内容哈希和来源类型；不只保存 LLM 总结。
5. **主张提取**：把网页自然语言拆成可比较的原子主张，例如 `witch.can_self_heal=false`，每条主张必须回链到证据片段。
6. **变体聚类**：把明确属于不同赛事、平台或年份的规则拆为不同候选变体，不采用“多数票”把互斥规则混成一个板子。
7. **冲突分析**：同一个规则键存在不同值时，按照来源权威性、版本适用性、发布时间和板子上下文判断；无法消解则进入 `needs_decision`。
8. **草稿生成**：一次生成 board、role、mechanic、interaction 文档及来源清单，机器字段与说明正文保持一致。
9. **静态验证**：检查 Schema、引用闭合、角色数量、行动窗口、权限、胜负条件和必须字段覆盖率。
10. **语义验证**：从生成的知识包自动生成规则问答和情境测试，确保“女巫看到什么”“猎人何时不能开枪”等答案能从已引用规则中得到。
11. **人工确认**：主持人查看来源摘要、冲突报告和发布 diff；只需处理仍未解决的规则键，不重新回答已充分验证的常见规则。
12. **发布**：将通过验证的草稿复制到正式 Vault，分配不可变语义版本并生成 manifest；已发布版本禁止原地修改。

### 9.3 调研任务状态机

```text
CREATED
  -> SEARCHING
  -> EVIDENCE_COLLECTED
  -> EXTRACTING
  -> ANALYZING
  -> DRAFTED
  -> VALIDATING
      -> NEEDS_DECISION   （存在阻塞冲突/缺项）
      -> READY_TO_PUBLISH
  -> PUBLISHED

任一步骤可进入 FAILED；修复后从最近的完整产物继续。
```

`PUBLISHED` 是终态。更新规则必须创建新的 research job 和新版本，不允许把旧版本退回草稿状态。

### 9.4 工作区产物

每次调研使用独立时间戳任务目录：

```text
vault/_workbench/20260927T151505Z_classic-12/
├── request.json                 # 原始板子名、语言、地区、约束
├── search-plan.json             # 搜索主题与覆盖矩阵
├── sources/
│   ├── index.json               # 来源元数据、哈希、可信度类别
│   └── <source-id>.md           # 必要摘录，不保存无关页面内容
├── claims.jsonl                 # 每行一个带证据的原子规则主张
├── variants.json                # 候选变体及其主张集合
├── conflicts.json               # 冲突键、双方证据和推荐处理
├── coverage.json                # 必须规则维度的覆盖情况
├── draft/
│   ├── boards/
│   ├── roles/
│   ├── mechanics/
│   └── interactions/
├── validation-report.md
├── decision-requests.md         # 只列真正需要主持人决定的项目
└── publish-manifest.json
```

`_workbench` 不在 `KnowledgeService` 的搜索根目录内，玩家不能查询草稿、网页内容或冲突报告。

### 9.5 证据与规则主张模型

```python
class SourceEvidence(BaseModel):
    source_id: str
    url: AnyHttpUrl
    title: str
    publisher: str | None
    source_class: SourceClass
    published_at: datetime | None
    fetched_at: datetime
    content_sha256: str
    excerpt: str
    retrieval_method: str

class RuleClaim(BaseModel):
    claim_id: str
    ruleset_candidate_id: str
    key: str                    # 例如 witch.can_self_heal
    value: JsonValue
    scope: ClaimScope           # board/role/mechanic/interaction
    conditions: dict[str, JsonValue]
    evidence_ids: list[str]
    confidence: float
    extraction_note: str | None
    status: ClaimStatus         # supported/conflicting/unverified/rejected
```

来源优先级只是冲突分析输入，不可机械覆盖：

1. 明确对应目标版本的官方/赛事规则；
2. 平台发布的完整板子规则；
3. 有版本和上下文的成熟规则说明；
4. 多个独立社区说明；
5. 攻略、复盘、短视频口述和无来源转述。

攻略里的策略性建议不能提取为硬规则。搜索排名、网页重复转载和同一原文的多个镜像不能算独立来源。

### 9.6 完整性覆盖矩阵

工作台在允许发布前必须为每个板子验证：

- 基本信息：人数、角色组成、阵营；
- 胜负：屠边/屠城、平局和特殊胜利条件；
- 开局：首夜、身份相认和特殊信息；
- 夜间：窗口顺序、依赖、每个角色的目标与次数；
- 狼队：成员可见关系、讨论、刀口形成和特殊狼规则；
- 女巫：刀口信息、自救、同夜用药上限、同目标限制和药剂次数；
- 预言家：结果粒度及特殊身份显示；
- 猎人：可/不可开枪的死亡原因；
- 白痴等触发角色：触发、存活、投票权、发言权；
- 白天：发言顺序、警长流程及其未覆盖的退选/平票/警徽处置字段；
- 投票：公开时点、弃票、平票、PK、重投资格；
- 死亡：遗言、死亡公布、多个死亡的顺序；
- 交互：至少覆盖板子内所有主动技能两两可能影响的组合；
- 来源：所有机器执行字段至少有一条有效证据或明确的人工决议。

缺少关键维度时只能生成 `NEEDS_DECISION` 草稿，不能以“经典规则通常如此”自动填空并发布。

### 9.7 何时询问主持人

只有下列情况才询问：

- 同名板子对应多个实质不同且都常用的变体，用户没有给平台/赛事/年份；
- 两个同等级可信来源对关键规则冲突，且版本信息无法解释；
- 搜索后关键覆盖项仍无证据；
- 用户要求的规则与选定来源明确不一致；
- 发布会覆盖已有规范 ID，但无法确定应升 major、minor 还是另建变体。

问题必须包含“冲突键、候选值、各自来源、推荐选择及影响”，而不是把整套规则重新问给用户。例如应问“女巫首夜是否可自救：来源 A 表示不可，来源 B 的某赛事版本允许；建议按 A 建经典变体，是否确认？”。

### 9.8 工作台 CLI 与可插拔 Provider

```powershell
uv run werewolf rules research "预女猎白" --locale zh-CN --min-sources 3
uv run werewolf rules status <job_id>
uv run werewolf rules report <job_id>
uv run werewolf rules decide <job_id> --claim-key witch.can_self_heal --value false --reason "采用经典变体"
uv run werewolf rules validate <job_id>
uv run werewolf rules publish <job_id> --id classic_12_seer_witch_hunter_idiot --version 1.0.0
uv run werewolf rules refresh classic_12_seer_witch_hunter_idiot@1.0.0
```

搜索和页面读取通过接口隔离：

```python
class RuleResearchProvider(Protocol):
    async def search(self, query: SearchQuery) -> list[SearchHit]: ...
    async def fetch(self, url: str) -> FetchedDocument: ...

class RuleSynthesisProvider(Protocol):
    async def extract_claims(self, evidence: list[SourceEvidence]) -> list[RuleClaim]: ...
    async def generate_draft(self, bundle: ResearchBundle) -> DraftRuleset: ...
```

V1 的首个合成实现为 `PiRuleSynthesisProvider`：使用独立的、非玩家 Pi Session。Python 将经过大小限制的证据摘录分批发送给该 Session，要求返回严格 `RuleClaim`/`DraftRuleset` JSON；该 Session 不读取游戏状态、不复用玩家会话，也不拥有发布权限。Python 必须验证引用片段确实存在于对应 evidence、所有 claim 引用闭合，再接受 LLM 输出。

搜索/抓取默认走 Agent Reach/HTTPS Provider，并支持导入由当前编码 Agent 生成的标准 `ResearchBundle`。传输优先 HTTP/HTTPS；只有 HTTP 无法满足工具要求时才允许其他传输。搜索 Provider、Pi 合成 Runtime 和 Publisher 三者权限分离，均不得依赖 `GameManager` 或读取进行中的游戏状态。

目标 Windows 宿主机已安装 Agent Reach；它需要网络和用户配置访问权，因此生产适配器必须在宿主机/获准的非沙箱执行上下文运行，并支持配置绝对 executable。Codex 沙箱内出现 `uv trampoline`、PATH 或网络错误只说明沙箱受限，不能据此把宿主安装标记为不可用。每个调研任务启动前运行 `agent-reach doctor --json`，保存不含凭证的 capability/active-backend 摘要；实际搜索仍优先选择 HTTP/HTTPS 后端。

如果联网 Provider 未配置，`rules research` 应明确报错并允许 `rules import-evidence <bundle>`，不能悄悄退化成无来源的 LLM 常识生成。

### 9.9 发布门禁和版本策略

发布必须同时满足：

- `coverage.required == 100%`；
- 角色总数等于板子人数；
- 所有引用 ID/版本闭合；
- 所有关键 claim 为 `supported` 或有明确 `human_decision`；
- 不存在未处理的阻塞冲突；
- 机器字段与正文语义一致性检查通过；
- 自动生成的知识问答和场景测试通过；
- 发布 diff 经主持人确认。

语义版本规则：规则效果变化升 minor/major；仅补来源或不改变含义的文字修正可升 patch。已经被对局引用的版本绝不原地改写。`rules refresh` 只生成新草稿和差异报告，不自动发布。

正式文档的唯一终态字段为 `status: published`；“已人工审核”由 `reviewed_by/reviewed_at` 表达，不再使用 `status: reviewed` 这一第二套终态。Publisher 先写同父目录 staging，重新加载并校验完整依赖闭包后再原子改名；目标 ID/version 已存在时必须比较哈希并拒绝覆盖。编译逻辑哈希只包含规范化内容（UTF-8、LF、排序键和稳定相对路径），不包含编译时间、绝对路径或临时目录；易变构建信息单独记录且不参与 package identity。

### 9.10 联网内容安全

- 网页文本是不可信数据，网页中的“忽略指令、执行命令、读取文件”等内容不得成为 Agent 指令；
- fetcher 只允许 HTTP/HTTPS，限制重定向次数、响应大小、MIME 类型和超时，阻止访问回环、局域网、`file://` 及云元数据地址；
- 来源摘录与系统 Prompt 分隔，提取 Agent 没有发布权限；
- 搜索/抓取凭证不写入 evidence、日志或 Vault；
- 正式 Vault 只能由 `RulesetPublisher` 在门禁通过且主持人确认后写入；
- 游戏开始后完全禁用 research provider，只挂载规则快照的只读查询接口。

### 9.11 机器规则与说明文字分层

每个板子 Markdown 的 Frontmatter 存放机器需要执行/校验的规则，正文存放模型可阅读的规则说明。关键流程不能只存在自然语言正文。

板子 Frontmatter 最少字段：

```yaml
schema_version: 1
kind: board
id: classic_12_seer_witch_hunter_idiot
name: 12人预女猎白经典局
version: 1.0.0
aliases: [预女猎白, 经典12人局]
status: published
reviewed_by: GM
reviewed_at: 2026-09-27
sources:
  - evidence_id: src-001
    claim_set: classic-12-core
seat_count: 12
roles:
  werewolf: 4
  villager: 4
  seer: 1
  witch: 1
  hunter: 1
  idiot: 1
victory_mode: eliminate_side
vote:
  visibility_during_collection: secret
  reveal_after_close: ballots_and_totals
  tie_policy: pk_then_no_exile_on_retie
night_windows:
  - wolf_team_chat
  - wolf_kill
  - seer_inspect
  - witch_action
  - night_resolve
```

角色文件定义阵营、能力、查询别名和适用板子；`interactions/` 明确定义女巫与猎人、白痴与放逐等跨角色规则。`ActionValidator` 使用经过解析的快照模型，而不是重新从 Markdown 正文做关键词推断。

### 9.12 快照创建

`KnowledgeSnapshotBuilder.create(board_ref)`：

1. 解析目标板子；
2. 递归收集板子引用的角色、机制、交互；
3. 校验 ID 唯一、版本兼容、`status=published`、引用闭合；
4. 拒绝存在同 ID 同版本不同内容的冲突；
5. 将所需文件复制到 `games/active/<game_id>/ruleset/`；
6. 生成排序稳定的 `manifest.json`，包含相对路径、SHA-256、文档 ID/版本；
7. 对 manifest 本身计算 `manifest_sha256`；
8. 游戏后续查询只绑定此目录和摘要。

不复制整个 Vault，避免无关或未审核草稿进入查询范围。

### 9.13 查询接口

`KnowledgeService` 提供纯 Python 方法：

```python
get_board(snapshot_id, board_id) -> KnowledgeResult
get_role(snapshot_id, role_id) -> KnowledgeResult
get_mechanic(snapshot_id, mechanic_id) -> KnowledgeResult
get_interaction(snapshot_id, subject_ids, situation) -> list[KnowledgeResult]
get_rule_topic(snapshot_id, topic_id) -> KnowledgeResult
search_rules(snapshot_id, query, *, kinds, limit=8) -> list[KnowledgeHit]
```

精确查询先匹配规范 ID，再匹配唯一 alias；别名歧义返回 `AMBIGUOUS`，不随机选。V1 搜索使用规范化字符串、标题、别名、标签和正文子串评分，不引入向量数据库。所有结果返回文档 ID、标题、版本、摘录、来源引用和快照摘要；不得返回原 Vault 绝对路径。

### 9.14 HTTP Gateway

主进程启动后绑定 `127.0.0.1:0`，由操作系统分配端口。禁止绑定 `0.0.0.0`。为每个座位生成高熵 bearer token，只通过该 Pi 子进程环境变量传入：

```text
WEREWOLF_KNOWLEDGE_BASE_URL=http://127.0.0.1:<port>/v1
WEREWOLF_KNOWLEDGE_TOKEN=<seat-scoped-random-token>
```

接口：

```text
GET  /v1/board/{board_id}
GET  /v1/role/{role_id}
GET  /v1/mechanic/{mechanic_id}
GET  /v1/topic/{topic_id}
POST /v1/search
POST /v1/interactions/query
GET  /v1/health
```

除仅返回 `{status, schema_version}` 且不产生 receipt 的 `/health` 外，其余接口都要求 bearer token。token 在服务端映射到固定 `game_id + snapshot_id + seat + session_epoch`。token 的有效期覆盖当前 Session，不能在 active turn 中途自然过期；Session 重建时先签发新 token，再启动新进程，确认旧进程退出后立即撤销旧 token。客户端不能请求另一个快照，也没有任何游戏状态、玩家身份列表或消息接口。请求体限制 8 KiB，结果限制 64 KiB，默认 `limit<=8`，记录工具名、耗时、结果文档 ID 和 receipt，但不记录敏感 Prompt。关闭游戏时先关闭玩家进程，再撤销 token 并停止 HTTP 服务。

### 9.15 开局检索证明

Extension 每次成功调用返回不可伪造的 `receipt_id`；服务端将 tool、canonical document ID、snapshot ID、seat 和 session epoch 写入 `GameState.knowledge_receipts` 的私有区，token 本身不落盘。玩家提交 ready 时，Python 检查该座位在当前 `session_epoch` 是否存在：

- 当前 `board_id` 的 `get_board` receipt；
- 该座位真实 `role_id` 的 `get_role` receipt。

角色 ID 只通过该座位初始化 Prompt 告知；知识服务的 `get_role` 可以查询规则角色，但永远不能回答“几号是什么身份”。

### 9.16 Pi 启动卡、必读与游戏中重读

Session 初始化时，Python 从本局知识快照生成 `KnowledgeBootstrapCard`，明确发送：板子 ID/版本/名称和简要介绍、该玩家角色 ID/名称和简要身份卡、`snapshot_id`、必读工具调用以及“遗忘或不理解时重新读取”的规则。启动卡只做导航，不把整库一次性塞进上下文。

准备阶段固定流程：

```text
收到启动卡
  -> get_board() 读取板子介绍、组成、胜负、流程和 ReadingPlan
  -> get_role(本人角色) 读取当前板子合成后的 EffectiveRoleProfile
  -> 按 recommended_next_reads 选择机制/交互
  -> 返回 ready
  -> Python 校验服务端 receipt
```

每次决策 Prompt 都重复稳定的板子/角色引用和知识重读提示。Pi 在后续发言、投票和技能窗口可随时调用：`get_board`、`get_role`、`get_mechanic`、`get_interaction`、`get_rule_topic`、`search_rules`。重读是只读、幂等操作，不消耗游戏资源，不修改状态；所有结果始终绑定当前局 `snapshot_id`。

`get_role` 返回的是“基础角色 + 当前板子覆盖”合成的有效角色规则，避免女巫自救、同夜用药上限等跨板子差异被通用说明覆盖。玩家可以阅读当前板子中任意角色的公开规则，但不能查询某座位实际是什么身份。

---

## 10. Runtime 与 Pi RPC 适配

### 10.1 抽象接口

```python
class PlayerRuntime(Protocol):
    async def start(self, config: RuntimeConfig, context: InitialContext) -> RuntimeRef: ...
    async def run_turn(self, request: TurnRequest) -> RuntimeTurnResult: ...
    async def steer(self, request_id: str, message: str) -> None: ...
    async def abort(self, request_id: str) -> None: ...
    async def close(self, reason: str) -> None: ...
    def get_session_ref(self) -> RuntimeRef: ...
```

Runtime 只返回候选结果和指标，不直接调用 `GameManager`。这样 `ScriptedRuntime` 可以完整测试游戏主流程，Pi 适配故障也不会污染领域逻辑。

### 10.2 Pi 子进程管理

每个 `PiRuntime` 持有：

- 独立 `asyncio.subprocess.Process`；
- 独立 `.runtime/players/<game_id>/seat_XX/`；
- 单独的 stdin 写锁；
- 一个 stdout JSONL reader task；
- 一个 stderr reader task；
- 一个 process watcher task；
- `pending_commands[rpc_id]` 和唯一 `active_turn`；
- `session_epoch` 和最后一次终态序号。

推荐启动参数（值均以独立 argv 传入，禁止拼接 shell 字符串）：

```text
pi.cmd
  --mode rpc
  --session-dir <absolute seat session dir>
  --session-id <uuid4>
  --provider <provider>
  --model <model>
  --thinking <level>
  --append-system-prompt <absolute generated system prompt>
  --no-builtin-tools
  --tools get_board,get_role,get_mechanic,get_interaction,get_rule_topic,search_rules
  --no-extensions
  --extension <absolute werewolf_knowledge.ts>
  --no-skills
  --no-prompt-templates
  --no-themes
  --no-context-files
  --no-approve
```

子进程 `cwd` 使用该座位专属的空运行目录，而不是仓库根目录。Windows 使用 `asyncio.create_subprocess_exec` 创建新进程组，并通过标准库 `ctypes` 创建带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object，把启动器及其 Node 子进程纳入同一作业；有序关闭失败后依次 terminate、限时等待、关闭 Job/kill，并确保 stdout/stderr reader 收尾。Job 绑定失败视为 `pi doctor` 失败，不能带着无法回收的进程树开始 12 人长局。若入口是 `.cmd`，必须有 Windows contract test 证明参数按 argv 原样到达 Pi，禁止自行拼接 `cmd /c` 字符串。

子进程环境不得无筛选继承主持进程的全部变量。实现按 allowlist 传入 Windows/Node 启动所需的 `SystemRoot`、`ComSpec`、`PATH`、`PATHEXT`、临时目录和 Pi 配置目录，加上该玩家所选 provider 明确需要的凭证变量，以及本座位的 Knowledge base URL/token；不同 provider 的密钥不能全部广播给所有座位。API key 不使用 `--api-key` 命令行参数，避免出现在进程列表和诊断输出中。

stdout 只允许 RPC JSONL；使用二进制/UTF-8 StreamReader 按字节 LF (`b"\n"`) 切分并兼容前置 CR，不能使用会把 Unicode `U+2028/U+2029` 当换行的通用行读取器。无法解析的行触发协议错误，不得当作模型最终文本。持续消费 stdout 防止管道反压卡死。stderr 写入座位级诊断日志，经过 API key/令牌脱敏，不与公共记录混合。

### 10.3 RPC 完成语义

适配器将 Pi 原始事件归一化为：

```text
COMMAND_ACCEPTED
TURN_STARTED
TOOL_STARTED / TOOL_FINISHED
OUTPUT_DELTA
TURN_SUCCEEDED(final_text, usage)
TURN_FAILED(error)
TURN_ABORTED
PROCESS_EXITED
```

Pi `0.87.1` 的映射为：`prompt response success=true -> COMMAND_ACCEPTED`；`agent_start/turn_start -> TURN_STARTED`；`tool_execution_start/end -> TOOL_STARTED/TOOL_FINISHED`；`message_update text_delta -> OUTPUT_DELTA`；assistant `message_end` 保存权威最终消息；`agent_settled` 触发本项目终态判定。收到 `agent_settled` 后再调用 `get_last_assistant_text`，以其 `data.text` 作为待解析最终输出，并用已捕获的 assistant `message_end` 做一致性检查。provider error/abort、进程退出、最终文本为空或两者不一致均形成失败。`OUTPUT_DELTA` 不参与 Schema 提交。

启动握手依次执行 `get_state`、`set_steering_mode(one-at-a-time)`、`set_follow_up_mode(one-at-a-time)`、`set_auto_compaction(true)`、`set_auto_retry(false)`。自动重试关闭后，提供商瞬态失败立即交给 Python/主持人处理，避免 Pi 在硬超时之外继续隐式重试；每次正式任务完成可调用 `get_session_stats` 记录 Token、成本、工具数和上下文用量。

如果 Pi 原始事件不携带项目 `request_id`，适配器仍保持“一个 Session 同时最多一个 active turn”，并用本地 `rpc_id + session_epoch + terminal sequence` 关联，严禁仅按到达顺序跨请求匹配。

### 10.4 超时与重试

`run_turn` 同时启动软/硬定时器：

- 软超时：若尚未终态，只发送一次 `steer` 友好提醒并记录 `soft_timeout_sent_at`；
- 硬超时：不自动推进阶段，将请求标记 `HARD_TIMEOUT`，游戏进入 `WAITING_GM`；
- 主持人可 `extend`、`wait`、`abort`、`retry` 或按板子超时规则处理；
- `abort` 前先发送 `clear_queue`，再发送 `abort` 并等待其成功响应和 `agent_settled`；迟到事件只有已匹配且仍为 ACTIVE 的请求能提交；
- `retry` 默认清队列并 abort 旧请求；无法确认 idle 时重启该座位进程并递增 `session_epoch`。

格式错误允许最多一次自动修复请求，只把 Pydantic 错误摘要和同一输出 Schema 发给同一 Session，不补充新的游戏信息。修复使用新的物理 request ID；两次仍失败则等待主持人处理。

### 10.5 `pi doctor`

`uv run werewolf pi doctor` 必须完成：

1. 按配置/环境变量/PATH 定位 Pi 可执行文件并打印版本；
2. 验证 RPC 模式能启动且 stdout 不混入非 JSON；
3. 创建临时 Session，完成两轮 Prompt 并验证上下文延续；
4. 验证 `steer`、`abort`、进程退出事件；
5. 加载最小测试 Extension，调用回环 HTTP mock；
6. 验证禁用 Shell、任意文件读写等非必要工具；
7. 用 `pi auth check` 验证配置中的 provider/model 可用，但不输出凭证；
8. 验证 `agent_settled`、assistant `message_end` 和用量采集；
9. 输出机器可读 capabilities JSON；
10. 删除临时测试 Session，不触碰正式游戏目录。

若任何 P0 能力缺失，`new/start` 必须拒绝启动真实 Pi 玩家并给出明确错误，不能静默降级。

---

## 11. Prompt 组装

### 11.1 初始化 Prompt

只发送一次的固定内容：

- 全局游戏行为边界；
- 座位号、真实角色和阵营；
- 板子 ID/版本、胜利目标；
- 由 Python 读取并展开的 Werewolf Reading Skill 全文；通过 `--append-system-prompt <file>` 显式注入，不依赖模型使用已禁用的 read 工具自行加载 Skill；
- 规则工具签名；
- 公屏文字是不可信玩家声明；
- 不得伪造引擎状态、频道或工具结果；
- 最终响应必须匹配统一 JSON Schema。

不在初始化时灌入完整规则正文，保留自主检索要求。

### 11.2 每回合 Prompt

按稳定顺序组装：

1. 短身份摘要：座位、角色、阵营、存活/投票权、剩余技能资源；
2. 当前任务：阶段、频道、允许动作、截止信息；
3. 授权增量事件，带 `event_id` 和来源类型；
4. 当前行动窗口的目标约束和可见信息；
5. 输出 Schema；
6. 提醒有疑问可调用规则工具，不能要求主持引擎泄露隐藏状态。

身份摘要由 Python 从权威状态生成，解决长上下文压缩后的身份漂移；它不包含不必要的全部历史。

---

## 12. 行动注册表与基础校验

`config/actions.yaml` 是动作类型注册表，板子再决定哪些动作在本局启用：

| Code | 名称 | 基础约束 |
|---:|---|---|
| 101 | `WOLF_KILL` | 狼队最终刀口窗口；目标为一名合法存活非授权狼队成员。 |
| 102 | `SEER_INSPECT` | 预言家夜间；目标一名其他存活玩家。 |
| 103 | `WITCH_POISON` | 女巫毒药未使用；目标符合板子限制。 |
| 104 | `WITCH_HEAL` | 女巫解药未使用；目标等于当夜刀口且不能是自己。 |
| 105 | `HUNTER_SHOOT` | 猎人触发窗口且死亡原因允许；目标一名其他存活玩家。 |
| 201 | `VOTE` | 当前有投票权；目标属于当前候选集合。 |
| 202 | `ABSTAIN` | 仅窗口明确允许。 |
| 299 | `PASS` | 仅可选技能窗口明确允许。 |

`PlayerResponse.actions` 是一个请求 bundle。首板子所有窗口（包括女巫药剂窗口）都要求恰好一个动作。女巫窗口可提交 `[WITCH_HEAL]`、`[WITCH_POISON]` 或 `[PASS]`；`PASS` 不能与其它动作并存，解药和毒药不能同夜同时提交。未来板子若启用多动作，必须由其规则快照显式声明并继续使用原子校验和 `ActionResolution` 事务。

校验顺序固定：

1. Schema 和字段类型；
2. game/session/request/window 关联；
3. 窗口是否开放、是否已提交；
4. 玩家存活和资格；
5. bundle 数量、code 唯一性以及每个 action code 是否在本窗口允许集合；
6. 角色授权；
7. 技能资源/使用次数；
8. 单动作及跨动作的目标数量、范围、存活状态、自选/同目标限制；
9. 窗口依赖和必要可见信息；
10. 幂等性检查。

校验通过只生成 `ValidatedActionRequest`，不会立刻消耗药剂或杀死目标。资源消耗和实际效果随 GM 确认的 `ActionResolution` 一起提交。

---

## 13. 主持人 CLI

### 13.1 顶层命令

```powershell
uv run werewolf config validate config/game.yaml
uv run werewolf rules validate vault/published/boards/classic_12_seer_witch_hunter_idiot/1.0.0/board.md
uv run werewolf pi doctor
uv run werewolf moderator --config config/game.yaml
uv run werewolf archive verify games/archive/<timestamp_game_id>
```

`moderator` 是长生命周期进程，内部使用命令循环；不能把每个阶段做成独立进程命令，否则内存权威状态和子进程生命周期会丢失。

### 13.2 交互命令

- `new`：验证配置、生成 seed、创建规则快照和活动目录；
- `start`：分配角色、启动 Session、进入玩家准备；
- `status [--private]`：默认仅显示运行状态；`--private` 明确显示 GM 隐藏信息并在终端标注敏感；
- `next`：仅在当前阶段 guard 通过时转移；
- `confirm`：确认待提交的普通阶段结果；
- `resolve <draft_id>`：交互选择实际效果、资源消耗、公开/私有通知；
- `pause/resume`：修改 `RunStatus`；
- `extend <seat> <seconds>`：延长当前请求；
- `retry <seat> --reason ...`：使旧请求过期并重试；
- `abort <seat> --reason ...`：终止请求，不自动替玩家生成决策；
- `team extra-round wolves`：追加一轮狼队讨论；
- `save`：只在没有未决 Resolution、没有活跃提交临界区时允许手动一致快照；
- `finish`：完成胜负确认、最终保存和时间戳归档；
- `quit`：若对局未完成，必须明确提示当前仅能从最近一致快照恢复。

所有会改变状态的主持命令写 `ModeratorOperation`：操作人、时间、命令类型、原因、前后 revision、关联 request/window。密钥、完整 Prompt 和模型隐藏推理不入日志。

---

## 14. 存档、时间戳目录与恢复

### 14.1 活动目录

```text
games/active/<game_id>/
├── state.json
├── public.md
├── ruleset/
│   ├── manifest.json
│   └── ...冻结规则文件
├── private/
│   ├── gm.md
│   ├── channels/
│   │   └── wolves.md
│   └── runtime_refs.json
└── snapshots/
    └── 20260927T151505Z_round_001/
        ├── state.json
        ├── public.md
        ├── private/
        └── snapshot_manifest.json
```

狼队专属 `wolves.md` 位于 GM 私有目录，按夜晚和发言顺序渲染。它用于主持审计，不是狼人共享文件；Pi 玩家只通过消息路由收到自己有权看到的增量团队事件。V1 的主要安全保证来自不暴露通用文件工具和不把路径发给 Agent；Windows ACL 级强隔离列为加固项。

### 14.2 自动保存

一次“完整昼夜循环”定义为：从某夜 `NIGHT_TEAM_CHAT` 开始，到对应白天 `VICTORY_CHECK` 完成。若游戏结束，也视为循环边界。

边界保存流程：

1. 确保无 `OPEN` 且正在提交的请求、无未确认 Resolution；
2. 在内存副本上计算下一 `snapshot_revision`；
3. 在同一目标父目录建立临时目录 `.tmp-<random>`；
4. 写 `state.json`、从 PUBLIC 投影生成 `public.md`、生成私有记录和 manifest；
5. flush 并重新读取 JSON 做 Schema 校验；
6. 校验公开文档不存在私有事件类型、真实身份和团队内容；
7. 原子重命名为 `<UTC timestamp>_round_NNN`；
8. 原子更新活动目录的 `state.json/public.md`；
9. 在内存中更新 `last_snapshot`。

目录时间戳用 UTC，例如 `20260927T151505Z_round_001`，避免 Windows 路径中的冒号和夏令时歧义。

### 14.3 结束归档

`finish` 完成最终快照后复制到：

```text
games/archive/20260927T183012Z_<game_id>/
```

先生成 `.staging-<id>`，校验文件数、大小和 SHA-256 后再原子改名。归档包括最终状态、公开记录、GM/狼队私有记录、冻结规则、配置的非密钥部分、Runtime 引用、版本清单和每轮正式快照，不包括 API key、环境变量值或完整模型内部历史。

归档成功后不自动删除 `games/active/<game_id>`，避免不可恢复操作。后续提供显式清理命令时再要求主持人确认。

### 14.4 恢复策略

启动时对比：

- `state.json.last_snapshot.snapshot_id`；
- `runtime_refs.json` 的 Session 引用和 `session_epoch`；
- 每个 Session 的可探测进度/最后任务引用；
- 规则 manifest 摘要。

如无法证明一致，不允许原 Session 直接续接。V1 提供：

1. `rebuild-sessions`：从最近一致快照为每座位创建新 Session，重新注入身份摘要和截至快照时该玩家有权知道的事件；标记 `recovered=true`，实验连续性降级；
2. `abandon`：结束当前局并保留诊断归档。

不提供“猜测 Session 已走到哪里后继续”的选项。

---

## 15. 错误模型与可观测性

### 15.1 错误分类

| 类别 | 示例 | 处理 |
|---|---|---|
| `ConfigError` | 重复座位、模型缺失 | 启动前失败，无状态变更。 |
| `RulesetError` | 版本冲突、引用缺失、未审核 | 拒绝创建快照。 |
| `RuntimeUnavailable` | Pi 不存在、启动失败 | 座位失败，游戏不得开始。 |
| `RpcProtocolError` | stdout 非 JSON、未知终态 | 隔离该 Runtime，等待 GM。 |
| `TurnTimeout` | 软/硬超时 | 软提醒；硬超时等待 GM。 |
| `ResponseSchemaError` | 输出非合法结构 | 最多一次格式修复，然后等待 GM。 |
| `ActionRejected` | 越权技能、非法目标 | 不改变状态；可反馈玩家并按策略重试。 |
| `InvalidTransition` | 必需窗口未完成就 next | CLI 拒绝并列出未满足 guard。 |
| `PersistenceError` | 写盘或校验失败 | 保留旧快照，状态标记 DEGRADED，不覆盖好文件。 |
| `SecurityViolation` | 查询其他游戏快照、伪造 token | 拒绝、记录 GM 审计，必要时终止座位。 |

### 15.2 日志

标准 Python `logging` 输出结构化字段：`game_id`、`seat`、`phase`、`request_id`、`window_id`、`event_id`、`state_revision`、`duration_ms`。日志分为：

- 控制台 GM 日志；
- 运行诊断日志；
- 游戏内审计（进入 state 快照）；
- PUBLIC 投影；
- TEAM/GM 私有 Markdown。

不得记录 API key、bearer token、完整环境变量、供应商认证头、模型隐藏推理。RPC 原始事件如需排障，默认只保留类型、序号、状态和用量；显式 debug 模式也必须脱敏且标为私有。

---

## 16. 测试方案

### 16.1 测试层级

1. 单元测试：模型、状态转移、权限、行动矩阵、规则调研/解析/发布、公开投影、原子保存；
2. 属性测试：任意事件集合下，非 audience 座位永不收到事件；任意重复请求最多提交一次；
3. Runtime contract：同一套测试运行于 `ScriptedRuntime` 和 Pi 录制协议 fixture；
4. 集成测试：用本地 fake JSONL 子进程模拟 accepted/delta/tool/terminal/迟到/崩溃；
5. 场景测试：完整昼夜、待决 PK 变体、白痴放逐、猎人吃毒、女巫单药与同夜禁双药、狼队隔离；
6. 实机 Smoke：2～3 个 Pi 模型加脚本玩家完成受控一局。

### 16.2 P0 用例

| 用例 | 必测断言 |
|---|---|
| 板子名入库 | 只输入板子名称即可生成包含 board/roles/mechanics/interactions 的完整草稿包。 |
| 多来源证据 | 关键机器规则能回链到足够的独立来源；重复转载不能虚增来源数。 |
| 规则冲突 | 不同版本的互斥规则被拆为变体或进入 NEEDS_DECISION，不会被静默混合。 |
| 覆盖门禁 | 缺少投票、遗言、胜负或关键技能交互时禁止发布。 |
| 网页 Prompt 注入 | 网页内的指令不能触发命令、读本地文件或绕过发布审批。 |
| 发布不可变 | 已发布版本不能原地修改；refresh 只能生成新草稿和 diff。 |
| 准备检索 | 没有服务端 receipt 时模型自称已查询也不能 ready。 |
| Session 隔离 | 两座位 Session 目录、进程、token、上下文不相同。 |
| 串行发言 | 前一座位未提交时下一座位 `run_turn` 调用次数为 0。 |
| 增量游标 | 失败不 ack；重试收到同一 event IDs；成功只 ack 一次。 |
| Prompt 注入 | 玩家发言中的系统式指令不能扩大工具或消息权限。 |
| 狼队频道 | 非成员在 observation、HTTP 接口、公开文件中均找不到狼队内容。 |
| 狼队 Markdown | 只含 TEAM 狼队事件，且不出现在 `public.md`。 |
| 狼队提交协调 | 固定 seed 得到可审计的运行时协调者；它不被当作板子角色，且不替代未决的官方狼队共识规则。 |
| 秘密投票 | 所有投票请求使用同一 observation revision；关闭前没有 PUBLIC 票。 |
| 平票 PK（候选实现） | 若已发布板子启用 PK，则按其快照创建发言和重投；候选资格及二次平票结果由板子字段决定，补充资料候选不作为官方断言。 |
| 白痴放逐 | 存活、公开翻牌、失去投票权但仍能发言。 |
| 女巫规则 | 官方字段验证不可自救、每晚最多一瓶、解药/毒药一次性资源与 `POISONED` 交互；刀口信息时序若无证据则阻止发布。 |
| 猎人规则 | `POISONED` 不开枪；`WOLF_KILL`/`EXILED` 打开合法触发窗口。 |
| 越权动作 | 村民提交 102 被拒绝，revision、资源、生死均不变。 |
| 迟到回包 | superseded request/session epoch 的成功事件也不能提交。 |
| 软硬超时 | 软提醒恰好一次；硬超时不推进队列并进入 WAITING_GM。 |
| 快照原子性 | 中途写失败时旧快照可读，新目录不成为正式快照。 |
| 公开防泄漏 | 对 public 投影做敏感字段/事件类型白名单断言。 |
| 恢复不一致 | Session 比快照新时必须阻止静默续接。 |

### 16.3 完成标准

- `uv sync --locked` 在干净机器上成功；
- Ruff、Mypy、Pytest 全通过；
- P0 领域代码行覆盖率建议不低于 85%，权限和 ActionValidator 分支覆盖率不低于 95%；
- `pi doctor` 在目标 Pi 版本上全部 P0 项通过；
- 至少一场 2～3 个真实模型 + 脚本玩家的对局完成并生成可验证归档；
- 人工检查 `public.md` 无真实身份、狼队消息、未公开投票或夜间私密信息。

---

## 17. 开发里程碑与交付顺序

### M0：工程与规则冻结

1. `uv init --package --python 3.11`，提交 `.python-version`、`pyproject.toml`、`uv.lock`；
2. 建立 Pydantic 领域模型、调研证据模型和错误类型；
3. 建立规则生产工作台骨架、发布门禁和 Provider 接口；
4. 通过“预女猎白”板子名完成首个调研任务，生成并人工复核板子、6 类角色、机制和交互文档；
5. 冻结行动注册表、阶段表、可见性矩阵；
6. 安装目标 Pi，完成 `pi doctor` 和协议 fixture；
7. 建立 Ruff/Mypy/Pytest 基线。

退出条件：机器规则能回答首局所有阶段、资格、可见性和基础交互；Pi 协议能力有实测证据。

### M1：知识闭环

1. 搜索计划、Agent Reach/HTTPS Provider 和证据归档；
2. 原子规则主张提取、变体聚类和冲突报告；
3. board/role/mechanic/interaction 草稿生成与覆盖矩阵；
4. Vault loader、严格 Schema、发布门禁和不可变版本；
5. 规则依赖闭合和 snapshot manifest；
6. `KnowledgeService` 精确/搜索/交互查询；
7. 回环 HTTP Gateway、座位 token 和 receipt；
8. Pi Extension 和 Werewolf Skill；
9. 入库、发布、查询权限、冲突、别名和未知规则测试。

退出条件：只给一个常见板子名即可形成带来源和冲突报告的可发布知识包；发布后，陌生 Session 仅获板子/身份 ID，可以自主完成强制查询并 ready，且无法读取游戏状态。

### M2：持续会话闭环

1. `PlayerRuntime`、`ScriptedRuntime`；
2. Pi 子进程、JSONL reader、终态关联；
3. 独立 Session、Prompt composer；
4. 软硬超时、steer/abort/retry、迟到丢弃；
5. 多轮 PoC：准备→发言→投票→下一轮发言。

退出条件：多个座位无 Session 混线；同座位跨任务保持上下文；格式错误和超时不会推进游戏。

### M3：游戏闭环

1. 状态机和事务式 StatePatch；
2. MessageRouter 和 DeliveryCursor；
3. 串行公屏；
4. 狼队频道、归票者与 GM 私有 Markdown；
5. 夜间窗口、行动校验和主持 Resolution；
6. 秘密投票、PK、白痴与猎人触发；
7. ModeratorShell。

退出条件：脚本 Runtime 能完成一个完整昼夜，真实 Pi 能参与至少发言、投票和一个技能窗口。

### M4：存档与验收

1. 完整昼夜自动快照；
2. PUBLIC/TEAM/GM 投影；
3. 结束时间戳归档和完整性校验；
4. 不一致恢复检测和重建 Session；
5. 全部安全/故障场景；
6. README、配置示例和演示脚本。

退出条件：受控对局归档可审计、可验证、无已知跨玩家泄漏，且从一致快照可按声明的降级方式恢复。

---

## 18. 配置示例

```yaml
schema_version: 1
runtime:
  pi:
    executable: null              # null 时按环境变量和 PATH 发现；可填绝对 pi.cmd
    compatible_version: "0.87.x"
    auto_compaction: true
    auto_retry: false

research:
  search_provider: agent_reach
  search_executable: null         # null 时从 PATH 发现
  min_independent_sources: 3
  synthesis:
    runtime: pi
    provider: provider_a
    model: model_a
    reasoning: high

game:
  game_id: demo-001
  board:
    id: classic_12_seer_witch_hunter_idiot
    version: 1.0.0
  seed: 20260927
  moderator_mode: human_assisted
  save:
    cycle_snapshot: true
    final_timestamp_archive: true
  timeout:
    prepare_soft_seconds: 240
    prepare_hard_seconds: 480
    turn_soft_seconds: 120
    turn_hard_seconds: 240
  response:
    max_format_retries: 1

players:
  - seat: 1
    runtime: pi
    provider: provider_a
    model: model_a
    reasoning: medium
  - seat: 2
    runtime: pi
    provider: provider_b
    model: model_b
    reasoning: high
```

Session 目录由程序根据 `game_id + seat` 派生，普通配置不允许任意绝对路径，避免路径穿越和座位共享目录。API key 只从环境变量或 Pi 提供商凭证读取，不允许写入 YAML、状态或归档。

规则调研的 Pi synthesis 配置与玩家配置分离；它使用独立 Session 和权限，不得自动继承某个座位身份。`executable: null` 表示自动发现，不表示功能可选：`pi doctor` 或 `rules doctor` 找不到依赖时必须在执行对应流程前失败。

---

## 19. 已知限制与后续项

- V1 的 Pi Extension 与 Pi 进程仍运行在同一 Windows 用户权限下；禁用工具是能力最小化，不是对恶意原生扩展的强沙箱。
- 狼队 Markdown 的“其他 Agent 不可见”由不授予文件工具、不传路径和消息路由保证；若未来加载不可信扩展，需要 Windows 独立用户、ACL 或容器级隔离。
- V1 只在完整昼夜边界保证一致恢复，边界内崩溃可能损失进度。
- 人工主持裁定会影响实验可比性，必须依靠规则版本、Resolution 和干预审计留痕。
- 首板子已纳入官方 12 人标准场的警长竞选；仍不做更多特殊角色、自动裁判、数据库、Web UI、远程玩家、向量检索和多 Harness 正式适配。
- 官方已明确首夜夜遗言和白天死亡遗言范围；首日竞选发言起点、警长方向/轮换、退选与警长平票等未被官方页覆盖，规则生产工作台必须保留为 `NEEDS_DECISION`，不能由补充资料静默替代。

---

## 20. 开发前最终检查表

- [ ] “预女猎白”调研任务已完成多来源取证、冲突分析和覆盖验证；
- [ ] 首板子知识包由工作台发布，全部 Markdown 已按 2.2 录入、标记 `published` 并带审核元数据；
- [ ] 所有机器规则字段均可回链到来源证据或明确的主持人决议；
- [ ] 官方遗言范围已录入；每日发言起点/方向、轮换及警长竞选未覆盖项已有独立来源或主持人定向决策；
- [ ] 目标 Pi 已安装，`pi doctor` 通过；
- [ ] Pi RPC 原始事件到内部事件映射已有 contract fixture；
- [ ] Extension 确认可在禁用通用文件/Shell 工具时加载；
- [ ] `.python-version` 为 3.11，`uv.lock` 已提交；
- [ ] `.gitignore` 已阻止 Pi Session、身份 Prompt、游戏私密目录、日志、token 和 `.env` 入库；
- [ ] 规则快照、权限矩阵、行动表和状态机单测通过；
- [ ] 女巫解药、毒药、PASS、每晚最多一瓶、同夜禁双药和 bundle 原子拒绝场景通过；
- [ ] 并发投票在 revision 竞争下不丢票，Windows Pi/Node 进程树清理 contract test 通过；
- [ ] 狼队私有 Markdown 与 `public.md` 的正反向泄漏测试通过；
- [ ] 主持人接受“仅从完整昼夜快照恢复”的 V1 限制；
- [ ] 一场脚本端到端场景通过后，才接入真实模型做完整演示。
