# Werewolf Arena V1 开发前技术评审

> 评审日期：2026-09-27  
> 评审对象：PRD、总技术方案、知识库技术设计、目标 Windows 环境  
> 结论：架构和主流程可以开始开发；Agent Reach 在宿主机可用，真实模型联调仍需配置 Pi provider/model  

---

## 1. 总结结论

当前方案已经形成四个闭环：

1. 板子名 -> 联网证据 -> 规则主张 -> 知识草稿 -> 验证发布；
2. 发布知识 -> 编译索引 -> 单局快照 -> Pi 动态读取/重读；
3. 独立 Pi Session -> 增量消息 -> 结构化响应 -> 基础校验；
4. 主持裁定 -> 权威状态 -> 完整昼夜快照 -> 最终时间戳归档。

不存在必须先安装 Obsidian、数据库或向量数据库才能开发的问题。知识库本体是项目内版本化 Markdown/YAML/JSON 和 Python 生成的只读索引。

可以立即开始的工作：项目骨架、领域模型、规则 Schema、知识编译器、Fake/ScriptedRuntime、状态机、权限路由和单元测试。

在真实 Pi 模型联调前必须完成：配置至少一个 Pi provider/model。

Agent Reach 在宿主机环境可用；自动规则调研必须从有网络与用户配置访问权的宿主机进程调用，不能把 Codex 受限沙箱内的启动失败判定为工具损坏。

---

## 2. 环境核验

| 项目 | 状态 | 核验结果 | 是否阻塞 |
|---|---|---|---|
| uv | 通过 | `uv 0.10.12`；沙箱外默认缓存和 Python 发现正常 | 否 |
| Python 3.11 | 通过 | uv 可发现 CPython `3.11.15`，另有系统 `3.11.9` | 否 |
| Pi | 通过 | `0.87.1`，入口 `C:\nvm4w\nodejs\pi.cmd` | 否 |
| Pi RPC/CLI | 通过 | `--mode rpc` + `get_state` 冒烟成功；`--help` 核对所需开关；CPython 3.11 以 argv 直接运行 `pi.cmd` 成功 | 否 |
| Pi TypeScript Extension | 通过 | 自带 `hello.ts` 可直接加载；Pi 使用 jiti | 否 |
| Pi provider/model | 未就绪 | `pi --offline --list-models` 返回 `No models available` | 阻塞真实模型联调 |
| Agent Reach | 通过（宿主机） | 入口 `C:\Users\x7859\.local\bin\agent-reach.exe`；需在沙箱外运行以访问网络和用户配置 | 否 |
| Agent Reach 后端 | 运行时路由 | 由 `agent-reach doctor --json` 在实际宿主执行上下文选择；不要求每个后端命令都出现在 Codex 沙箱 PATH | 否 |
| Node | 可用 | 当前环境 Node 24；Pi 安装自带可用 Node 入口 | 否 |
| npm/TypeScript compiler | 未发现但不需要 | Extension 由 Pi 直接加载，无第三方 npm 依赖 | 否 |
| Git | 可用 | Git 2.51.0；当前目录尚未初始化仓库 | 不阻塞，强烈建议开工前初始化 |
| Obsidian | 未核验/不要求 | 仅作为可选 Markdown 浏览编辑器 | 否 |

注：Codex 沙箱与宿主机的 PATH、网络和用户配置访问能力不同。Pi 与 Agent Reach 均应由生产代码支持显式可执行文件路径和启动前 doctor；Agent Reach 必须在允许联网及读取其用户配置的宿主环境执行。

---

## 3. 反向流程评审

### 3.1 从“开始一局”反推

前置条件均有明确产物：

- 玩家配置通过 Pydantic 校验；
- Pi executable、版本、provider/model 通过 `pi doctor`；
- 板子版本为 `published`；
- compiled package 和 manifest 验证通过；
- 单局 ruleset snapshot 创建完成；
- 身份数量等于人数；
- 每个座位 Session、token 和目录唯一。

缺少任一条件时 `start` 明确失败，不允许降级成模型常识或共享 Session。

### 3.2 从“玩家作出一次决策”反推

流程闭合：

```text
ActionWindow
 -> 授权事件 peek（不推进游标）
 -> TurnRequest
 -> Pi prompt accepted
 -> message/tool events
 -> agent_settled
 -> get_last_assistant_text
 -> PlayerResponse Schema
 -> ActionValidator
 -> GM Resolution
 -> 权限事件发布
 -> ack 游标和提交 revision
```

迟到、重复、旧 Session epoch、错误 request/window、非法角色和非法目标均在提交前被拒绝。

### 3.3 从“玩家忘记规则”反推

流程闭合：启动卡持续提供板子/角色 stable refs；所有决策阶段都可调用六类知识工具；工具固定绑定 snapshot；重读无副作用；服务故障时当前任务失败但游戏不推进。

### 3.4 从“增加一个新板子”反推

流程闭合：板子名 -> Research Job -> Evidence -> Claim -> Variant -> Draft -> Coverage/Conflict -> Human Diff -> Publish -> Compile -> Snapshot。工作台草稿不会进入玩家查询。

### 3.5 从“主进程崩溃”反推

边界明确：只承诺最近完整昼夜快照；检测 Session/状态不一致；只能新建 Session 重放该玩家截至快照的授权信息，或放弃本局；不允许猜测续接。

---

## 4. 本轮评审发现并已修正的问题

| 编号 | 原问题 | 已冻结方案 |
|---|---|---|
| REV-01 | 文档仍写本机未发现 Pi | 更新为 Pi 0.87.1 实测基线 |
| REV-02 | RPC 完成边界描述仍偏抽象 | `agent_settled` 后调用 `get_last_assistant_text`；`agent_end` 不算 settle |
| REV-03 | abort 可能遗留 steer/follow-up | 固定先 `clear_queue`，再 `abort` 并等 idle |
| REV-04 | Skill 在禁用 read 后如何加载不唯一 | Python 展开后用 `--append-system-prompt` 注入，Pi 使用 `--no-skills` |
| REV-05 | TypeScript 是否需要编译未定 | Pi 0.87.1 jiti 直接加载 `.ts`；不安装 npm/tsc |
| REV-06 | UUIDv7/UUIDv4 摇摆 | V1 全部使用标准库 UUIDv4 + 独立时间戳/序号 |
| REV-07 | 中文全文检索没有可实现算法 | NFKC + 原文子串 + 中文单字/二元/三元 n-gram |
| REV-08 | 规则提取由哪个 LLM 执行不明确 | 独立 `PiRuleSynthesisProvider`，不复用玩家 Session，无发布权限 |
| REV-09 | Windows Pi 进程清理不明确 | 先做有序退出；超时升级 terminate/kill，stdout/stderr reader 必须收尾 |
| REV-10 | PRD 仍把保存边界列为未决 | 固定完整昼夜快照 + 结束时间戳归档 |
| REV-11 | Schema 向前兼容策略不明确 | V1 仅接受 schema 1；未知版本明确拒绝，无隐式迁移 |
| REV-12 | 自动 retry 可能越过游戏硬超时 | Pi auto retry 关闭，重试由 Python/主持人控制；auto compaction 保持开启 |
| REV-13 | 六个知识工具只定义了四个业务 HTTP 路由 | 补齐 mechanic/topic 路由，并要求六工具逐一做 Extension-Gateway contract test |
| REV-14 | `reviewed` 与 `published` 两套正式状态冲突 | 正式终态统一为 `published`；审核信息拆为独立元数据 |
| REV-15 | 单个 `action` 无法表达女巫同夜双药 | 改为 `actions[]` 原子 bundle；普通窗口 1 项、女巫窗口最多 2 项 |
| REV-16 | 并发投票可能发生内存 revision 丢更新 | 单一状态提交器 + lock + revision CAS；外部 I/O 永不持锁 |
| REV-17 | Windows 只终止 `pi.cmd` 可能遗留 Node 子进程 | 使用 Kill-on-close Job Object 管理完整进程树并纳入 `pi doctor` |
| REV-18 | receipt 声称会存档但 `GameState` 无字段 | 增加私有 `knowledge_receipts`，与 session epoch 一起校验和快照 |
| REV-19 | 编译产物含时间时无法满足可重复哈希 | 逻辑哈希排除时间/绝对路径，固定 UTF-8/LF/排序规范 |
| REV-20 | 配置示例含未定义的 `max_steps` | 移除该字段；V1 用软/硬超时和 Pi 终态控制任务边界 |
| REV-21 | 新仓库可能误提交 Session、身份 Prompt 和私密存档 | 冻结 `.gitignore` 范围；共享对局必须走显式脱敏导出 |
| REV-22 | Pi 子进程继承全部环境会扩大凭证暴露面 | 按 OS 必需项 + 当前 provider 凭证 + 本座位知识 token 建 allowlist |

---

## 5. 尚未完成但不属于架构歧义的事项

### 5.1 Pi 模型认证

至少选择一个 provider/model，完成 Pi 登录或环境变量配置，然后验证：

```powershell
pi auth check --provider <provider> --json
pi --list-models
```

不要把 API key 写入 `players.yaml`、`.env` 后提交、游戏状态或归档。程序只继承必要的凭证环境变量，日志必须脱敏。

### 5.2 规则调研 Provider

方案默认 Agent Reach/HTTPS，Agent Reach 已在宿主机安装并可用。`RuleResearchProvider` 必须通过可配置的绝对入口在宿主机执行，并在调研任务开始前运行 `agent-reach doctor --json` 记录 active backend/capabilities；不要先在受限沙箱里运行后据其失败判定宿主不可用。开发初期仍可用 fixture 和 `rules import-evidence` 完成确定性模块，M1 则增加真实宿主调用的 contract/integration test。

若宿主 provider 临时不可用，`rules research` 必须报错并保留 `rules import-evidence` 回退，不允许让 Pi 凭训练记忆生成无来源规则。

### 5.3 首板子知识发布

目前只有规则目标基线和 Schema 方案，还没有实际完成“预女猎白”的联网证据包、冲突报告和发布版本。它是 M1 的交付，不是开写代码前需要手工补完的输入。

### 5.4 Git 仓库

当前目录不是 Git 仓库。开发可以进行，但失去安全回滚和变更审查能力。建议在创建项目骨架前初始化 Git，并先提交现有三份设计文档和本评审报告。

---

## 6. 安装与配置结论

### 必需

- uv：已安装；
- CPython 3.11：已安装；
- Pi 0.87.x：已安装；
- 至少一个 Pi 模型 provider 的认证：**尚未配置**；
- 规则调研搜索 Provider：Agent Reach **宿主机可用**，项目需实现沙箱外调用适配器与 doctor 门禁；
- Python 包：项目初始化后由 `uv sync` 自动安装，不手工 pip install。

首次创建项目时按方案的版本范围执行：

```powershell
uv init --package --python 3.11
uv add "aiohttp>=3.10,<4" "pydantic>=2.9,<3" "pyyaml>=6,<7" "rich>=13,<15" "typer>=0.12,<1"
uv add --dev "mypy>=1.11,<2" "pytest>=8,<9" "pytest-asyncio>=0.24,<2" "pytest-cov>=5,<8" "ruff>=0.8,<1"
uv lock
uv sync --locked
```

### 不需要安装

- Obsidian；
- PostgreSQL、SQLite 服务或其它数据库服务器；
- Chroma、Milvus、Elasticsearch 或任何向量数据库；
- npm、TypeScript、tsc；
- 独立 Web 服务器。

这里的“知识库”不是一个需要另外部署的软件产品：运行时由项目内 Markdown/YAML/JSON、Python 编译器/索引和内嵌 `aiohttp` 回环服务组成，全部 Python 包由 uv 锁定和安装。

### 可选

- Obsidian：便于人工浏览和编辑 `vault/published`；
- Git 仓库/远程：强烈建议用于回滚；
- Windows 独立用户或容器：未来加载不可信 Extension 时再做强隔离。

---

## 7. 推荐开工顺序

在真实模型和联网搜索尚未配置完成时，也可以无阻塞推进：

1. 初始化 Git（建议）和 uv Python 3.11 项目；
2. 建立 Pydantic Schema、枚举、错误类型和配置解析；
3. 实现知识内容模型、模板、编译器和索引；
4. 实现 `ScriptedRuntime`、Pi JSONL protocol parser 和录制 fixture；
5. 实现 KnowledgeService/Gateway，并用假 Extension 客户端测试；
6. 实现状态机、消息路由和 ActionValidator；
7. 配好 Pi provider 后完成真实两轮 RPC 与自研 Extension Spike；
8. 接通宿主机 Agent Reach Provider 后完成“预女猎白”入库；
9. 再做真实模型参与的完整昼夜闭环。

建议把第 7、8 步作为两个独立的技术 Spike，未通过前不要并行铺开十二玩家长局或更多板子。

---

## 8. Go / No-Go

| 开发活动 | 当前结论 |
|---|---|
| 项目骨架和领域开发 | GO |
| 知识 Schema/编译/查询开发 | GO |
| Fake Runtime 游戏闭环 | GO |
| Pi RPC 协议实现 | GO（本地协议和 Extension 已验证） |
| 真实模型调用 | NO-GO，先完成 provider/model 认证 |
| 12 个 Pi 的长局运行 | NO-GO，先通过 Windows Job Object 进程树回收 contract test |
| 自动联网规则调研 | GO（宿主机 Agent Reach 可用；先完成 Provider contract test） |
| 安装 Obsidian | 不需要 |

最终反向评审后没有剩余的架构级未决项。仍需在实现阶段通过 Spike 验证的外部集成事实是：Pi provider/model 认证、Windows 进程树回收，以及项目对宿主机 Agent Reach 的调用 contract；它们已有明确失败策略，不要求改动领域模型。
