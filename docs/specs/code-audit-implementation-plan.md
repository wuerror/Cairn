# Code Audit —— 实现计划

> 蓝本：[`code-audit-design.md`](./code-audit-design.md) 已拍板决策 #1–#14。
> 协议基线：[`server-protocol.md`](./server-protocol.md)。
> 本文只讲**怎么落地**，不重述设计动机；每个任务标注对应决策号与要改的真实文件。

---

## 0. 原则与范围

- **不重写引擎。** lease / heartbeat / conclude 双阶段 / OODA 循环全部复用。改动是"扩数据模型 + 窄腰 emit + 新增 verify 相 + 新增基座层"。
- **窄腰红线（决策 #5）。** 模型只吐扁平 `emit schema`；`id` / `code_version` / `intent_id` / `batch_id` / `confidence` 门控全在 server。任何"让模型填富 schema"的实现都要打回。
- **append-only 红线（决策 #6）。** 没有任何 fact 被就地改写。confidence 升级 = 追加 `type: verification` fact；有效 confidence 是 server 折叠视图。
- **分期交付。** P0 不需要测试环境即可跑通（富 fact + 窄腰 + 1→N conclude + 折叠 + origin 解析）；P1 加持久化与真实子图检索；P2 才引入"对实况环境发包"的验证路径与合规围栏。
- **前端同期适配（本版新增）。** 前端是一个**单文件静态 SPA**（`server/static/index.html`，Alpine.js + Cytoscape + Tailwind，无构建步骤，server 直接托管）。富 fact 的 `type`/`confidence`/`locations`/`verifies` 若不改前端就在 UI 上隐形，且决策 #7 的附属 fact 会变成图上孤点。每个后端阶段配一条前端泳道（`P0.8`/`P1.5`/`P2.7`）。**合规人工闸门是 P2 的独立大界面，工作量不亚于部分后端。**

### 就绪度对照（同事第三轮判断）

| 层级 | 状态 | 落在阶段 |
|---|---|---|
| 问题形状 / 与 Cairn 关系 / append-only confidence / 1→N conclude / verify 相 / origin JSON | 已定 | — |
| 富 Fact + 窄腰 emit + 折叠视图 + origin 解析 | 可开工 | **P0** |
| 跨 run 持久化 + `relevant_subgraph` 真实实现 + 防腐 | 结构边已定(#12) | **P1** |
| 真实验证：verify 相 + capability + 双容器 + harness + Brief 组装 + 合规闸门 | 三缝已补(#12/#13/#14) | **P2** |
| Codebase / Goal-Run 拆表、向量检索、`preconditions`/`chain` 自动修复、`gadget`/`reachability` 类型 | 可后置 | **Later** |
| 前端适配（富 fact 可视化 + 图外第二存储面板 + 合规人工闸门） | 原计划遗漏，本版补 | **P0补齐 / P1 / P2** |

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
- [ ] `server/services.py`：实现 `relevant_subgraph(project, goal)`：
  1. goal → 终态 sink 类型：**v1 小映射表**（`RCE→{exec,deserialize,ssti,template}` 等）+ sink `description` 关键词兜底；
  2. 从匹配 sink 沿 **Intent provenance（`from[]→to`，即 `intent_sources`+`to_fact_id`）反向 BFS N 跳**；`type` 只作节点标签；
  3. 结果按 **有效 confidence 过滤掉已 `refuted` 路径**，再按 `batch_id` 附上同批附属 fact（`source`/`constraint`）。
- [ ] server 暴露 `relevant_subgraph` 接口；`reason` 任务取数从"全量 export"切到该接口。**接口 P0 就可先存在并返回全部**（决策 #4），P1 换实现、不动 prompt。
- [ ] `dispatcher/tasks/reason.py` & `prompts/default/reason.md`：`{graph_yaml}` 来源改为子图切片。

### P1.2 持久化 + 防腐（决策 #3，设计要点 5，已知限制 5）
- [ ] 存储：fact 以 `file:line + code_version` 为持久键，跨 run 累积；goal 用 tag/filter 表达（**v1 不拆表**）。
- [ ] 折叠视图消费 `code_version` 失配 → 节点降 `stale`/待重验（视图计算，存储不动）。
- [ ] 跨 run 去重升级为硬需求：写入前按 canonical key 查已存在 fact。

### P1.3 基座知识层（设计要点 1/2/3，第二存储约束）
- [ ] 新增 `base_knowledge` 存储（**协议外第二存储，非图节点**）：`version` + `entries[]`(kind/statement/evidence/confidence/revised_by) + `routing_map`。
- [ ] `bootstrap` 任务产出基座（`prompts/default/bootstrap*.md` 扩语义）；worker 只读、带 `version`。
- [ ] **patch 规则**：`revised_by` 必须由冲突 fact 触发并留审计（谁/因哪条 fact/何时）；worker 执行中 `version` 变了要在 conclude 前重拉。
- [ ] `kind: auth` 条目支持"早期接地"占位（P2 才真接地）。

### P1.4 测试
- [ ] 反向可达遍历正确（含 constraint、剔除 refuted）；跨 run 复用不重建；基座 patch 审计链完整。

### P1.5 前端（`server/static/index.html`）
- [ ] **基座知识 / routing_map 面板（图外第二存储，UI 今天完全没有）。** 新 tab/面板显 `base_knowledge.entries`（kind/statement/evidence/confidence/revised_by）+ `routing_map`，并显 `version` 与 `revised_by` 审计链（谁/因哪条 fact/何时）。
- [ ] **按 goal 过滤/高亮子图。** Reason 现在吃 `relevant_subgraph`；跨 run 大图会糊，UI 加"聚焦当前 goal 相关子图"开关（前端过滤或调用 `relevant_subgraph` 接口）。
- [ ] **`stale` 节点显式标记。** 代码变动后待重验的节点在图上要有别于正常节点。

**P1 出口判据**：goal #2 的 Reason 只看相关子图、复用 goal #1 已确认 fact、不重挖；代码变动后旧 fact 自动降 stale；**UI 能查看基座/routing_map 及其 revised_by 审计链，能按 goal 聚焦子图**。

---

## P2 —— 真实验证：verify 相 + capability + 双容器 + harness + Brief 组装 + 合规围栏

**目标：对显式授权的测试环境发真实 payload，把 `poc-confirmed` 顶起来。合规围栏是硬门槛。**

### P2.1 verify 任务类型（决策 #10）
- [ ] `config.py`：`TaskType` 加 `verify`；`TasksConfig` 加 `verify: VerifyTaskConfig(timeout, ...)`，`tasks.verify.timeout` **明显长于 explore**（决策 #11）。
- [ ] `config.py:WorkerConfig`：新增 `capabilities: list[Literal["static_fs","live_http","browser"]]`。
- [ ] `loop.py:_select_worker` / `worker_select.py`：路由时校验 `verify` 任务所需 capability ⊆ worker.capabilities（现只看 task_types + priority）。
- [ ] `MOCK_ALLOWED_OUTCOMES` 加 `verify` 通道（triggered/refuted/...）。

### P2.2 Reason → verify 派发 + Brief 组装（决策 #13）
- [ ] `prompts/default/reason.md`：新增"链已齐→派 verify"分支，只吐 `{from: chain_fact_ids, description}` 粗 Intent（**不手填 Brief**）。
- [ ] `server`/`dispatcher`：校验 chain 存在性/连通性后，用**已落 fact + 模板**组装 `poc_brief`（`entry.endpoint`←`routing_map`；`dataflow`←chain 各 fact `locations`；`oracle`←explore 的 `oracle_draft`，缺则模板）。Brief 作为 verify Intent 的结构化 payload。

### P2.3 verify 任务运行时（决策 #11）
- [ ] 新增 `dispatcher/tasks/verify.py`：读 chain 代码 + Brief → 写 Python 脚本跑 harness → **任务内多轮迭代**（试一发→看 `why_failed`→调 payload），中间过程进 `evidence`/日志**不进 fact**；**末轮**结构化 conclude。
- [ ] 回写（append-only）：成功→`poc-confirmed` verification fact（`verifies`→末端 sink）+ PoC evidence；失败→`refuted` verification fact + 带 `why_failed` 的 `constraint` fact（可空 `locations`）；观察到的真实路由→回补 `routing_map`。

### P2.4 双容器拓扑（决策 #8/#14）
- [ ] `config.py:ContainerConfig`：从单 profile 扩为**按 task 能力选 profile**——`static`（只读 FS、无网络）与 `verify`（网络 + 凭证 + 代理 + 可选浏览器）。
- [ ] `runtime/containers.py:ContainerManager`：支持一 project **并存** static + verify 容器；codebase volume **只读**挂两边；测试环境凭证/出站网络**只注入 verify profile**；verify 容器**每个 verify 任务结束即 remove**（static 沿用 `completed_action`）。
- [ ] `loop.py`：dispatch verify 时选 verify profile 容器；生命周期与清理接入现有 `_queue_container_cleanups`。

### P2.5 harness 与合规围栏（合规章节 1–4，决策 #9）
- [ ] verify harness：固化基建（强制走代理、伪造 header、session/auth 装配、base URL），只暴露"payload 逻辑"槽；消费 `success_signature` 返回结构化 `harness_result`。
- [ ] **allowlist 硬约束**：出站目标 + 带外回连地址只允许 `origin.allowlist`；超范围由 harness 层直接拒绝（不靠模型自觉）。
- [ ] **代理 = 强制审计轨 + 人工闸门**：所有攻击流量过代理留档；v1 人工在代理面板点确认（复用 `Hint`）才发包（体验/安全权衡可配置）。
- [ ] **凭证 + kill-switch**：`credentials_ref` 走 secret 存储、只进 verify profile；提供"立即中止所有在跑 verify run"的开关（接 `runtime/cancellation.py`）。
- [ ] **围栏边界即职责边界**：强模型只产出 Brief，permissive 模型只实例化+开火、不越 Brief 自主决策。

### P2.6 测试
- [ ] 起一个**本地一次性靶标**（如刻意可 RCE 的 Flask demo）跑 Worked Example 那条链，端到端到 `poc-confirmed`；
- [ ] allowlist 拒绝越界目标；kill-switch 能中止；失败路径产出 refuted+constraint 并反向制导。

### P2.7 前端（`server/static/index.html`）—— 合规面板是重头
- [ ] **`verifies` 第二类边渲染。** `buildElements` 今天只有 Intent 边；verification fact → 被验证节点要画成**区别于 Intent 边的第二类边**（不同颜色/线型），并把 verification 折叠展示在被验证节点上（呼应决策 #6/#12 的"两类边"）。
- [ ] **PoC Brief 查看器。** verify Intent 上挂的结构化 Brief（chain / entry / dataflow / payload_recipe / success_signature）可查看。
- [ ] **人工闸门面板（合规硬需求，新的大界面）。** 设计文档 §合规 point 2 的"开火前人工确认"落在这里：审攻击流量（代理留档的 request/response，基线 vs payload diff）、approve/deny 开火（复用 `Hint` 写确认）、**kill-switch 按钮**（接 `runtime/cancellation.py`）、`origin.allowlist` 展示。
- [ ] **harness_result 展示。** `triggered`/`evidence`/`why_failed`/`request`/`response` 结构化呈现，成功/失败一眼可辨。

**P2 出口判据**：授权测试环境上，一条候选链能被验证 worker 实证到 `poc-confirmed`，全程流量留档、越界被拒、可一键中止；失败被结构化回写并反哺审计；**UI 能查看 Brief、verification 边与 harness 结果，且开火前的人工确认与 kill-switch 都在面板上可操作**。

---

## Later（明确后置，不阻塞上面）

- Codebase / Goal-Run **拆表**（现用"一 codebase 一 fact 存储 + goal tag"务实版顶着）。
- 子图**向量检索 / LLM rank**（结构遍历返回也爆时才上）。
- `preconditions` / `chain`-ID **自动修复**。
- `gadget` / `reachability` fact 类型（v1 先 source/sink/dataflow/constraint + verification 五类）。
- 网关/反代重写下的路由映射自动化（靠 verify worker 现场解析兜底）。

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

## 风险与验证

- **迁移风险**：facts 表加列要兼容存量项目（保留 `origin`/`goal` 行、缺省字段可空）。上线前对现有 DB 跑一次迁移演练。
- **窄腰回归**：每次改 prompt/emit 都要回归"模型有没有被要求填门控字段"（confidence 高档位、code_version、id）。
- **append-only 回归**：加一条测试断言"任何 conclude 都不 UPDATE 已存在 fact 的内容字段"。
- **合规**：P2 上线前，allowlist 拒绝、kill-switch、代理留档三项必须有自动化测试，不能只靠人工。
- **前端隐形回归**：`index.html` 无构建、无类型检查，富字段"忘了显"不会报错、只会静默隐形（P0 已出现：附属 fact 变孤点、type 不配色）。判据是"看图能不能一眼读出 type/confidence"，而非"接口有没有返回字段"。合规闸门面板还需人工走查 approve/deny/kill-switch 的实际生效。
