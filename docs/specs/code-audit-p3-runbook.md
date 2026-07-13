# Code Audit P3 —— 最小可跑清单

目标：本机代码路径 + 测试站 URL + goal，系统能挂载代码、探索并打到 `poc-confirmed`。

## 依赖

| 组件 | 说明 |
|---|---|
| Python 3.11+ | `cairn` 包 |
| Docker | worker 容器（`ghcr.io/oritera/cairn-worker-container:latest`） |
| Server | `python -m cairn.server` 或项目既有启动方式 |
| Dispatcher | 配置 `dispatch.example.yaml`（填 API key）或 `dispatch_mock.yaml` |
| 可选 LLM | 真实 worker 需 API key；无 LLM 可用 mock + seed facts 验收 harness |

## 1. 起 demo 靶标

```bash
python examples/vuln_yaml_import/app.py
# http://127.0.0.1:18080  POST /api/import
# body 含 CAIRN_POC_OK 或 !!python → 响应 CAIRN_POC_OK
```

## 2. 建项（UI 结构化表单，不必手写 JSON）

- **代码库路径**：dispatcher **宿主机**绝对路径，例如  
  `D:\workspace\Cairn\examples\vuln_yaml_import`  
  （不是浏览器本机路径；Docker Desktop 需保证该路径可 bind）
- **commit**：可选
- **测试站 base_url**：`http://127.0.0.1:18080`
- **allowlist**：默认从 base_url 预填 `127.0.0.1:18080`
- **goal**：`unauth RCE via yaml.load`

提交后 origin 落库为决策 #9 JSON。

## 3. Dispatcher 配置要点

见 `dispatch.example.yaml`：

- 至少一个 worker：`task_types` 含 `verify`，`capabilities` 含 `live_http`
- 演示：`tasks.verify.require_fire_approval: false`（自动开火）
- `container.codebase_mount_path: /workspace/codebase`
- `container.verify.network_mode` 需能访问测试站（本地 demo 用 `host`）

Mock 调度：`dispatch_mock.yaml`（`require_fire_approval: false`，verify 已开）。

## 4. 期望图形态

```
origin → (bootstrap) → surface facts
       → explore: source (POST /api/import) + sink (yaml.load) + dataflow
       → reason: task_kind=verify
       → verify harness POST body=payload_draft / template
       → verification fact (poc-confirmed) --verifies--> sink
```

## 5. 两档验收

### A. 无 LLM（自动化）

```bash
cd cairn
pytest tests/test_p3_runnable.py -q
```

覆盖：static `ensure_running` 带 codebase bind；Brief 优先 `payload_draft`；demo 靶标 + harness → `poc-confirmed`。

### B. 有 LLM

1. 填好 `dispatch.example.yaml` API key  
2. 起 server + dispatcher + demo 靶标  
3. UI 建项（路径 + URL + goal）  
4. 等待 explore 富 fact → reason 提 verify  
5. 若 `require_fire_approval: true`，在 Verify 面板 Approve  
6. 图上 sink 出现 `poc-confirmed` verification 边；Proxy Traffic 可见真实 `payload_body`

## 路径语义

- UI / origin 中的 `codebase.path` = **跑 dispatcher 的机器**上的路径  
- 容器内只读挂载点：`/workspace/codebase`（可配）  
- 异机部署时勿填浏览器所在机的本地盘符
