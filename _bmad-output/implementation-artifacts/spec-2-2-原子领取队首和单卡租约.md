---
title: 'Story 2.2 原子领取队首和单卡租约'
type: 'feature'
created: '2026-11-25'
status: 'in-review'
baseline_commit: 'e1421b92c614b3502784bb346cc7fa25c4232d78'
route: 'dispatch'
review_loop_iteration: 0
context: ['{project-root}/AGENTS.md', '{project-root}/_bmad-output/project-context.md', '{project-root}/_bmad-output/implementation-artifacts/epic-2-context.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## 意图

按全局 FIFO 给队首发放单卡模拟租约。任务已获准、领取 ID、稳定 GPU UUID、准入代际、租约历史和页面版本须在同一 SQLite 写事务提交。依据 FR-01/02/04/05、NFR-01、AD-1/2/3/5/6、AC-02/06。

## 范围与约束

仅限本地双卡。应用进程内可信调用发起领取，无 HTTP 写入口、后台轮询、实际执行、启动确认或回队事务。保留旧库与入队回执语义。启动确认由 2.3 处理，实际回队由 4.2 处理。

| 给定 | 当 | 则 |
| --- | --- | --- |
| 跨组 A、B 按确认顺序入队，A 显存未知，仅一张卡可用 | 两个独立领取者争同一张卡并重试 | 仅 A 获准一次，领取 ID 与租约同提交，B 仍排队且队位 1。历史同卡区间重叠为 0，同任务同时最多一份有效租约。 |
| 已提交的更早领取符合原序回队条件，后到任务排队且另有空闲卡 | 后到任务进入真实领取写事务 | 拒发租约并返回需先原序回队的原因。执行证据未知仍保留原卡。 |
| 仅一个排队任务，原快照只有 GPU 1 可用 | 分别注入查询失败、漏报、过期和外部占用不明，再恢复到仅 GPU 2 可用 | 各异常新增领取均为 0。恢复后恰好一次，租约绑定 GPU 2 的稳定 UUID。 |

</frozen-after-approval>

## 代码定位

- `submissions.py` 的 `initialize` 已启用 WAL/FULL，`_connect_readonly` 适用于不确定提交核对，`_task_snapshot` 共读页面版本、原因和队位。
- `observations.py` 管理事务内准入、代际、截止点和观察发布。保持有效但候选换卡时，只在可见等待原因也变化时推进页面版本。
- `app.py` 的 lifespan 暴露可信模拟观测入口。`static/tasks.js` 校验原因并渲染状态。

## 任务与验收

- [x] `src/labqueue/leases.py`、`submissions.py` 增量迁移租约历史，使用解除版本为空表示保留，分别约束当前 GPU 和任务唯一。取得写权后重查，按版本记录区间，成功后才应答。忙锁重开事务。提交不明时关闭原连接并从只读连接核对精确领取 ID，禁止盲目重领。
- [x] `src/labqueue/observations.py` 在有效发布中持久撤销外部占用卡上未确认领取的启动资格。回队检查由当前租约关联的任务、原序号、失效或待核对状态和明确未启动证据共同决定。保留原租约。
- [x] `src/labqueue/app.py`、`submissions.py`、`static/tasks.js` 提供可信领取入口、本人已获准状态和一致的等待原因。回队优先原因不携带他人标识。
- [x] `tests/test_dispatch.py`、`tests/tasks_page.test.cjs` 验证矩阵、旧库、原子回滚、提交回执丢失、历史约束与查询呈现。Story 1.2 补测两卡忙转单卡可用时的 FIFO。

## 实现记录

- 领取由进程内可信入口 `app.state.claim_head()` 发起。两张卡都可用时采用配置顺序。读到的卡还须没有当前平台租约。本人列表与详情同版本显示状态和等待原因。
- 租约历史以页面版本记录领取和解除边界。空解除版本表示继续保留，两项 SQLite 部分唯一索引分别约束已保留的 GPU UUID 和任务 ID。
- 回队裁决从同一领取写事务读取早到任务、当前租约与执行证据。较早任务已核实应回队时返回 `requeue_required`，本人查询显示「有更早的任务需先按原序回队」，原租约继续保留。

## 规格变更记录

本轮按原验收矩阵实施。

## 实施验证

### 2026-11-25 · Windows 本地双卡模拟

| 场景 | 已取得的证据 |
| --- | --- |
| 1 · 显存未知与并发领取 | 两卡忙时领取 0 次。仅 GPU 1 恢复可用后，首个领取者持写锁停在提交前，第二个领取者实际遇到 `SQLITE_BUSY`。独立读连接此前看到 A、B 均排队、租约 0、版本未变。提交后，A 仅获准一次，领取 ID 和租约对应同一个新版本。B 仍排队、队位由 2 变为 1；两领取者重试均无新增领取。 |
| 1 · 租约历史 | 测试用提交夹具模拟解除，再领取同一任务和同一 GPU。按提交版本查询全部区间，同卡与同任务重叠均为 0。直接插入同卡或同任务的第二份当前租约，分别被相应的 SQLite 唯一约束拒绝。 |
| 2 · 队首写事务中的回队优先 | 单卡被外部占用、准入代际失效和已核实的待核对三种情形均通过。另一张卡可用。SQL 轨迹显示 `BEGIN IMMEDIATE` 后重读队首与执行证据，B 新增租约 0，原租约保留，B 队位 1，原因是应先原序回队。执行证据未知、已启动或已确认的对照用例保留原卡，允许 B 领取另一张可用卡。 |
| 3 · Story 2.1 联调 | 查询失败、设备漏报、到截止点过期、外部占用不明，各新增领取 0 次。每次新快照仅 GPU 2 可用且只有一个排队任务时，持久租约恰好新增 1 条，UUID 为 `GPU-6c61338a-4bc7-4a97-a6a4-293caa9f8602`。重试队列为空，历史仍只有该条。 |

定向用例在 [test_dispatch.py](../../tests/test_dispatch.py)。它还覆盖旧库迁移、重启保留租约、提交失败原子回滚、忙锁重读最新观测、提交回执丢失后按精确领取 ID 读回。列表和详情的旧快照回归见 [tasks_page.test.cjs](../../tests/tasks_page.test.cjs)。

- `uv run --locked pytest -q` 共 87 项通过，其中 Story 2.2 定向用例 20 项。
- `node --test tests/submission_page.test.cjs tests/tasks_page.test.cjs` 共 33 项通过。
- `uv run --locked ruff check .`、`uv run --locked python scripts/check_env.py` 和 Git 差异空白检查通过。启动检查使用 Python 3.12.15。Starlette 仍报告已有的 TestClient／httpx 弃用提示。

## 待联调

- Story 2.3 复用当前领取和租约，验证领取与启动重复请求合计至多产生一次模拟启动。4.1 验证到期与启动的提交顺序。
- Story 4.2 接入实际原序回队、旧领取封闭与原租约解除，随后验证按新观测重领。4.3 验证旧领取重放无法改写新租约。
- Story 2.4 接入模拟终态的实际释放，并复测同卡交接及历史重叠。本轮历史区间的释放使用测试夹具。
