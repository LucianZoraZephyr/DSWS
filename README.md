# 电商问数智能体 · 从零重建（教学版）

> 这个目录不是旧系统的重构，而是**按契约从零搭的第二套实现**。
> 旧系统（`../shopkeeper-agent` + `../shopkeeper-agent-frontend`）冻结为 `legacy`，作为**反例库**。

路线图见 `../shopkeeper-agent/docs/REBUILD-ROADMAP.md`。

---

## 当前进度

| 阶段 | 状态 | 验收 |
|---|---|---|
| **S0 骨架** | ✅ 完成 | `make stage0` |
| S1 Schema 契约 | ⬜ 待做 | `make stage1` |
| S2 元知识构建 | ⬜ 待做 | `make stage2` |
| S3 检索 | ⬜ 待做 | `make stage3` |
| S4 生成 SQL | ⬜ 待做 | `make stage4` |
| S5 安全边界 | ⬜ 待做 | `make stage5` |
| S6 失败通道 | ⬜ 待做 | `make stage6` |
| S7 前端收口 | ⬜ 待做 | `make stage7` |
| S8 性能 | ⬜ 待做 | `make stage8` |

---

## S0 做了什么

**一句话**：不接 LLM、不接数据库、不接检索，只把「一次问数」的契约跑通。

三件产物：

1. **`contracts/steps.yaml`** —— 12 个步骤的唯一事实源。
   前端启动时从 `GET /api/steps` 拉取，**零硬编码**。
   `correct_sql` 标了 `conditional: true`，后端显式发 `skipped`，前端显示「未触发」。

2. **`contracts/sse.schema.json`** —— 只允许三种事件：
   - `progress { step, status, duration_ms }`
   - `result { columns, rows }`
   - `error { code, message, step }`
   未知事件会被 schema 拒绝（旧系统是静默忽略）。

3. **假 SSE 流** —— 严格按契约发 24 帧：11 个非条件步 ×（running + success）+ 1 个条件步 skipped + 1 个 result。

**刻意没做**：真实 SQL、LLM、检索、数据库。这些从 S1 开始一层层接。

---

## 快速开始

```bash
cd code/rebuild

# 终端 1
make dev-backend      # http://127.0.0.1:8100

# 终端 2
make dev-frontend     # http://127.0.0.1:5273
```

打开 http://127.0.0.1:5273 ，输入任意问题，看步骤条依次点亮。

### 验收

```bash
make install          # 首次
make stage0           # 契约测试 + 前端构建（不需要浏览器）
make stage0-ui        # 可选：真实 Chrome 冒烟测试（需两个 dev 服务已启动）
```

---

## 目录

```
rebuild/
├── contracts/              # 契约（前后端唯一事实源）
│   ├── steps.yaml          # 12 个步骤 + conditional 标记
│   └── sse.schema.json     # 三种 SSE 事件
├── backend/
│   ├── app/main.py         # S0 假流 + /api/steps
│   └── tests/              # 契约测试（5 条）
├── frontend/
│   ├── src/App.tsx
│   ├── src/components/StepRail.tsx   # 竖向导轨（响应式）
│   └── tests/s0_smoke.mjs            # 无头 Chrome 冒烟
└── Makefile
```

---

## 为什么 S0 不装数据库

审计（`../shopkeeper-agent/docs/audit-*.md`）把 30+ 条缺陷收敛到三个根因，第一条是：

> **契约只写在提示词、YAML 和文档里，没有一行代码强制它。**

如果先装数据库、再写检索、最后对前端，这个根因会被固化进每一层。
所以第一个可运行的系统先钉死契约，后面的阶段只能「填实现」，不能改契约。

---

## 与旧系统的端口隔离

| | 旧栈 | rebuild |
|---|---|---|
| 后端 | 8000 | **8100** |
| 前端 | 5173 | **5273** |

两套可以同时跑，方便并排对比（差分验收）。
