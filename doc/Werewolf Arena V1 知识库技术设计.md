# Werewolf Arena V1 知识库技术设计

> 文档状态：开发基线（2026-09-27 反向评审通过）  
> 所属系统：Werewolf Arena V1 规则知识子系统  
> 对应总方案：`Werewolf Arena V1 技术方案 v1.0`  
> 技术栈：Python 3.11、uv、标准 Markdown/YAML/JSON（可选用 Obsidian 浏览）、Pydantic v2、Pi Extension  
> 核心原则：知识生产与游戏读取分离；发布版本不可变；对局只读固定快照；规则均可追溯  

---

## 1. 定位

Werewolf Arena 包含两个并列的一等子系统：

```text
┌──────────────────────────────┐   published snapshot   ┌─────────────────────────────┐
│ 规则知识子系统                │ ─────────────────────> │ 狼人杀游戏子系统             │
│                              │                        │                             │
│ 调研 -> 结构化 -> 验证 -> 发布 │                        │ 身份 -> 阶段 -> 消息 -> 行动  │
│            │                 │                        │             │               │
│            └-> 只读查询服务  │ <──── tool calls ───── │        Pi 玩家 Session       │
└──────────────────────────────┘                        └─────────────────────────────┘
```

知识子系统承担：

1. 根据自然语言板子名联网调研规则；
2. 将来源拆成可验证的原子规则主张；
3. 生成板子、角色、机制、交互和阅读指南；
4. 检测不同平台/赛事/年份的规则变体和冲突；
5. 验证知识完整性并发布不可变版本；
6. 为每局创建固定知识快照；
7. 通过受控工具让 Pi 玩家在开局和游戏中随时读取、重读；
8. 记录玩家完成必读知识的服务端证明。

知识子系统不承担：

- 保存真实玩家身份映射；
- 返回某个座位的角色或秘密行动；
- 根据规则自动完成复杂结算；
- 在进行中的对局里联网临时补规则；
- 把攻略、胜率或策略建议当作硬规则；
- 允许 Agent 修改 Vault 或已发布知识。

---

## 2. 逻辑架构

### 2.1 六层架构

```text
┌───────────────────────────────────────────────────────────────────┐
│ 1. Acquisition / 证据采集层                                       │
│ SearchProvider + PageFetcher + SourceNormalizer                   │
├───────────────────────────────────────────────────────────────────┤
│ 2. Workbench / 规则工作台                                         │
│ ClaimExtractor + VariantClusterer + ConflictAnalyzer              │
├───────────────────────────────────────────────────────────────────┤
│ 3. Authoring / 知识编写层                                         │
│ BoardGenerator + RoleGenerator + Mechanic/InteractionGenerator    │
├───────────────────────────────────────────────────────────────────┤
│ 4. Validation & Publishing / 验证发布层                            │
│ SchemaValidator + CoverageValidator + SemanticTests + Publisher   │
├───────────────────────────────────────────────────────────────────┤
│ 5. Snapshot & Index / 快照索引层                                   │
│ PackageCompiler + Manifest + Alias/Topic/Relation/Text Index       │
├───────────────────────────────────────────────────────────────────┤
│ 6. Runtime Serving / 对局读取层                                    │
│ KnowledgeService + HTTP Gateway + Pi Extension + Reading Skill    │
└───────────────────────────────────────────────────────────────────┘
```

层间只通过 Pydantic 模型和文件产物交接。采集/工作台不能直接写已发布目录；对局读取层不能访问工作台目录；游戏引擎不能调用搜索 Provider。

### 2.2 组件职责

| 组件 | 输入 | 输出 | 关键限制 |
|---|---|---|---|
| `ResearchOrchestrator` | 板子自然语言名称 | 调研任务 | 只在开局前/维护期运行 |
| `RuleResearchProvider` | 查询词、URL | 搜索结果、网页证据 | HTTP/HTTPS 优先；网页不可信 |
| `PiRuleSynthesisProvider` | 受限证据摘录 | `RuleClaim[]`、知识草稿 | 使用独立非玩家 Session；无发布权限 |
| `ClaimExtractor` | Pi 结构化输出 | 已校验 `RuleClaim[]` | 引用片段必须能在 evidence 中精确定位 |
| `VariantClusterer` | 规则主张 | 候选板子变体 | 不混合互斥版本 |
| `ConflictAnalyzer` | 候选变体 | 冲突和推荐 | 不能擅自填无证据规则 |
| `KnowledgeGenerator` | 已选变体主张 | Markdown 草稿包 | 机器字段和正文一致 |
| `KnowledgeValidator` | 草稿包 | 覆盖/Schema/语义报告 | 阻塞错误禁止发布 |
| `RulesetPublisher` | 通过验证的草稿 | 不可变版本包 | 主持人确认 diff 后才能写 |
| `KnowledgePackageCompiler` | 已发布 Markdown | 只读运行包和索引 | 结果可重复构建 |
| `KnowledgeSnapshotBuilder` | 发布包引用 | 对局固定快照 | 只复制依赖闭包 |
| `KnowledgeService` | 快照内查询 | 结构化知识结果 | 不接触游戏秘密状态 |
| `KnowledgeGateway` | 座位 token + HTTP 请求 | 限权查询结果/receipt | token 固定到单局快照 |
| `WerewolfKnowledgeExtension` | Pi 工具调用 | HTTP 查询代理 | 无任意路径和 Shell |
| `WerewolfReadingSkill` | 当前任务和工具说明 | 模型阅读行为约束 | 指导读取，不裁判 |

### 2.3 软件边界

知识库本体是项目内的 Markdown/YAML/JSON 文件、Python 编译索引和单局快照，不依赖任何知识库服务器、数据库或向量数据库。Obsidian 只是可选的人类浏览/编辑界面；未安装 Obsidian 不影响调研、发布、查询或游戏运行。

V1 自动知识生产需要一个可工作的联网搜索/抓取 Provider，以及一个已认证模型的独立 Pi authoring Session。流程为 Python 搜索和抓取、Pi 只做受限证据的结构化提取/编写、Python 做确定性校验和发布门禁。Pi authoring Session 不复用玩家 Session，也不直接读写 Vault。

目标宿主机的 Agent Reach 可用，但必须从允许联网和读取其用户配置的宿主环境调用；Codex 受限沙箱不是生产运行环境。Provider 启动前执行 `agent-reach doctor --json` 选择 active backend，并优先使用 HTTP/HTTPS 路径。

---

## 3. 知识领域模型

### 3.1 知识实体总览

```text
KnowledgePackage
├── BoardDefinition               # 这个板子是什么、如何整体运行
│   ├── BoardRoleBinding[]        # 使用哪些角色及板子内覆盖规则
│   ├── PhaseDefinition[]         # 阶段和窗口顺序
│   ├── MechanicRef[]             # 投票、遗言、胜负等机制
│   ├── InteractionRef[]          # 本板子需要的角色交互
│   └── ReadingPlan               # 应该怎样读本板子
├── RoleDefinition[]              # 角色目标、技能、资源、信息和限制
├── MechanicDefinition[]          # 跨角色公共机制
├── InteractionDefinition[]       # 多规则相遇时怎样处理
├── GlossaryEntry[]               # 刀口、屠边、PK 等术语
├── RuleSource[]                  # 证据和出处
├── KnowledgeTestCase[]           # 规则问答和情境测试
└── PackageManifest               # 版本、依赖和哈希
```

板子、角色、机制和交互必须分开建模。只写一篇长篇“板子说明”会造成以下问题：工具无法精确读取、角色复用困难、变体覆盖不清晰、冲突无法落到具体规则键、模型每次都要读取大量无关内容。

### 3.2 `BoardDefinition`：板子介绍和总规则

板子文档必须包含：

| 分类 | 字段/内容 |
|---|---|
| 标识 | `board_id`、名称、别名、语义版本、语言、状态 |
| 摘要 | 一段不超过 300 字的板子介绍、适合人数、核心特点 |
| 组成 | 人数、阵营、角色数量、角色版本绑定 |
| 胜负 | 屠边/屠城、特殊胜利、平局条件、检查时点 |
| 主流程 | 首夜/白天顺序、阶段拓扑、循环边界 |
| 狼队 | 相认、可见关系、讨论顺序、归票/刀口形成方式 |
| 夜间 | 行动窗口、依赖、并行性、中间信息可见性 |
| 白天 | 死亡公布、发言、警长、投票、PK、遗言 |
| 公共机制引用 | 投票、死亡、技能触发、胜负检查等 mechanic refs |
| 特殊交互引用 | 板子内必须处理的 interaction refs |
| 阅读计划 | 开局必读、按阶段推荐读、易错规则 |
| 来源 | 支撑机器执行字段的 claim/source refs |

板子正文建议固定章节：

```markdown
# 板子名称
## 一句话介绍
## 身份配置
## 阵营与胜利条件
## 完整游戏流程
## 夜间行动顺序
## 狼队沟通与刀口
## 白天发言和投票
## 死亡、遗言与触发技能
## 本板子特殊规则
## 推荐阅读顺序
## 来源与版本说明
```

### 3.3 `RoleDefinition`：角色技能介绍

角色知识不能只写“技能描述”，必须完整描述执行契约：

```text
RoleDefinition
├── role_id / name / aliases / version
├── faction / team / victory_goal
├── public_summary
├── private_identity_card
├── abilities[]
│   ├── ability_id / name / action_code
│   ├── timing / allowed_phases
│   ├── trigger_type: ACTIVE | PASSIVE | DEATH_TRIGGER
│   ├── target_rule
│   ├── usage_limit / resource
│   ├── input_information
│   ├── request_effect
│   ├── resolution_effect
│   ├── result_visibility
│   └── failure/exception rules
├── knowledge_at_start
├── team_visibility
├── death_behavior
├── board_compatibility
├── common_mistakes
└── claim/source refs
```

同一角色在不同板子可能有不同细则。例如女巫能否自救、能否双药，不能只放在全局女巫文档中作为唯一事实。

### 3.4 基础角色与板子有效角色

采用“基础角色 + 板子绑定覆盖”模式：

```python
class BoardRoleBinding(BaseModel):
    role_ref: VersionedRef
    count: int
    effective_rules: dict[str, JsonValue]
    override_claim_refs: list[str]
```

- `RoleDefinition` 介绍角色的稳定概念和默认能力结构；
- `BoardRoleBinding.effective_rules` 存放该板子版本的具体覆盖值；
- 发布时编译出 `EffectiveRoleProfile`；
- 游戏中的 `get_role` 默认返回当前板子的 `EffectiveRoleProfile`，不能只返回脱离板子的通用角色说明；
- 覆盖值必须有板子来源 claim，不能靠生成器猜测。

因此首板子预女猎白中的 `witch` 可以明确为：不能自救、每晚最多使用一瓶药、解药和毒药各一瓶；官方页没有明确女巫何时获知刀口，`knows_wolf_target` 必须保持待决或由后续证据覆盖。另一个板子可绑定不同值，而不会污染彼此。

### 3.5 `MechanicDefinition`：公共规则介绍

Mechanic 用于不属于单一角色的规则：

- `game_cycle`：夜晚、白天和轮次；
- `speech_order`：发言起点、方向、PK 发言；
- `voting`：投票资格、秘密/公开、弃票、统计；
- `tie_and_pk`：平票候选、重投资格、二次平票；
- `death_and_last_words`：死亡原因、公开时点、遗言；
- `wolf_team_chat`：狼队成员、发言和归票；
- `victory_eliminate_side`：屠边胜利检查；
- `action_request_resolution`：请求不等于生效；
- `information_visibility`：公开、团队、私人、GM。

每个机制都包含：适用阶段、参与资格、输入、输出、顺序、异常分支、可见性、示例和来源。

### 3.6 `InteractionDefinition`：特殊交互

交互用于两个或更多规则同时发生时的裁定依据：

```python
class InteractionDefinition(BaseModel):
    interaction_id: str
    version: str
    board_refs: list[VersionedRef]
    subjects: list[str]             # role/ability/mechanic IDs
    situation_key: str              # hunter.dies_by.witch_poison
    preconditions: list[Predicate]
    ordering: list[ResolutionStep]
    outcome: dict[str, JsonValue]
    notifications: list[VisibilityRule]
    examples: list[ScenarioExample]
    claim_refs: list[str]
```

预女猎白至少需要：

- 猎人被女巫毒杀不能开枪；
- 猎人被狼刀或投票放逐后的开枪窗口；
- 白痴被投票放逐时翻牌存活、失去投票权；
- 白痴以其他方式死亡时正常死亡；
- 女巫解药与狼刀的结算；
- 女巫每晚最多一瓶药、解药/毒药资源消耗和目标约束；
- 多人死亡时公开/遗言/触发窗口顺序；
- 终局与死亡触发技能的先后检查。

### 3.7 `ReadingPlan`：怎么读板子

“怎么读板子”本身必须是版本化知识，而不是只藏在 Prompt 中：

```python
class ReadingPlan(BaseModel):
    board_ref: VersionedRef
    bootstrap_topics: list[KnowledgeRef]
    role_required_topics: dict[str, list[KnowledgeRef]]
    phase_topics: dict[GamePhase, list[KnowledgeRef]]
    high_risk_topics: list[KnowledgeRef]
    suggested_queries: list[str]
```

示例：

```yaml
bootstrap_topics:
  - board:overview
  - board:victory
  - mechanic:game_cycle
role_required_topics:
  witch:
    - role:witch
    - interaction:witch_wolf_kill
    - mechanic:night_resolution
phase_topics:
  VOTE:
    - mechanic:voting
    - mechanic:tie_and_pk
```

ReadingPlan 供启动卡、Skill 和工具响应共同使用。工具每次返回 `recommended_next_reads`，帮助模型从概览逐步阅读，而不是盲搜整库。

---

## 4. 物理存储架构

### 4.1 Vault 目录

```text
vault/
├── README.md
├── _schemas/                         # Pydantic/JSON Schema 导出和编写模板
│   ├── board.schema.json
│   ├── role.schema.json
│   ├── mechanic.schema.json
│   ├── interaction.schema.json
│   └── source.schema.json
├── _workbench/                       # 未发布，游戏查询永不挂载
│   └── <timestamp>_<research-job>/
├── published/
│   ├── boards/
│   │   └── <board_id>/
│   │       └── <version>/
│   │           ├── board.md
│   │           ├── reading-plan.yaml
│   │           └── faq.md
│   ├── roles/
│   │   └── <role_id>/
│   │       └── <version>/role.md
│   ├── mechanics/
│   │   └── <mechanic_id>/
│   │       └── <version>/mechanic.md
│   ├── interactions/
│   │   └── <interaction_id>/
│   │       └── <version>/interaction.md
│   ├── glossary/
│   │   └── <term_id>.md
│   ├── sources/
│   │   └── <source_id>.yaml
│   ├── tests/
│   │   └── <board_id>/<version>/*.yaml
│   └── manifests/
│       └── <package_id>.json
└── compiled/                          # 可重建，不手工编辑
    └── <package_id>/
        ├── package.json
        ├── documents.jsonl
        ├── exact-index.json
        ├── alias-index.json
        ├── topic-index.json
        ├── relation-index.json
        ├── text-index.json
        └── manifest.json
```

Obsidian 打开 `vault/published` 即可浏览正式知识；`compiled` 是运行时优化产物，不作为人工知识源。

### 4.2 Markdown 与机器字段

所有正式文档使用 YAML Frontmatter + Markdown 正文：

```markdown
---
schema_version: 1
kind: role
id: witch
version: 1.0.0
name: 女巫
aliases: [药师]
status: published
faction: good
applicable_boards:
  - classic_12_seer_witch_hunter_idiot@1.0.0
claim_refs: [claim-witch-001, claim-witch-002]
---

# 女巫

## 角色目标
...

## 本板子有效技能
...
```

机器执行字段必须在 Frontmatter 或关联的结构化 YAML 中；Markdown 正文用于模型阅读和人工维护。发布验证器必须检测两者明显不一致，例如机器字段 `can_self_heal=false`，正文却写“可以自救”。

YAML 只用 `yaml.safe_load` 并在进入 Pydantic 前限制文件大小、节点深度和允许类型；所有规范引用最终解析为受控 ID/version，不把 Frontmatter 中的字符串直接当文件路径。由 ID 推导路径时必须规范化并校验仍位于 `vault/published` 或指定快照根目录内。

### 4.3 Obsidian 链接和规范引用

- 人类导航可使用 Obsidian `[[链接]]`；
- 机器解析一律使用稳定 `VersionedRef`，不依赖文件名或 Wiki Link 文本；
- 移动文件不改变 ID；
- 同一 ID/版本只能对应一个内容哈希；
- 板子必须固定依赖的角色/机制/交互版本，禁止使用 `latest`。

---

## 5. 发布与编译

### 5.1 发布包

一个板子发布包是完整依赖闭包：

```text
RulesetPackage
├── package_id
├── board_ref
├── role_refs[]
├── mechanic_refs[]
├── interaction_refs[]
├── glossary_refs[]
├── reading_plan
├── source_refs[]
├── knowledge_tests[]
└── manifest_sha256
```

发布包必须能独立回答当前板子的所有知识查询，不得运行时回退到另一个未固定版本。

### 5.2 编译步骤

`KnowledgePackageCompiler.compile(package_id)`：

1. 加载发布 manifest；
2. 验证所有文档状态为 `published`；
3. 验证依赖闭合和内容哈希；
4. 合并基础角色和板子覆盖，生成 `EffectiveRoleProfile`；
5. 把 Markdown 按稳定 section ID 分段；
6. 生成精确 ID、别名、主题、关系和全文倒排索引；
7. 生成 ReadingPlan 导航；
8. 运行知识测试；
9. 生成只读 `package.json` 和 manifest；
10. 相同输入必须生成相同逻辑内容和哈希。

可重复构建的规范化规则固定为 UTF-8、LF 换行、JSON 排序键、稳定文档/section 顺序和相对路径。编译时间、绝对路径、临时目录和运行机器信息不得进入 `package_id` 或 `manifest_sha256`；如需记录则放在不参与逻辑哈希的 `build-info.json`。正式发布统一使用 `status: published`，审核人和审核时间是独立元数据。

V1 不使用向量数据库。全文检索先做 Unicode NFKC、大小写和空白规范化；拉丁文本按词切分，中文同时建立单字、二元/三元字符 n-gram 与原文子串索引，再结合标题、别名、标签、section 和正文评分。这样无需额外中文分词软件，也能稳定命中“猎人吃毒能否开枪”等查询。后续加入向量检索不能改变精确 ID 查询优先级。

### 5.3 索引

| 索引 | 用途 | 示例 |
|---|---|---|
| Exact Index | ID + 版本精确读取 | `role:witch` |
| Alias Index | 中文别名解析 | “女巫”“药师” |
| Topic Index | 按规则主题读取 | `vote.tie.pk` |
| Relation Index | 找角色相关交互/机制 | witch -> interactions |
| Text Index | 自由关键词搜索 | “猎人 吃毒 开枪” |

别名存在歧义时返回候选列表，不能随机选择。对局 token 已固定板子后，搜索结果优先当前板子的 Effective Profile 和适用交互。

---

## 6. 对局快照

### 6.1 创建

创建游戏时，`KnowledgeSnapshotBuilder` 从 compiled package 复制最小完整依赖到：

```text
games/active/<game_id>/ruleset/
├── package.json
├── documents.jsonl
├── indexes/
├── reading-plan.json
└── manifest.json
```

快照记录：`snapshot_id`、`package_id`、board ID/version、每个文件哈希、创建时间和编译器版本。

### 6.2 不变性

- 游戏开始后快照目录只读；
- Vault 发布新版本不影响已有快照；
- 所有玩家查询都绑定相同 `snapshot_id`；
- Runtime Prompt 和工具结果都携带 snapshot 摘要；
- 恢复游戏时必须验证 manifest 哈希；
- 快照损坏时阻止继续，不回退到当前 Vault 的最新版本。

---

## 7. Pi 玩家启动与动态阅读流程

### 7.1 启动卡

每个玩家 Session 启动时，Python 从快照生成 `KnowledgeBootstrapCard`，与身份私信一起发送：

```json
{
  "board": {
    "id": "classic_12_seer_witch_hunter_idiot",
    "version": "1.0.0",
    "name": "12人标准场（预女猎白）",
    "summary": "暗牌、带警长；4狼、4民、预言家、女巫、猎人、白痴；狼人屠边，好人消灭全部狼人。"
  },
  "your_role": {
    "id": "witch",
    "name": "女巫",
    "summary": "你属于好人阵营，拥有一瓶解药和一瓶毒药；具体限制请读取当前板子的有效角色说明。"
  },
  "snapshot_id": "ruleset-...",
  "required_reads": [
    {"tool": "get_board", "id": "classic_12_seer_witch_hunter_idiot"},
    {"tool": "get_role", "id": "witch"}
  ],
  "knowledge_policy": "规则不确定、遗忘或遇到特殊交互时必须重新调用知识工具，不要仅凭常识猜测。"
}
```

启动卡是简明导航，不取代完整知识读取。这样既满足“启动就把板子和当前角色发过去”，又不会把整库一次性塞进上下文。

### 7.2 准备阶段

```text
Python 发送启动卡
  -> Pi 调用 get_board()
  -> 获得板子介绍、角色构成、胜负、流程、必读主题和引用
  -> Pi 调用 get_role(本人 role_id)
  -> 获得当前板子的 EffectiveRoleProfile
  -> 按 recommended_next_reads 选择机制/交互
  -> Pi 返回 ready
  -> Python 用服务端 receipt 验证必读已完成
```

`ready` 不是模型的一句自我声明。只有知识服务记录了当前 `session_epoch` 的 `get_board` 和本人 `get_role` 成功 receipt，准备阶段才能完成。

### 7.3 游戏中重读

每个决策 Prompt 都重复短引用：

```text
当前板子：预女猎白 classic_12...@1.0.0
你的角色：女巫 witch（当前有效规则来自本局 snapshot）
知识工具：随时可重新读取；若遗忘、没看明白或不确定交互，先查规则再行动。
```

允许在所有需要模型决策的窗口调用知识工具：

- 忘记板子流程：`get_board()` 或 `get_rule_topic("board.flow")`；
- 忘记自己的技能：`get_role("witch")`；
- 想了解场上公开角色声明涉及的角色：`get_role("hunter")`；
- 不清楚平票：`get_mechanic("tie_and_pk")`；
- 不清楚猎人吃毒：`get_interaction(subjects=["hunter", "witch_poison"])`；
- 不知道该查什么：`search_rules("平票以后谁能投票")`。

查询角色规则不等于查询座位身份。任何玩家可以读取当前板子中角色的公开规则说明，但不能问“7 号是什么角色”。

### 7.4 上下文压缩与遗忘

- Python 每回合发送 board/role stable refs 和最少状态摘要；
- 工具结果带 `snapshot_id`、文档版本和 `result_id`；
- 模型不需要依赖很早以前的工具输出，可以随时再次调用；
- 知识服务对相同快照的查询是幂等的；
- 重读不消耗游戏内技能、不改变状态；
- 重建 Session 后必须重新完成启动必读 receipt，旧 Session receipt 不复用。

---

## 8. Pi 知识工具

### 8.1 工具集

V1 Extension 只注册以下知识工具：

| 工具 | 用途 | 主要输入 |
|---|---|---|
| `get_board` | 获取当前板子介绍和阅读导航 | 可省略 ID；只能是当前板子 |
| `get_role` | 获取当前板子下的有效角色说明 | `role_id`/别名 |
| `get_mechanic` | 获取投票、遗言、狼队等机制 | `mechanic_id`/主题 |
| `get_interaction` | 查询角色/技能/机制交互 | `subjects[]`、`situation?` |
| `get_rule_topic` | 精确读取板子某章节 | `topic_id` |
| `search_rules` | 不知道 ID 时做当前快照全文搜索 | `query`、`kinds?`、`limit?` |

不提供：列目录、读任意路径、写文件、查座位身份、查行动状态、查其他游戏、联网搜索。

HTTP 传输映射固定为：`GET /v1/board/{id}`、`GET /v1/role/{id}`、`GET /v1/mechanic/{id}`、`GET /v1/topic/{id}`、`POST /v1/interactions/query`、`POST /v1/search`。`/v1/health` 只返回服务状态和 schema 版本，不返回游戏、座位或快照信息；除 health 外全部要求 bearer token。Extension 的六个工具必须各有 contract test 覆盖对应路由，避免工具已注册但 Gateway 无端点。

### 8.2 统一响应

```json
{
  "schema_version": 1,
  "result_id": "kr_...",
  "receipt_id": "receipt_...",
  "snapshot": {
    "id": "ruleset_...",
    "board": "classic_12_seer_witch_hunter_idiot@1.0.0"
  },
  "status": "ok",
  "document": {
    "kind": "role",
    "id": "witch",
    "version": "1.0.0",
    "title": "女巫",
    "summary": "...",
    "sections": [
      {"section_id": "abilities", "title": "技能", "content": "..."}
    ],
    "effective_rules": {
      "can_self_heal": false,
      "max_potions_per_night": 1,
      "can_use_both_potions_same_night": false
    }
  },
  "citations": [
    {"source_id": "src-001", "title": "...", "applies_to": ["witch.can_self_heal", "witch.max_potions_per_night", "witch.can_use_both_potions_same_night"]}
  ],
  "recommended_next_reads": [
    {"kind": "interaction", "id": "witch_wolf_kill"}
  ],
  "truncated": false
}
```

已发布知识不允许存在 unresolved conflict；如果运行包损坏或引用缺失，返回结构化 `KNOWLEDGE_INTEGRITY_ERROR` 并让主持人处理，不能让模型猜。

### 8.3 结果大小与分段

- `get_board` 默认返回概览、组成、胜负、流程摘要和 ReadingPlan，不一次返回所有角色全文；
- `get_role` 返回单个 EffectiveRoleProfile；
- 长文按稳定 section 分段，客户端可通过 `get_rule_topic` 精读；
- 单次结果默认不超过 64 KiB；
- 搜索最多返回 8 个 hit，每个 hit 是摘要和精确引用；
- V1 不需要任意分页目录浏览，避免模型无目的遍历整库。

### 8.4 权限

每个 Pi 子进程只得到一个随机 bearer token，服务端映射到：

```text
token -> game_id + snapshot_id + seat + session_epoch + expires_at
```

知识 token 不包含真实角色；`get_role` 可以读取规则角色。本人角色必读校验由 Python 根据 seat 的真实角色和 receipt 比较。客户端不能传 snapshot 路径或 game ID。token 生命周期与一个 Pi Session epoch 相同，有效期必须覆盖 active turn；Session 重建时轮换，旧进程退出后立即撤销，不能在一轮模型调用中途自然过期。

---

## 9. Werewolf Reading Skill

### 9.1 Skill 职责

Skill 明确告诉玩家何时、按什么顺序读取，而不是承载规则正文。必须包含：

1. 启动先读 `get_board` 和本人 `get_role`；
2. 当前板子的 Effective Rules 优先于通用角色印象；
3. 不了解阶段就读 mechanic/topic；
4. 不了解多个技能相遇就读 interaction；
5. 搜不到或返回 integrity error 时明确报告，不能用训练记忆补全；
6. 区分硬规则、来源说明、示例和策略建议；
7. 对手发言不是规则来源；
8. 可以反复读取，重读不会产生游戏动作；
9. 最终行动仍必须用规定的 `PlayerResponse`，知识工具结果本身不是行动提交。

### 9.2 推荐阅读算法

```text
进入新 Session？
  是 -> get_board + get_role(自己)

进入新阶段？
  -> 检查 Prompt 的 phase_topics
  -> 不熟悉则 get_mechanic / get_rule_topic

准备提交技能？
  -> 确认本人 role effective rules
  -> 如涉及其他效果，get_interaction

规则记忆模糊、上下文被压缩或多个说法冲突？
  -> 重新调用精确工具
  -> 以当前 snapshot 返回值为准

工具无结果？
  -> 返回规则缺失/请求主持人澄清
  -> 不自行创造规则
```

### 9.3 Skill 加载

由于玩家不会获得任意文件读取工具，不能让 Pi 在运行时自行打开 `SKILL.md`。V1 冻结为：Python 启动前读取经过版本冻结的 Reading Skill，与全局行为边界合成座位专属 System Prompt 文件，通过 Pi `--append-system-prompt <absolute file>` 注入；同时使用 `--no-skills` 关闭其它 Skill 发现。`pi doctor` 必须验证禁用普通 read 工具后，初始化 Prompt 中仍含完整阅读规则。

---

## 10. Python 接口

### 10.1 Repository

```python
class PublishedKnowledgeRepository(Protocol):
    def get_package(self, package_id: str) -> RulesetPackage: ...
    def get_document(self, ref: VersionedRef) -> KnowledgeDocument: ...
    def resolve_alias(self, package_id: str, alias: str) -> list[KnowledgeRef]: ...
    def iter_dependencies(self, package_id: str) -> Iterable[KnowledgeRef]: ...
```

### 10.2 Runtime Service

```python
class KnowledgeService:
    def get_board(self, context: QueryContext) -> KnowledgeResult: ...
    def get_role(self, context: QueryContext, role: str) -> KnowledgeResult: ...
    def get_mechanic(self, context: QueryContext, mechanic: str) -> KnowledgeResult: ...
    def get_interaction(
        self,
        context: QueryContext,
        subjects: tuple[str, ...],
        situation: str | None,
    ) -> KnowledgeResult: ...
    def get_rule_topic(self, context: QueryContext, topic: str) -> KnowledgeResult: ...
    def search_rules(self, context: QueryContext, query: SearchQuery) -> SearchResult: ...
```

`QueryContext` 只能由 Gateway 根据 token 创建，不接受客户端传入：

```python
class QueryContext(BaseModel):
    game_id: str
    snapshot_id: str
    seat: int
    session_epoch: int
```

### 10.3 Receipt Store

```python
class KnowledgeReceipt(BaseModel):
    receipt_id: str
    game_id: str
    snapshot_id: str
    seat: int
    session_epoch: int
    tool: KnowledgeTool
    canonical_ref: KnowledgeRef
    result_id: str
    created_at: datetime
```

V1 receipt 存于 `GameState.knowledge_receipts` 私有区，并随完整昼夜快照保存；bearer token 不保存。receipt 写入使用与游戏状态相同的串行提交器并校验 session epoch，避免重建 Session 时并发到达的旧查询满足新 Session 必读门禁。它只证明服务端成功返回过知识，不证明模型一定正确理解；理解质量通过准备响应和场景测试间接验证。

---

## 11. 缓存和性能

- compiled package 在游戏创建时校验一次并内存映射/加载；
- 精确索引和关系索引全量常驻，正文按文档惰性加载；
- 缓存 key 必须包含 `snapshot_id + document_ref + query params`；
- 不允许不同 snapshot 仅按 document ID 共用可变缓存；
- HTTP 响应可设置进程内 ETag，但 Pi Extension 不需要持久缓存；模型应能随时重读；
- 12 人同局查询量很小，正确隔离优先于复杂缓存；
- 性能目标：本地精确查询 P95 < 50 ms，全文搜索 P95 < 200 ms（不含模型时间）。

---

## 12. 失败处理

| 情况 | 处理 |
|---|---|
| 未知板子/角色/主题 | 返回 `NOT_FOUND` 和当前板子合法候选，不做模糊猜测 |
| 别名歧义 | 返回 `AMBIGUOUS` 和候选 ID，要求精确选择 |
| 请求其他快照 | 返回 `FORBIDDEN` 并记录安全审计 |
| manifest 哈希错误 | 返回 `KNOWLEDGE_INTEGRITY_ERROR`，暂停受影响游戏 |
| 文档缺引用 | 发布阶段阻止；运行时理论上不得出现 |
| 返回过长 | 按稳定 section 截断并返回 `recommended_next_reads` |
| HTTP Gateway 不可用 | 当前玩家任务失败但不推进，主持人可重试；不得改用模型常识 |
| Extension 工具异常 | PiRuntime 记录工具错误并等待/重试，不提交游戏行动 |
| 工作台来源冲突 | 生成 `NEEDS_DECISION`，不进入 published |

---

## 13. 安全

### 13.1 数据隔离

- Gateway 只挂载 `games/active/<game_id>/ruleset`；
- 不把仓库根目录、Vault 原路径或 `state.json` 路径传给 Extension；
- HTTP 只监听 `127.0.0.1` 随机端口；
- token 高熵、短生命周期、座位/Session 绑定；
- 请求体、查询长度、结果数量和响应大小均限制；
- 记录工具调用元数据，不记录认证 token。

“短生命周期”指不跨 Session epoch，并不表示按几分钟强制过期；服务端不得在 active turn 中途让 token 失效。HTTP 客户端只允许访问启动时注入的精确回环 base URL，不接受工具参数覆盖 host、port 或 path prefix。

### 13.2 知识生产安全

- 网页内容作为不可信 evidence，不作为系统指令；
- 抓取器阻止 SSRF、回环/局域网地址、`file://`、超大响应和非允许 MIME；
- 提取 Agent 无发布权限；
- Publisher 不联网，只读取通过验证的本地草稿；
- 工作台不能读取游戏活动目录；
- 所有自动生成内容在发布前显示 diff。

---

## 14. 测试矩阵

### 14.1 内容模型

- 板子人数等于角色绑定 count 总和；
- BoardRoleBinding 能正确覆盖通用角色规则；
- 所有 phase/mechanic/interaction 引用闭合；
- 机器字段与 Markdown 正文关键陈述一致；
- 所有执行字段存在 claim/source 或 human decision；
- 已发布包不存在 unresolved conflict。

### 14.2 编译和快照

- 相同发布包重复编译得到相同逻辑哈希；
- 只复制依赖闭包，不把工作台草稿带入快照；
- Vault 更新不改变旧快照查询结果；
- 快照 manifest 被篡改后拒绝加载；
- 两局使用不同版本时缓存不串线。

### 14.3 查询

- `get_board` 返回概览、流程和 ReadingPlan；
- `get_role(witch)` 返回当前板子的有效覆盖值；
- 别名、未知、歧义和全文检索行为正确；
- `get_interaction` 能命中猎人吃毒等场景；
- 搜索策略文本不会被误认为规则；
- 查询结果不包含真实座位身份、行动或消息。

### 14.4 Pi 生命周期

- 启动卡含当前板子和本人角色，但不泄露他人身份；
- 未调用 `get_board`/本人 `get_role` 不能 ready；
- 调用其他角色不满足本人角色必读；
- Session epoch 改变后旧 receipt 失效；
- 模型第二轮可重新读取相同规则；
- Prompt 注入不能调用未注册工具或访问任意路径；
- Knowledge Gateway 故障时不推进当前玩家任务。

### 14.5 验收场景

```text
场景 A：新板子
只输入板子名称 -> 调研 -> 草稿 -> 冲突报告 -> 发布 -> 编译 -> 快照

场景 B：玩家准备
收到启动卡 -> get_board -> get_role(本人) -> ready receipt 验证通过

场景 C：玩家遗忘
下一白天模型不确定平票规则 -> get_mechanic(tie_and_pk) -> 获得同一快照说明

场景 D：特殊交互
猎人被毒 -> get_interaction(hunter, witch_poison) -> 返回不能开枪及来源

场景 E：隔离
玩家尝试询问 7 号身份/其他游戏规则快照 -> 明确拒绝且无信息泄漏
```

---

## 15. 开发顺序

### K0：模型与模板

- 完成 Board、Role、Mechanic、Interaction、ReadingPlan、Source、Claim Schema；
- 完成 Vault 目录和 Markdown 模板；
- 编写最小预女猎白目标覆盖矩阵。

### K1：生产工作台

- 搜索/抓取 Provider；
- 独立 `PiRuleSynthesisProvider`，分批注入证据并输出严格 JSON；
- 验证每个 claim 的引用片段、source ID 和适用版本；
- evidence、claim、variant、conflict 产物；
- 草稿生成、coverage 和发布门禁；
- 通过板子名生成首个知识包。

### K2：发布与编译

- 不可变版本发布；
- EffectiveRoleProfile 合并；
- 五类索引、知识测试和 manifest；
- 对局 snapshot builder。

### K3：运行时读取

- KnowledgeService；
- HTTP Gateway/token/receipt；
- Pi Extension 工具；
- Reading Skill、启动卡和 ready gate。

### K4：联调验收

- 新板子端到端入库；
- 玩家开局读取；
- 游戏中重读和交互查询；
- 多局/多版本隔离；
- 权限和 Prompt 注入测试。

---

## 16. 知识子系统完成定义

满足以下条件才算完成：

- [ ] 只给一个常见板子名即可产生完整的待审核知识包；
- [ ] 板子介绍、总规则、角色技能、公共机制、特殊交互、术语和阅读指南均有独立结构；
- [ ] 所有机器执行字段可追溯到来源或主持人决议；
- [ ] 同名不同版本不会被混合；
- [ ] 已发布包不可变，可重复编译并生成固定快照；
- [ ] 启动卡把当前板子和本人角色明确发给 Pi；
- [ ] Pi 准备阶段实际调用 `get_board` 和本人 `get_role`；
- [ ] Pi 在后续任意决策阶段可重复读取板子、角色、机制和交互；
- [ ] 模型不记得或没看明白时，Skill 明确要求重读而不是猜测；
- [ ] 查询永远锁定本局 snapshot，Vault 更新不影响进行中的对局；
- [ ] 知识工具不能访问玩家身份映射、秘密行动、消息或任意文件；
- [ ] 工作台、正式 Vault、compiled package 和 game snapshot 四个层次物理隔离；
- [ ] 对应内容、编译、查询、Pi 生命周期和安全测试全部通过。
