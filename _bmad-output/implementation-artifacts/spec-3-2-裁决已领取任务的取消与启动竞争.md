---
title: 'Story 3.2 裁决已领取任务的取消与启动竞争'
type: feature
created: '2026-11-26'
status: done
route: dispatch
baseline_commit: 'bc165b9fc42576fbfa067ee51ca9c95fe93b0af7'
story_key: '3-2-裁决已领取任务的取消与启动竞争'
review_loop_iteration: 0
---

<frozen-after-approval reason="人工确认后锁定意图">

## 意图

本人可撤回已获准但尚未确认启动的任务；取消与启动按裁决提交顺序决定结果。回执丢失后可核实原请求终局，列表与详情不会被旧响应恢复成可取消状态。

## 边界与约束

依据课程要求、项目上下文、epics.md Story 3.2 全部验收条件、AD-1/2/3/4/7、D-010/011。仅本地双卡、合成账号、127.0.0.1，不执行任务命令。保持 SQLite WAL/FULL、先取写权再读证据、所有权和 CSRF 校验、全局版本及 FIFO。

受理不抢占裁决。取消成功须同事务提交任务取消、旧领取封闭、原租约释放、原 ID 终局及页面版本。已确认启动则持久记录已过截止点，取消不释放租约。读取失败或证据不完整不能视为未启动；本轮验证禁止误取消和释放，完整待核对保留、恢复和组合故障验收留 Story 3.3。

确认规格后实施、验证并提交当前 codex/story-3-2 分支，不推送。Sprint 最终为 review，不宣称人工验收完成；保留待 UX 复核及跨 Story 联调记录。

## 验收矩阵

各行均为“给定／当／则”；以真实模拟入口和已提交数据库取证。

| 编号 | 给定 | 当 | 则 |
| --- | --- | --- | --- |
| A1 | A 已领取，暂停启动确认，另一卡不可用，B 排队 | 本人取消先提交，再重放、重试领取及旧领取启动 | 独立连接只见提交前或提交后完整状态；取消、封闭、释放及终局同版本，释放恰好 1 次，A 启动 0，B 可领取原卡 |
| A2 | A 的取消已提交 | 丢弃回执，先 GET 原 ID，再重放 | 读到原取消终局，GET 零写入；入口关闭，累计释放 1、启动 0 |
| A3 | 取消受理已提交、终局尚未提交 | 启动确认先提交，再裁决取消；丢截止点回执后 GET、重放启动和取消 | 原 ID 为已过截止点，不是处理中；取消释放 0，原租约保留，累计启动 1，入口关闭；后续正常完成或失败由执行结果流转 |
| A4 | 已领取 A 的旧请求拦在受理前并超时 | 普通刷新、切页、重开 | 原 ID 持续未知，记录和版本不变，不自动发取消、不恢复入口 |
| A5 | A4 且 A 仍可取消 | 本人主动封存，先收局部回执再收合格完整快照 | 只有完整快照同时证明确切 ID 已封存且 A 可取消才恢复入口；任务和租约不变 |
| A6 | 封存已提交，尚未点新取消 | 放行旧请求并重放；另在重启后重放 | 请求、任务、租约及历史、取消事件和版本均零变化；本人主动新 ID 才取消、释放各一次 |
| A7 | 受理先提交而无终局，或任务已过截止点 | 主动封存／核对；中断后由服务恢复原 ID | 已受理不封存，处理中入口关闭；过截止点即使 ID 已终结也不恢复；恢复按当前证据完成原 ID，不新增请求 |
| A8 | 封存成功回执丢失 | 本人手动重复封存 | 返回原封存结果，无新增写入 |
| A9 | A 已领取，旧 ID 确未受理且未封存 | 同组、异组非本人取消或封存；匿名及无效 CSRF | 已认证非本人反馈与未知资源一致；匿名 401、无效 CSRF 403；私有标记不泄露，所有持久事实及版本不变；有本人正对照 |
| A10 | 列表、详情扣住可取消旧快照 | 同一次取消提交的新完整快照先到，旧响应后到，含跨页交叉 | 两页分别验证 A 仍已取消、无旧队位，等待原因和入口不回退 |
| A11 | 两页已有完整画面且列表有其他任务 | 局部回执在完整快照之前／之后到达；旧 ID 终局与新 ID 处理中交错 | 局部回执不替换页面、不清行、不拼快照、不启用入口；提高共享下限则旧缓存仅标上次确认，取得合格完整快照才更新；旧 ID 不解除新 ID 待核对 |
| A12 | A 已领取 | 裁决读取启动事实失败或得到不完整证据 | 不推断尚未启动，不确认取消、不封闭并释放原卡；完整待核对处置及恢复验收归 3.3 |

</frozen-after-approval>

## 代码定位

- `src/labqueue/cancellations.py`：复用 `_advance_once` 的写事务、所有权、提交不明只读核对，`cancel` 的终局快读及 `recover_cancellations`；扩展 `_transition`、`CANCEL_ELIGIBLE_SQL`。
- `src/labqueue/leases.py`：当前租约部分唯一索引；`startup_revoked`、`released_version` 已可表达封闭与释放，复用历史，不另建释放账本。
- `src/labqueue/starts.py`：`decide_start`、`read_start` 提供匹配任务／领取／UUID 的启动事实；保持按任务唯一事件和旧领取拦截。
- `src/labqueue/submissions.py`：`initialize` 与 `_task_snapshot` 接入持久结果及同快照资格；`src/labqueue/app.py` 复用本人命令、CSRF、服务恢复入口。
- `src/labqueue/static/tasks.js`：扩展 `isTaskView`、结果呈现、确认文案；保留 `withVersionFloor`、共享 pending ID、只读刷新。列表保持详情导航，取消命令沿用详情入口。

## 实现与验收任务

- [x] `src/labqueue/cancellations.py`：扩展已领取裁决，事务内验证当前领取与启动证据；条件释放检查行数，任一步失败全部回滚；终结／封存重放不写。
- [x] `src/labqueue/cancellations.py`、`src/labqueue/submissions.py`：新增可空持久 `reason`，以 `ineligible + past_cutoff` 表达截止点，兼容旧终局；增量迁移不删除旧记录。同步事实模型、回执、单请求读取与页面批量投影，资格覆盖证实未启动的 admitted；截止点须核实匹配任务／领取／GPU／所有者的启动事件及确认事实，不能仅凭 running 状态推断。
- [x] `src/labqueue/static/tasks.js`：接受已获准可取消画面，显示明确截止点反馈；检查结果原因和完整字段，保持原 ID 与版本门槛。
- [x] `tests/test_cancellations.py`、`tests/test_dispatch.py`：覆盖 A1–A9，使用事务暂停点控制两个真实连接提交顺序；比较原卡全部历史，注入提交前失败、提交后回执丢失和证据读取失败；覆盖旧库升级与服务恢复。调整 Story 3.1 中“领取后必定 ineligible”的旧范围预期。
- [x] `tests/tasks_page.test.cjs`：列表／详情各跑 A4–A5、A10–A11，包含其他任务行、同会话重开、无版本／字段缺失、ID 错配及终局不降级。
- [x] 本规格记录逐项命名用例和实际结果；`decision-log.md` 记录真实建议及用户确认；`_bmad-output/implementation-artifacts/sprint-status.yaml` 最终置 review，记录 3.3、4.2、4.6 及 UX 待联调。

## 设计说明

没有待用户裁决的需求缺口。变更跨数据库、服务和页面，采用完整规格；数据库只需可重复的增量加列，旧请求终局不重新解释。实现测试使用临时本地库，不修改现有用户数据或触发外部副作用。取消事件沿用原请求的 cancelled 终局，释放证据沿用原领取的 released_version；检查事务写入次数和重放前后整行历史，避免仅凭最终计数掩盖重复写入。

## 实现记录

### 87d96a4 独立复审退回后的修复

**恢复隔离。** 裁决先在持有 SQLite 写锁的事务内建立保存点。失败时回滚裁决的任务、租约及终局写入，按所有者／任务／原请求 ID 持久保存 `cancellation_recovery_blocks`，并在确证记录存在后提交。只有已提交隔离的请求才允许恢复循环跳过并继续下一个请求。故障任务及原租约不封闭、不释放；新增启动检查持久阻断，已确认启动仍只读返回原事件；队首有阻断时不发新租约。页面资格与安全裁决证据分开，避免故障恢复后被自身阻断误判为不可取消。

**重试与操作边界。** 启动恢复若留下隔离请求，会自动唤起后台恢复；每轮尚有失败时等待 0.5 秒再重试原 ID。证据恢复后，原 ID 终局与清除其阻断同事务提交，不依赖客户端重发。重复失败不重复插入或推进版本。若连阻断写入／提交都失败或提交结果不明，不报告隔离成功；当前服务实例关闭新增启动及派发，必须重启服务并在初始化推进代际、重新恢复核实后继续。底层数据库整体不可用不承诺服务启动成功。完整未知执行事实状态机、持久执行待核对终局、心跳超时及组合故障仍留 3.3；本轮阻断是未完成取消裁决的安全重试机制，不宣称替代该状态机。

**回执版本。** 旧 ID 回执先校验关联和版本。只有画面版本确实低于共享下限才标记“上次确认”。有效 v10 完整快照收到旧 ID v4 时保留画面、反馈和取消按钮，不增加 GET；合法旧 ID v12 仍提高下限、使低版本缓存失效，保留当前新 ID。

| 用户问题／边界 | 仓库回归用例 | 验证断言 |
| --- | --- | --- |
| A 故障不阻断服务与 B | `test_recovery_isolates_failed_request_and_retries_without_unsafe_start[False/True]` | 运行中／重启服务可用、本人列表详情可查，B 原 ID 已取消；A 原租约整行不变，未封闭未释放，旧启动及原卡领取被拒；块持久后再次重启仍安全，恢复证据后自动裁决 A |
| 排队请求故障及重试 | `test_queued_recovery_block_repeated_failure_and_success` | 双卡空闲仍不领取故障队首；重复故障全历史与版本不变；恢复后原 ID 取消，块消失 |
| 阻断写失败保守关闭 | `test_uncommittable_recovery_block_fails_closed[ABORT/IGNORE]` | 插入报错或静默拒绝均不视为成功隔离，原租约不变，服务实例启动／派发入口关闭 |
| 阻断提交结果未知 | `test_recovery_block_commit_failure_stays_closed_until_restart[False/True]` | 提交前失败／提交后丢回执，块分别不存在／存在；均不误取消、释放或新增启动；重启重新隔离并自动恢复原 ID |
| 已确认启动重放 | `test_blocked_recovery_allows_only_confirmed_start_replay` | 已确认事件只读重放，记录数与租约不变；取消恢复为原 ID 的截止点结果 |
| 低版及较新旧 ID 回执 | JS「列表／详情 新 ID sealed v10 后旧 ID v4／v12 仅按版本失效缓存」四项 | v4 保持有效画面、按钮、反馈及 GET 数；v12 提高下限但不清新 ID，旧画面只标上次确认 |

修复后的最终全量结果：`uv run --locked pytest -q` **177 passed**，122.77 秒，保留 1 条既有 Starlette/httpx 弃用提示；页面全量 **59 passed**、0 跳过；`uv run --locked ruff check .`、`uv run --locked python scripts/check_env.py`、`git diff --check` 均通过。仅使用本地双卡模拟和临时数据库。代码与仓库回归完成不等于独立复审通过；Sprint 保持 review，UX 复核继续待办。原先将服务启动中断留到 3.3 的结论被用户独立复审否决，已修订 `deferred-work.md`。

2026-11-26 用户批准完整规格并继续，保留单 Story。补充实施约束：取消受理不决定胜负；数据库升级、服务恢复和重放不得重新裁决旧终局或改变封存含义；启动事实读取失败或证据不完整不得取消、封闭领取或释放。调整 Story 3.1 测试仍须保留原终局不可变、封存有效、重放零写入保证。关键故障及并发验证必须进入仓库测试，并逐项关联实际结果。本轮提交但不推送，Sprint 停在 review。

## 规格修订

### 独立复审退回修复 · 2026-11-26

用户明确要求基于 `87d96a4c6cdd80d38995fff245573778018c60f6` 直接修复并提交，不再等待规格确认。本次只处理以下两项，原 Story 意图不变，Sprint 保持 review，不宣称独立复审通过。

- `src/labqueue/cancellations.py`、`src/labqueue/app.py` 及必要的启动/派发入口：恢复时隔离单个请求证据异常。A 已领取、B 排队且取消均已受理，仅 A 读启动事实失败，重启服务仍可启动、本人可查询、B 原 ID 恢复。A 不得误取消、封闭领取或释放原租约，并继续阻止不安全启动及原卡派发；不能仅吞异常。明确故障恢复后的服务端重试机制，旧终局不可重新裁决。
- `src/labqueue/static/tasks.js`：新 ID 版本 10 的完整封存快照恢复入口后，旧 ID 版本 4 回执不得改变画面、按钮或引发无谓 GET；合法较新旧 ID 回执仍提高共享下限，标记低版本缓存，但不清除新 ID。列表和详情分别覆盖低版本及较新回执。
- `tests/test_cancellations.py`、`tests/tasks_page.test.cjs` 纳入复现及故障恢复回归；更新本卡逐项证据及 `deferred-work.md`，完整未知事实状态机仍留 Story 3.3。复跑全量 Python、页面、Ruff、启动、差异检查，再本地提交当前分支，不推送。

## 审查记录

### 本轮修复构建检查（不是独立复审结论）

| 发现 | 判定及处理 |
| --- | --- |
| 排队请求持久阻断后仍可领取 | medium；在同快照派发判断阻断队首并补空闲双卡负向测试。 |
| 存储失败保护锁存至重启 | low；这是保守故障策略，不自动清除，见本轮重试及操作边界说明。 |
| 后台缺少运维诊断 | low；健康接口仅表示 HTTP 可用，不宣称执行健康。通用限流诊断需额外机制，本轮不增加；完整故障可观测性随 3.3 设计。 |
| 阻断提交回执丢失缺覆盖 | medium；补提交前失败、提交后回执丢失及重启验证。 |
| 持久阻断后第二次重启缺覆盖 | medium；补保持证据不可读的再次重启与证据恢复后自动重试。 |
| 页面夹具手动投递存储事件 | false；当前测试有意确定响应顺序和浏览器事件交付，覆盖实际处理函数及两页状态。浏览器原生事件实现未被此变更修改，UX 实机复核仍保留。 |
| 根代理：静默忽略首次阻断插入 | medium；INSERT OR IGNORE 返回 0 不等于已有记录，提交前读回确切键，缺失进入失败关闭路径。 |

边界及验证缺口层未提出额外发现；上述构建检查仅用于本轮修复，不代表用户要求的独立复审已经通过。

2026-11-26 三个独立层完成构建审查，以下逐项裁决；真实验收仍待人工 review。

| 来源与发现 | 判定 | 依据及处理 |
| --- | --- | --- |
| 盲审：旧 queued 测试改为 admitted | medium | 原封存、权限及提交丢失回归覆盖被替换；参数化保留两种状态。patch |
| 盲审：启动先提交未验证实际争锁 | medium | 原分支在启动完成后才放行取消；增加提交前暂停与取消写锁入口同步。patch |
| 盲审：不同取消 ID 并发缺证据 | medium | 原用例只重放同 ID；补双请求及原卡释放一次断言。patch |
| 盲审：版本写入失败未覆盖 | low | 新 rowcount 分支缺故障注入；补仅裁决阶段版本写入失败回滚。patch |
| 盲审：旧库 cancelled/processing 缺样本 | medium | 原升级只保留 ineligible/sealed；补取消终局保持及未终结原 ID 恢复。patch |
| 盲审：reason 与 state 缺中心约束 | false | 当前唯一写入 past_cutoff 的路径同时返回 ineligible，旧库增列为 NULL；未发现可由现有入口生成非法组合的路径。前端仍校验输入，不为假设的数据库篡改扩展模型。 |
| 盲审：结束后新 ID 取消分支未覆盖 | low | 仅重放结束前的 ID；补 completed/failed 后新 ID 的持久截止点。patch |
| 盲审：A12 未验证后续启动和派发 | medium | 本轮仅证实错误读取不取消、不封闭、不释放；完整未知事实保留原卡及启动/派发组合验收被用户明确留给 3.3。记录待联调。 |
| 边界审查：未知事实中断启动恢复 | medium | recover_cancellations 对证据读取异常直接传播，可中断服务启动及后续请求恢复；用户明确将完整待核对及恢复验收留给 3.3，记录该具体限制。 |
| 验证审查：API admitted 资格无正向断言 | medium | 浏览器夹具自行提供 true，直接取消未使用页面资格分支；补真实列表/详情领取前后、封存后及终局资格。patch |
| 验证审查：非法 reason 的同版本测试无效 | medium | 同版本渲染复用掩盖缺失校验；改用较新版本并检查下限和错误反馈，补非法命令回执。patch |
| 根代理：已领取的新旧 ID 交错未两页核验 | medium | 原跨页单例使用 queued 且末尾只断言详情；参数化 queued/admitted，并分别读取两页的新 ID processing 快照。patch |
| 根代理：旧 ID 合法回执未提高版本下限 | medium | 回执处理先比较当前 pending ID，导致合法旧 ID 回执版本 80 被忽略，已有测试期待下限仍为 4；按 D-011 先接纳关联已发送请求的合法版本，再隔离请求结果，禁止旧 ID 清除新 ID。patch |

## 验证

在同一 PowerShell 中先 `. ./scripts/env.ps1`，执行 `uv run --locked pytest -q`、`node --test tests/submission_page.test.cjs tests/tasks_page.test.cjs`、`uv run --locked ruff check .`、`uv run --locked python scripts/check_env.py`、`git diff --check`。实际命令结果记录如下。保留全量回归与本 Story 逐项证据的区分，不以单 Story 通过宣称综合 NFR 达标。


### Story 3.2 实施证据 · 2026-11-26

已实现可重复的 `reason` 增量迁移；历史无原因的终局保持原意。取消事务在读取完整启动事实后，核验原领取并条件封闭、释放；任务、终局与全局版本一起提交，任一条件写入失败全部回滚。启动确认获胜时持久记录 `ineligible + past_cutoff`，原租约由后续执行结果释放。页面接受 admitted 取消资格并校验原因，只读完整快照保留原有版本门槛和原请求 ID。

| 验收 | 已运行仓库用例 | 实际证据 |
| --- | --- | --- |
| A1 | `test_admitted_cancel_start_commit_order_and_lost_receipt[cancel]` | 取消事务暂停在提交前，启动连接进入写锁入口后再放行；独立读者见完整前态，提交后原租约释放版本等于请求终局版本，旧启动被拒，B 领取原卡 |
| A2 | 同上；`test_http_cancel_reconciles_lost_decision_commit[True-True]` | 丢弃回执后 GET 原 ID；提交后连接失联只读核对；原领取整行及全部持久表重放无变化 |
| A3 | `test_admitted_cancel_start_commit_order_and_lost_receipt[start]`；`test_past_cutoff_cancellation_preserves_execution_until_result[completed/failed]` | 受理后、裁决前启动获胜；截止点原因持久化；启动一次、取消释放零；后续完成或失败才释放 |
| A4–A5 | `test_unknown_refresh_then_seal_survives_restart_and_late_request`；JS「列表/详情 A4-A5 已领取未知请求重开和封存均只从完整快照恢复」 | 已领取旧 ID 在受理前扣住，普通读零写入；封存局部回执不恢复入口，合格完整快照才恢复 |
| A6 | `test_unknown_refresh_then_seal_survives_restart_and_late_request` | 重启后放行旧请求及重放，全部持久表无变化；本人主动新 ID 可取消 |
| A7 | `test_accepted_request_recovered_using_current_evidence`；`test_restart_recovers_accepted_id_after_start_won`；`test_transient_failure_recovers_original_id_without_client_replay` | 已受理不能封存；服务用原 ID 按当前领取/启动事实恢复；无新增取消 ID |
| A8 | `test_admitted_seal_lost_receipt_manual_repeat_is_read_only` | 第一次封存回执丢弃后本人重复命令返回 sealed，全部持久表无变化 |
| A9 | `test_nonowners_anonymous_and_invalid_csrf_cannot_change_requests`；A1 本人正对照 | 已领取资源的同组/异组非本人反馈与未知资源一致；匿名 401、无效 CSRF 403；全历史无变化 |
| A10 | JS「列表/详情 A10-A11 取消完整快照先到，跨页旧响应和局部回执不能清行或回退」 | 两页新取消完整快照先到，旧 admitted 快照后到；取消终态不回退，无旧队位或入口 |
| A11 | 上述 JS；「列表/详情 A11 局部回执先到保留完整画面和其他任务」；「跨页核对未知、主动封存、完整快照恢复入口，旧回执不能替换新请求」 | 局部回执前/后到达都不替换画面或清除其他行；共享下限标上次确认；旧 ID 不清新 ID；非法原因/字段/错 ID 被拒 |
| A12 | `test_incomplete_start_evidence_or_release_failure_preserves_original_lease` | 读取失败、未知、待核对、缺事件、running 无证据、事件归属/GPU/领取错配等均不取消、不封闭、不释放；释放、任务或终局条件写入失败回滚 |
| 升级 | `test_old_database_migration_preserves_terminal_and_sealed_requests` | 旧表无 reason 两次启动迁移，cancelled/ineligible/sealed 原字段不变；processing 原 ID 恢复一次；重放零写入 |

审查补强：`test_two_cancel_ids_race_release_original_lease_once` 观测两请求写锁竞争并计数实际释放更新为一次。A1/A3 均以提交前暂停和竞争连接进入写锁作同步，独立读者核验前态。封存、权限及提交丢失用例保留 queued/admitted 双分支，并通过真实列表/详情核验已领取任务及封存后的可取消资格。A12 增补版本写入失败回滚。页面 queued/admitted 新旧 ID 交错中，旧 ID 合法回执提高下限到 80；列表和详情均拒绝版本 79，接受版本 80 的新 ID processing 完整快照，原待核对 ID 不被解除。较新非法原因快照与命令回执均不能提高版本下限。

最终全量验证结果（审查修补后）：`uv run --locked pytest -q` 169 项通过，114.86 秒；`node --test tests/submission_page.test.cjs tests/tasks_page.test.cjs` 55 项通过，0 跳过；`uv run --locked ruff check .`、`uv run --locked python scripts/check_env.py`、`git diff --check` 全部通过。pytest 保留 1 条既有 Starlette/httpx 弃用警告。全部 A1–A12 覆盖用例已随最终回归执行，未跳过。

三层构建审查与根代理逐项核对已完成；表内 patch 均已修补并通过上述全量验证，明确延期项见 [后续工作](deferred-work.md)。规格 status=done 仅表示本次构建流程完成，Sprint 的 Story 3.2 仍为 review，等待独立人工验收。

仅本地合成账号、临时数据库和模拟执行入口，无 GPU 探测、任务命令执行、训练或基准。完整未知事实处置和恢复留 Story 3.3；原序回队及旧领取恢复联调留 4.2；综合故障/NFR 门槛留 4.6。文案与运行交互待 UX 复核。Sprint 停在 review，不宣称人工验收或综合 NFR 达标。
