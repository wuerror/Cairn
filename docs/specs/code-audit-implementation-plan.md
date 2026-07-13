# Code Audit —— 实现计划

> 蓝本：[`code-audit-design.md`](./code-audit-design.md) 已拍板决策 #1–#14。
> 协议基线：[`server-protocol.md`](./server-protocol.md)。
> 本文只讲**怎么落地**，不重述设计动机；每个任务标注对应决策号与要改的真实文件。

---

## 0. 原则与范围

- **不重写引擎。** lease / heartbeat / conclude 双阶段 / OODA 循环全部复用。改动是"扩数据模型 + 窄腰 emit + 新增 verify 相 + 新增基座层"。
- **窄腰红线（决策 #5）。** 模型只吐扁平 `emit schema`；`id` / `code_version` / `intent_id` / `batch_id` / `confidence` 门控全在 server。任何"让模型填富 schema"的实现都要打回。
- **append-only 红线（决策 #6）。** 没有任何 fact 被就地改写。confidence 升级 = 追加 `type: verification` fact；有效 confidence 是 server 折叠视图。
- **分期交付。** P0 不需要测试环境即可跑通（富 fact + 窄腰 + 1→N conclude + 折叠 + origin 解析）；P1 加持久化与真实子图检索；P2 引入 verify 相与发包路径（含合规骨架）；**P3 以「能实际跑通」为唯一优先级**——代码挂进 explore、payload 能实例化、配置/demo 可一键演示。细扣合规（mitm/netns）后置，不阻塞实测。
- **前端同期适配（本版新增）。** 前端是一个**单文件静态 SPA**（`server/static/index.html`，Alpine.js + Cytoscape + Tailwind，无构建步骤，server 直接托管）。富 fact 的 `type`/`confidence`/`locations`/`verifies` 若不改前端就在 UI 上隐形，且决策 #7 的附属 fact 会变成图上孤点。每个后端阶段配一条前端泳道（`P0.8`/`P1.5`/`P2.7`/`P3.1`）。

### 就绪度对照

| 层级 | 状态 | 落在阶段 |
|---|---|---|
| 问题形状 / 与 Cairn 关系 / append-only confidence / 1→N conclude / verify 相 / origin JSON | 已定 | — |
| 富 Fact + 窄腰 emit + 折叠视图 + origin 解析 | 已落地 | **P0** |
| 跨 run 持久化 + `relevant_subgraph` 真实实现 + 防腐 | 已落地 | **P1** |
| 真实验证：verify 相 + capability + 双容器 + harness + Brief 组装 + 合规骨架 | 骨架/围栏已落地 | **P2** |
| **端到端可跑：代码挂载 + payload 实例化 + 示例配置 + demo 靶标** | **已落地** | **P3** |
| mitm 透明代理 / 容器 netns 硬隔离 / Codebase 拆表 / 向量检索 | 可后置 | **Later** |
| 前端适配（富 fact 可视化 + 基座面板 + 合规面板 + 建项结构化） | 已落地（含结构化建项） | **P0–P3** |

---

## 现状锚点（已核对代码）

| 关注点 | 现状 | 位置 |
|---|---|---|
| Fact 结构 | 仅 `id` + `description` | `server/models.py:Fact`、`server/db.py` facts 表 |
| origin/goal | 作为**保留 id 的 fact** 种入图（`{origin, goal}` 触发 bootstrap 判定） | `db.py` facts 表、`loop.py:_is_initial_project` |
| conclude | 1 intent → 1 fact（`ConcludeResponse{fact, intent}`） | `server/models.py`、`dispatcher/contracts.py` |
| explore emit | 只收 `{"description"}` | `contracts.py:validate_explore_payload` |
| Intent 边 | `from[]`（`intent_sources` 表）→ `to`（`to_fact_id`） | `db.py` intents/intent_sources |
| 任务类型 | `reason \| explore \| bootstrap` | `config.py:TaskType` |
| worker 路由 | 只按 `task_types` 过滤 + `priority/running/random` 排序 | `loop.py:_select_worker`、`worker_select.py` |
| 容器 | 一 project 一 container，单 image/network/cap | `config.py:ContainerConfig`、`runtime/containers.py` |
| 存储 | SQLite（WAL），schema 内联 + 轻量迁移 | `server/db.py` |
| 前端 | 单文件静态 SPA（Alpine + Cytoscape + Tailwind，无构建）；节点配色/详情/表单只认 `id`+`description`，`nodeType` 靠 id 算（origin/goal/其余） | `server/static/index.html`（`buildElements`:2143、node style:2209+、fact 面板:634-681、conclude 表单:865/3333） |

---

## P0 —— 富 Fact + 窄腰 emit + 1→N conclude + 折叠视图 + origin 解析

**目标：不接触测试环境，把"审计侧"跑通。** 静态 worker 能吐富 observation，server 落成富 fact 并去重，Reason 能读带 type/有效 confidence 的图，origin 可解析。

### P0.1 数据模型与存储（决策 #6/#7，设计要点 6）
- [ ] `server/db.py`：facts 表加列 `type`、`confidence`、`locations`(JSON)、`code_version`、`evidence`、`verifies`、`intent_id`、`batch_id`；走 `_ensure_*_columns` 式轻量迁移（对旧行 `type='sink'? ` 不假设——给 `NULL`/`legacy` 缺省，见迁移注记）。保留 `origin`/`goal` 两条保留 fact 兼容。
- [ ] `server/models.py:Fact`：加对应字段；`type`/`confidence` 用 `Literal`；`locations: list[str]`。**注意** `origin`/`goal` 仍是合法 fact，字段可空。
- [ ] `server/services.py`（业务层）：
  - `id` 分配沿用 `scoped_counters`；
  - **去重合并键 = `type + sorted(locations)`**，命中则 `locations` 取并集、描述冲突保留先写（差异落 hint 可选）；`type=verification` 与空 `locations` 的 `constraint` 走退化键 `verifies + why_failed.reason`；
  - `code_version` **由 server 现场哈希盖章**（拿到 `locations` 后按内容 hash 或 project origin 的 commit）；模型不填。

### P0.2 窄腰 emit + 1→N conclude（决策 #5/#7）
- [ ] `contracts.py`：新增 `validate_explore_payload` 的富形态——收 `{"observations": [{type, description, locations, evidence?, oracle_draft?}]}`（扁平、少字段）。**保留**旧 `{"description"}` 兼容路径（映射为单条 `type=?` observation）。
- [ ] `server` conclude 端点：`ConcludeRequest` 支持一次写 **N facts**；`ConcludeResponse` 返回 `facts: [...]` + `intent`。`intent.to` = **主 fact**（规则：`explore` → 优先 `dataflow`，否则第一个 `sink`/`source`；`verify` → 该 verification fact，见 P2）；其余 N-1 facts 盖 `intent_id`/`batch_id` 归属。
- [ ] `dispatcher/tasks/explore.py`：解析富 payload，多条 observation 一次性 conclude。
- [ ] **门控**：server 拒绝模型声明 `reachable-confirmed`/`poc-confirmed`/`refuted`（P0 审计侧最高 `static-confirmed`）。

### P0.3 折叠视图（决策 #6）
- [ ] `server/services.py`：实现 `effective_confidence(fact)` —— 取指向该 fact 的最新未过期 verification fact 档位；无则回落自身档位并置 `stale`（**仅视图、不落库**）。P0 尚无 verification fact，等价于返回自身档位；先把接口与 export 字段占好。
- [ ] `server/routers/export.py` & `ProjectDetail`：fact 序列化带 `type`/`effective_confidence`/`stale`/`locations`。

### P0.4 origin 结构化（决策 #9）
- [ ] `server/models.py:CreateProjectRequest`：`origin` 由自由文本升为 **JSON 字符串**并校验形状 `{codebase:{path,commit}, target:{base_url,credentials_ref}, allowlist:[...]}`。仍作为保留 `origin` fact 的 description 落库（不新增表）。
- [ ] `credentials_ref` **只存引用**；P0 不解析凭证内容。

### P0.5 prompt 语义（决策 #14 之外，纯 prompt）
- [ ] `prompts/default/explore.md` / `explore_conclude.md`：改为吐富 observation（含 `type`/`locations`），可选 `oracle_draft`（给 P2 Brief 组装用）。
- [ ] `prompts/default/reason.md`：complete 判据升级——末端 sink 需 `poc-confirmed`（无测试环境时允许人工 Hint 顶）；读图时消费 `type`/`effective_confidence`。
- [ ] `config.py:DEFAULT_PROMPT_REQUIRED_TOKENS`：同步新占位符校验。

### P0.6 协议文档
- [ ] `server-protocol.md`：补"代码审计扩展"节——富 fact 字段、`type: verification` + `verifies` 边、append-only confidence（**显式说明 confidence 是派生视图、fact 仍只增不改**）、1→N conclude、`intent.to=主 fact`。这是把 §"Fact 只有描述文本、无状态标记"与新字段调和的地方。

### P0.7 测试（mock 通道）
- [ ] `config.py:MOCK_ALLOWED_OUTCOMES`/`MOCK_DEFAULT_BEHAVIOR`：explore 增加"多 observation"产出形态。
- [ ] 单测：去重并集、`code_version` 盖章、confidence 门控拒绝越权档位、1→N conclude 的 `to`=主 fact、折叠视图 P0 退化正确。

### P0.8 前端补齐（`server/static/index.html`）
> P0 后端已让富 fact 落库，但 UI 全是同色方块 + 若干孤点，等于白存了 `type`。这几项应**紧跟 P0 做**。
- [ ] **按 `fact.type` 配色/形状 + 图例。** `buildElements()`（`2143`）现在 `nodeType` 只算 `origin/goal/fact`；改为读 `f.type` 映射出 `source/sink/dataflow/constraint/verification`，在 cytoscape style（`2209+`）加对应 `node[nodeType="..."]` selector，补一个图例块。负向 `constraint` 要有一眼可辨的负向视觉（如红/虚线）。
- [ ] **修 batch_id 孤岛节点（真回归）。** `buildElements` 只在 `intent.from → intent.to` 画边（`2156-2160`）；决策 #7 的 N-1 附属 fact 不是任何 intent 的 `to`，会无入边飘着。方案：给同 `batch_id` 的附属 fact 画到主 fact 的"同批"虚线边，或收进主 fact 的复合/分组节点。底线是不能是孤点。
- [ ] **fact 详情面板显富字段。** `634-681` 现只显 `description` + 产出 intent；加 `type` 徽章、`effective_confidence`（带 `stale` 标记）、`locations`（file:line 列表）、`evidence`。
- [ ] **confidence 视觉阶梯。** 节点边框/透明度按 `hypothesized → static-confirmed → …` 编码，让"多确定"可视。
- [ ] **origin 现在是 JSON。** `origin` fact 详情（`619-624`、`650-668`）改为解析 JSON 展示 `codebase`/`target`/`allowlist`，而非 raw 文本。
- [ ] （可选）conclude/complete 表单（`865`、`892`）允许人工产 fact 时选 `type`/填 `locations`；并反映 server 门控（人工最高 `static-confirmed`）。

**P0 出口判据**：mock/真实静态 worker 能对一个只读代码库产出带 type/locations 的富 fact 图，重复发现自动合并，Reason 能按有效 confidence 读图；全程无 fact 被就地改写；origin 可解析出 allowlist/commit；**UI 上 fact 按 type 区分配色、附属 fact 不再是孤点、详情面板可见 type/confidence/locations**。

---

## P1 —— 跨 run 持久化 + `relevant_subgraph` 真实检索 + 基座层 + 防腐

**目标：同一 codebase 多 goal 复用，Reason 不再吃全量图。** 仍不发包。

### P1.1 结构边与子图检索（决策 #4/#12）
- [x] `server/services.py`：实现 `relevant_subgraph(project, goal)`：
  1. goal → 终态 sink 类型：**v1 小映射表**（`RCE→{exec,deserialize,ssti,template}` 等）+ sink `description` 关键词兜底；
  2. 从匹配 sink 沿 **Intent provenance（`from[]→to`，即 `intent_sources`+`to_fact_id`）反向 BFS N 跳**；`type` 只作节点标签；
  3. 结果按 **有效 confidence 过滤掉已 `refuted` 路径**，再按 `batch_id` 附上同批附属 fact（`source`/`constraint`）。
  4. **fail-closed**：有 typed sinks 但 goal 关键词无一命中时，**不** seed 全部 sink；仅返回 `origin/goal` + constraints + 触及它们的 open intents（避免跨 run 后 goal #2 吃到无关 sink 图）。
- [x] server 暴露 `relevant_subgraph` 接口；`reason` 任务取数从"全量 export"切到该接口。**接口 P0 就可先存在并返回全部**（决策 #4），P1 换实现、不动 prompt。
- [x] `dispatcher/tasks/reason.py` & `prompts/default/reason.md`：`{graph_yaml}` 来源改为子图切片。

### P1.2 持久化 + 防腐（决策 #3，设计要点 5，已知限制 5）
- [x] 存储：跨 run 为 **copy-on-create**（同 `codebase.path` 的 sibling project 建项时导入 facts + intent 脊柱 + 空目标的 base_knowledge），**非** live shared store；canonical 去重键为 `type + sorted(locations)`；goal 用 project 级 goal fact 表达（**v1 不拆表**）。
- [x] 折叠视图：`stale` = **verification_stale only**——仅当指向该节点的 verification 因 `code_version` 失配过期时标 stale 并回落自身档位；无 verification 的静态节点即使 own `code_version` 与当前 commit 不同也不标 stale。
- [x] 跨 run 去重升级为硬需求：写入前按 canonical key 查已存在 fact。

### P1.3 基座知识层（设计要点 1/2/3，第二存储约束）
- [x] 新增 `base_knowledge` 存储（**协议外第二存储，非图节点**）：`version` + `entries[]`(kind/statement/evidence/confidence/revised_by) + `routing_map`。
- [x] `bootstrap` 任务产出基座（`prompts/default/bootstrap*.md` 扩语义）；worker 只读、带 `version`。
- [x] **patch 规则**：`revised_by` 必须由冲突 fact 触发并留审计（谁/因哪条 fact/何时）；worker 执行中 `version` 变了要在 conclude 前重拉。
- [x] **explore → BK patch 最小闭环**：explore emit 可选 `base_knowledge_patches[{entry_id, statement?, evidence?, confidence?}]`（不填 `revised_by`/`version`）；conclude 后 server 用本批主 fact 作 `revised_by` 调 PATCH；非法 `entry_id` skip 不回滚 facts。
- [x] `kind: auth` 条目支持"早期接地"占位（P2 才真接地）。

### P1.4 测试
- [x] 反向可达遍历正确（含 constraint、剔除 refuted）；关键词 miss fail-closed；跨 run 复用不重建；基座 patch 审计链完整；conclude 带 patches；verification 过期才 stale。

### P1.5 前端（`server/static/index.html`）
- [x] **基座知识 / routing_map 面板（图外第二存储，UI 今天完全没有）。** 新 tab/面板显 `base_knowledge.entries`（kind/statement/evidence/confidence/revised_by）+ `routing_map`，并显 `version` 与 `revised_by` 审计链（谁/因哪条 fact/何时）。
- [x] **按 goal 过滤/高亮子图。** Reason 现在吃 `relevant_subgraph`；跨 run 大图会糊，UI 加"聚焦当前 goal 相关子图"开关（前端过滤或调用 `relevant_subgraph` 接口）。
- [x] **`stale` 节点显式标记。** verification 过期待重验的节点在图上要有别于正常节点。

**P1 出口判据**：goal #2 的 Reason 只看相关子图（关键词 miss 时 fail-closed、不 seed 全部 sink）、复用 goal #1 已确认 fact（copy-on-create + 硬去重）、不重挖；verification 过期后节点降 stale；explore 可 patch 冲突基座并留审计；**UI 能查看基座/routing_map 及其 revised_by 审计链，能按 goal 聚焦子图**。

---

## P2 —— 真实验证：verify 相 + capability + 双容器 + harness + Brief 组装 + 合规围栏

**目标：对显式授权的测试环境发真实 payload，把 `poc-confirmed` 顶起来。合规围栏是硬门槛。**

> 就绪度档位：`骨架` = API/UI 占位；`强制围栏` = 调度/harness 硬路径；`e2e` = 真 HTTP 靶标自动化。

### P2.1 verify 任务类型（决策 #10）
- [x] `config.py`：`TaskType` 加 `verify`；`TasksConfig` 加 `verify: VerifyTaskConfig`（timeout>>explore、`force_harness`/`max_rounds`/`proxy_url`）。
- [x] `config.py:WorkerConfig`：新增 `capabilities: list[Literal["static_fs","live_http","browser"]]`。
- [x] `loop.py:_select_worker`：capability 子集校验 + fire 未批准不 claim + kill 硬拦新 verify。
- [x] `MOCK_ALLOWED_OUTCOMES` 加 `verify` 通道（triggered/refuted/...）。

### P2.2 Reason → verify 派发 + Brief 组装（决策 #13）
- [x] `prompts/default/reason.md`：链齐→`task_kind:verify` 粗 Intent（不手填 Brief）。
- [x] server `assemble_poc_brief`：chain 存在性 + **连通性**（batch / Intent provenance / 类型链）；routing_map→endpoint；缺 base_url 则 400（不再写 `UNKNOWN`）；oracle_draft→success_signature。

### P2.3 verify 任务运行时（决策 #11）
- [x] **强制路径**：`execute_allowed_request` 由 dispatcher 开火，模型不碰 socket；任务内 `max_rounds` 迭代只改 payload。
- [x] 回写 append-only：`poc-confirmed` / `refuted`+`constraint`；`observed_routing`→`routing_map` live-confirmed 回补。

### P2.4 双容器拓扑（决策 #8/#14）
- [x] `ContainerConfig` static/verify profile；verify 可注入 env；codebase **:ro** bind；凭证 env **仅 verify**。
- [x] verify 容器短生命周期 + kill 时 `destroy_verify_containers` 立即拆除。
- [ ] **仍弱**：无透明网络命名空间/出站 iptables；隔离依赖「开火只走 dispatcher harness」而非容器 netns 硬拦任意进程出站。

### P2.5 harness 与合规围栏（合规章节 1–4，决策 #9）
- [x] **强制围栏**：`execute_allowed_request` allowlist 空=fail-closed；越界不 open socket；开火前/后 `proxy_traffic` 记账。
- [x] fire 审批：未 approve 不 claim；kill-switch：cancel + 毁容器 + 拒新 claim。
- [x] `credentials_ref`→`secret:`/`env:` 解析注入 verify env（非明文落图）。
- [ ] **仍弱**：无独立 mitm 代理进程；`proxy_url` 可选注入 urllib ProxyHandler，不是透明审计网关。

### P2.6 测试
- [x] 本地 HTTP 靶标 e2e：`test_p2_verify_forced.py` 真 POST→`poc-confirmed` + 越界无 socket + credentials 解析。
- [x] allowlist / kill API / refuted+constraint 单测保留。

### P2.7 前端（`server/static/index.html`）
- [x] `verifies` 第二类边；PoC Brief 查看；人工闸门（approve/deny/kill/allowlist/proxy traffic）。

**P2 出口判据（契约层）**：verify 相、Brief、harness 回写、UI 闸门、allowlist/kill API 可用。  
**P2 不保证（实测层，交 P3）**：explore 容器内可见用户代码；verify 能打出真实利用 body；示例配置含 verify worker；demo 靶标端到端可演示。

---

## P3 —— 能实际跑：代码进容器 + payload 实例化 + 可演示闭环

**目标：给一份本地代码 + 一个测试站 URL，配好真实 LLM worker，系统能自动（或半自动）长出链并打到 `poc-confirmed`。**  
原则：**能跑 > 细扣合规**。P2 已有的 allowlist/fire/kill/记账保留即可；mitm、netns、透明代理一律不进 P3。

### 为何 P2 仍「跑不起来」（现状诊断，实施时对症下药）

| 阻塞 | 根因 | 落点 |
|---|---|---|
| explore 看不到代码 | `explore`/`bootstrap`/`reason` 的 `ensure_running` **不传** `codebase_host_path`；只有 verify 挂了 | P3.2 |
| 用户难填 origin | 建项 UI 仍是自由文本 Origin，结构化字段靠手写 JSON | P3.1 |
| verify 打不出利用 | 开火收归 dispatcher 后，payload ≈ Brief 描述字符串 + 玩具 mutate；**无人实例化真实 body** | P3.3 |
| 配置缺 verify 工人 | `dispatch.example.yaml` worker 只有 `bootstrap/reason/explore`，无 `verify`+`live_http` | P3.4 |
| 无标准靶标/跑法 | 仓库无「刻意脆弱 demo + 一页 runbook」 | P3.5 / P3.6 |

### P3.1 建项 UI：结构化 origin（前端）

- [x] `server/static/index.html` 新建项目表单改为分栏（可保留「高级：raw JSON」折叠）：
  - **代码库路径**（必填，host 侧绝对路径，即跑 dispatcher 那台机器能 bind 的路径）
  - **commit**（可选）
  - **测试站 base_url**（要做 verify 时必填）
  - **credentials_ref**（可选，`secret:NAME` / `env:VAR`）
  - **allowlist**（多行/标签；默认从 base_url 解析 host:port 预填）
  - **goal**（沿用）
- [x] 提交时拼成 origin JSON（与决策 #9 形状一致），不再让用户手写整坨 JSON。
- [x] 文案提示：`path` 是 **dispatcher/Docker 宿主机路径**，不是浏览器本机路径（异机部署时写清）。

### P3.2 代码挂进 static 容器（调度硬需求）

- [x] `dispatcher/tasks/explore.py` / `bootstrap.py` / `reason.py`：从 project origin 解析 `codebase.path`，调用  
  `container_manager.ensure_running(project_id, profile="static", codebase_host_path=...)`。
- [x] path 不存在或不可读时：任务失败并写清日志/可观测错误（勿静默空挂）。
- [x] `prompts/default/{bootstrap,explore,reason}.md`：写死工作目录约定  
  `Codebase is mounted read-only at {codebase_mount_path}`（默认 `/workspace/codebase`，与 `ContainerConfig.codebase_mount_path` 一致）；要求 `locations` 用相对该根的 `file:line`。
- [x] `prompting` / task 渲染：注入 `{codebase_mount_path}`（及可选 `{codebase_host_path}` 仅作说明）。
- [x] 单测：mock ContainerManager，断言 explore/bootstrap 创建容器时带上 binds。

### P3.3 verify 能构造真实利用（效果硬需求）

P2 把「谁发包」收成 dispatcher；P3 补回「谁写 payload」，且**仍不让模型直接开 socket**。

**推荐落地（窄腰，优先做）**：

- [x] explore emit 增加可选 `payload_draft`（字符串：建议的 HTTP body / 关键表单字段值；**不是**完整攻击框架）。contracts + conclude 可落 evidence 或挂在 observation 上；Brief 组装时 `payload_recipe.shape` **优先取 chain 上最新非空 `payload_draft`**，否则再回落 description 拼接。
- [x] `prompts/default/explore.md`：发现可利用 sink 时鼓励给出可打的 `payload_draft` + `oracle_draft`（成功判据字符串）。
- [x] verify：`_initial_payload` 只用 Brief 的 shape/gadget/`payload_draft`；多轮时若仍 `no_signal`，允许 **一轮受限模型实例化**（可选开关 `tasks.verify.allow_model_instantiate`）：
  - 输入：Brief + 上轮 `why_failed` +（可选）codebase 只读路径说明  
  - 输出：仅 `{"payload_body":"...","headers"?:{}}`  
  - 发包仍只走 `execute_allowed_request`（allowlist 硬拦）
- [x] 无模型时的兜底：按 sink 描述关键词的 **最小模板表**（v1 可只覆盖 demo：如 body 含 `!!python` / 固定 `CAIRN_POC_OK` 探针），保证 demo 靶标可过。

**不做（P3 明确砍掉）**：完整自主 exploit agent、浏览器自动化利用链、OOB 基础设施自建。

### P3.4 配置与 worker 能力可跑

- [x] `dispatch.example.yaml`：
  - 至少一个 worker：`task_types` 含 `verify`，`capabilities` 含 `live_http`（可与 explore 分 worker 或同 worker）
  - `tasks.verify.require_fire_approval: false` 作为 **demo 默认注释项**（生产可改 true）；或文档写清「演示时 UI 点 Approve」
  - `container.verify` 示例（network 可达测试站即可）
- [x] `dispatch_mock.yaml`：保持 mock 可演调度；可选加「强制 triggered」profile 方便 UI 演示。
- [x] README 或 `docs/specs/` 短文：**最小可跑清单**（server / dispatcher / docker image / API key / origin 字段）。

### P3.5 Demo 靶标 + 手工/半自动验收路径

- [x] 仓库内加 **刻意脆弱小应用**（如 `examples/vuln_yaml_import/`：Flask `POST /api/import` + 不安全 yaml 或「body 含标记即回 `CAIRN_POC_OK`」双模式；后者保证无 LLM 也能验收 harness）。
- [x] 提供 `origin` 样例 JSON + 期望图形态说明（source→dataflow→sink→verify→poc-confirmed）。
- [x] 验收分两档：
  1. **无 LLM**：seed facts 或 mock explore → verify harness → `poc-confirmed`（脚本/pytest 即可）
  2. **有 LLM**：真实 worker 读挂载代码 → 自动 intent → 人工/自动 approve → 真站 `poc-confirmed`

### P3.6 测试（对准「能跑」）

- [x] 单测：static ensure_running 收到 codebase bind；缺 path 时行为明确。
- [x] 单测：Brief 优先 `payload_draft`；verify 发出 body 与 draft 一致。
- [x] 集成：起 demo 靶标 + `run_verify_task`（或等价）全链路到 `poc-confirmed`（不只手拼 conclude）。
- [ ] （可选）mock e2e：从 create project 结构化 origin 到图上出现 verification 边。

### P3.7 前端（可跑体验，非合规加戏）

- [x] 建项结构化表单（见 P3.1）。
- [x] 项目页展示「代码已挂载路径 / 容器内路径」只读信息（来自 origin + 约定 mount）。
- [x] verify 面板：展示**实际发出的 payload_body**（从 proxy_traffic / harness evidence），便于判断「是不是还在 POST 描述文本」。
- [x] demo 模式：`require_fire_approval=false` 时面板提示「已自动开火」；为 true 时 pending 列表保持现有 approve/deny。

**P3 出口判据（硬）**：

1. 用户在 UI 填 **本机代码路径 + 测试站 URL + goal**，不必手写 origin JSON。  
2. explore/bootstrap 容器内 **只读可见** 该代码树，agent 能按 `file:line` 落富 fact。  
3. 链齐后 verify 发出的 body **不是** fact 描述废话，而是 draft/模板/受限模型产出的可打 payload；对 demo 靶标能到 **`poc-confirmed`**。  
4. `dispatch.example.yaml` 按文档改 key 后，真实 LLM 可调度 explore + verify。  
5. 仓库内有 demo 靶标 + 一页跑通说明；至少一条自动化测试覆盖「挂载 + 真 HTTP + poc-confirmed」。

**P3 明确不做**：mitm 网关、容器出站 iptables、凭证 KMS、多目标扫描产品化。

---

## Later（明确后置，不阻塞 P3 实测）

- **合规加深**：透明 mitm 代理、verify 容器 netns/出站硬拦、逐请求人工 diff 闸门。
- Codebase / Goal-Run **拆表**（现用"一 codebase 一 fact 存储 + goal tag"务实版顶着）。
- 子图**向量检索 / LLM rank**（结构遍历返回也爆时才上）。
- `preconditions` / `chain`-ID **自动修复**。
- `gadget` / `reachability` fact 类型（v1 先 source/sink/dataflow/constraint + verification 五类）。
- 网关/反代重写下的路由映射自动化（靠 verify 现场解析 + routing_map 回补）。
- OOB/timing oracle 基建（外部监听与 token 关联）。

---

## 契约变更清单（一页速查）

| 契约 | 变更 | 决策 | 阶段 |
|---|---|---|---|
| `Fact` | +`type/confidence/locations/code_version/evidence/verifies/intent_id/batch_id` | #6/#7 | P0 |
| conclude | 1→N facts；`intent.to`=主 fact | #7 | P0 |
| explore emit | `{observations:[...]}` 扁平富形态 | #5 | P0 |
| confidence | 派生视图（折叠）；高档位=追加 verification fact | #6 | P0(接口)/P2(写入) |
| 去重键 | `type + sorted(locations)`，并集合并；退化键 `verifies+why_failed.reason` | 设计要点6 | P0 |
| `origin` | 自由文本 → JSON（codebase/target/allowlist） | #9 | P0 |
| `relevant_subgraph` | 接口先定(返回全部) → Intent provenance 反向可达 | #4/#12 | P0→P1 |
| `base_knowledge` | 新增第二存储 + version + revised_by 审计 | 设计要点1-3 | P1 |
| `TaskType` | +`verify`；`TasksConfig.verify.timeout`>>explore | #10/#11 | P2 |
| `WorkerConfig` | +`capabilities` | #10 | P2 |
| `ContainerConfig` | 单 profile → static/verify 双 profile | #8/#14 | P2 |
| PoC Brief | server/dispatcher 组装，Reason 只吐粗意图 | #13 | P2 |
| harness_result | 新契约 → conclude 回写 | 设计契约4 | P2 |
| 建项 UI | 结构化 codebase/target/allowlist → origin JSON | #9 | **P3** |
| static 挂载 | explore/bootstrap/reason 传 `codebase_host_path` | #14 | **P3** |
| explore emit | 可选 `payload_draft` → Brief.shape 优先 | #5/#13 | **P3** |
| verify 实例化 | 可选受限模型只吐 `payload_body`；发包仍走 harness | #11/#13 | **P3** |

## 风险与验证

- **迁移风险**：facts 表加列要兼容存量项目（保留 `origin`/`goal` 行、缺省字段可空）。上线前对现有 DB 跑一次迁移演练。
- **窄腰回归**：每次改 prompt/emit 都要回归"模型有没有被要求填门控字段"（confidence 高档位、code_version、id）。
- **append-only 回归**：加一条测试断言"任何 conclude 都不 UPDATE 已存在 fact 的内容字段"。
- **能跑优先（P3）**：出口以 demo 靶标 + 挂载 + 真实 POST body 为准；合规加深不进 P3 出口。
- **路径语义**：UI 填的 codebase path 是 dispatcher 宿主机路径；Docker Desktop / 远程 dispatcher 场景需在 runbook 写清，避免「浏览器本机路径」误解。
- **前端隐形回归**：`index.html` 无构建、无类型检查，富字段"忘了显"不会报错、只会静默隐形。判据是"看图能不能一眼读出 type/confidence"，而非"接口有没有返回字段"。
