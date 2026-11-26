---
title: 'Story 2.4 查看完成或失败并释放租约'
type: 'feature'
created: '2026-11-26'
status: 'in-review'
baseline_commit: 'd32488098e513cc18e0931fa0414e3ac9c863f40'
route: 'oneshot'
review_loop_iteration: 0
---

<frozen-after-approval reason="human-owned intent">

## 意图

复用本地双卡模拟的启动事件和租约。执行方须匹配任务所有者、任务、领取及原卡，且确认执行已结束、无法继续用卡。正常完成或 OOM 时，在同一提交内记录终态并释放一次。证据不足或标识不匹配时保留原卡。本人列表与详情区分运行、完成和失败，可查看 OOM 原因，参数保持原值，累计启动一次。用独立连接核对提交边界，按最新观测重新领取并检验同卡租约历史。

</frozen-after-approval>

## 实现记录

依据 FR-03/05/06、NFR-01、AD-3/6/7 和 AC-03。结果上报沿用启动服务的进程内可信模拟入口。释放事件复用租约历史的 released_version。

## 本地双卡模拟证据

| 条件与操作 | 已提交状态断言 | 可复跑用例 |
| --- | --- | --- |
| 运行后分别上报正常完成、OOM 或普通失败 | 写连接 COMMIT 前，另一连接仍读到 running、无结果、租约未释放。提交后一个 SELECT 同时读到终态、结果和释放版本，全局版本只增一。 | `test_end_is_atomic_and_original_gpu_handoff` |
| 结束后原卡仍被外部占用，随后新观测确认原卡可用 | 两卡忙时拒绝领取。只恢复原卡后，下一任务取得同一 UUID，第一段释放版本小于第二段领取版本。迟到的旧报告返回原回执，租约历史逐行相同。 | 同上 |
| 本人查询，随后换为同组和异组账号 | 列表和详情具有相同版本及状态。OOM 的诊断分类为 oom，名称、命令（含 batch）和显存参数逐项等于结束前值，启动数为 1。匿名详情为 401，同组及异组非本人详情均为 404，列表不包含该任务。 | 同上 |
| 未启动、缺少启动事件、错任务／领取／卡／身份、执行未结束、仍能用卡或结果未知 | 拒绝报告。任务、版本、租约及启动事件与上报前相同，新增结果数为 0。 | `test_unverified_end_preserves_original_gpu` |
| 提交前或提交后连接关闭、核对连接不可读、并发重放、重放结果冲突 | 提交结果不明时尝试一次写事务，从新只读连接核对。匹配重放取得同一释放版本，冲突结果被拒绝。 | `test_lost_end_reply_reconciles_without_releasing_again` |
| 在写入结果、更新任务后，注入释放记录写失败 | 整个事务回滚，独立连接仍读到原运行状态、原租约和原版本。 | `test_release_write_failure_rolls_back_terminal_result` |
| 列表及详情先收到终态，随后收到更旧的运行快照 | 两页保留「已完成」或「失败 · GPU 显存不足（OOM）」，不会退回「运行中」。 | `tests/tasks_page.test.cjs` |

## 待联调

- Epic 3 接入真实的模拟取消与执行待核对裁决，核实与结果上报竞争时的提交顺序。
- Epic 4 接入重领、旧报告隔离和重启恢复，汇总 NFR-01。真实 L20 的执行身份及进程树结束证据属 AD-8 条件目标。
- 页面文案和交互待 UX 复核，Story 独立验收待进行。

## 构建盲审处理

- 低，普通失败只有分类，没有更具体的诊断。两页说明「模拟执行方未提供具体原因」，并分别测试普通失败和 OOM。
- 中，将下一任务入队提前到第一项任务运行期间；即使观测显示原卡空闲，提前领取仍被活跃租约阻断。
- 中，扩展双卡同时运行用例，结束第一项后逐字段比较第二项租约，任务状态仍为 running，两项启动事件均保持原值。
- 中，终态后直接断言提交时给定的任务名、带 batch 的命令、显存值，与结束前快照交叉核对。
- 低，新增重新打开原库的应用实例，核对 OOM、原释放版本与完整租约历史。

2026-11-26 在 Windows 本地双卡模拟复跑。先执行 `. ./scripts/env.ps1`。

- `uv run --locked pytest -q` 通过 128 项，有一条既有的 Starlette/httpx 弃用提示。
- `node --test tests/submission_page.test.cjs tests/tasks_page.test.cjs` 通过 41 项。
- `uv run --locked ruff check .`、`uv run --locked python scripts/check_env.py` 和 `git diff --check` 通过。启动检查使用 Python 3.12.15。
