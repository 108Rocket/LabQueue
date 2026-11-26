# LabQueue

面向研究生的共用 GPU 排队项目：一台服务器、两张 L20、多个研究组，普通用户权限部署。

当前可在本地用合成账号提交任务、核对入队回执，并在「我的任务」查看全局队位和等待原因。显存可以未知，任务命令仅作为数据保存。

## 本地启动

在仓库根目录为合成账号设置本次本地运行的密码，再启动服务。`demo-a` 属于 `group-a`，`demo-b` 属于 `group-b`，组映射在服务端预置。

```powershell
$env:LABQUEUE_DEMO_A_PASSWORD = Read-Host '为 demo-a 设置本地模拟密码'
$env:LABQUEUE_DEMO_B_PASSWORD = Read-Host '为 demo-b 设置本地模拟密码'
.\scripts\dev.ps1
```

访问 <http://127.0.0.1:8000>，API 文档在 <http://127.0.0.1:8000/docs>。关闭终端中的服务按 `Ctrl+C`。

登录后可用任务名和合成命令（例如 `echo synthetic-job`）提交，显存选择「未知」即可。任务保存在运行账号的私有目录，Windows 使用 `%LOCALAPPDATA%\LabQueue\labqueue.sqlite3`，Linux 使用 `~/.local/share/labqueue/labqueue.sqlite3`。服务重启后重新登录，可用原提交键核对回执。

依次用 `demo-a` 和 `demo-b` 提交任务，再分别进入「我的任务」及其任务详情。两个账号各自只会看到本人任务。全局队位按确认入队的顺序跨组计算，库中原有的排队任务也计入队位。常规启动后尚无有效模拟观测，页面显示「模拟 GPU 监测失败，暂停派发」。页面「刷新」只读取任务；晚到的旧版本不会覆盖较新的队位和原因。页面标为「上次确认」时，可刷新以获取当前快照。此页面使用浏览器的 Web Locks 和本地存储，在同一登录会话的多个窗口间核对版本。

要撤回尚未领取的任务，打开本人任务详情，点击「取消任务」并确认。列表和详情读取到已提交的取消后显示「已取消」，后续任务队位前移。遇到超时可先刷新核对；若结果仍为未知，可主动确认封存旧请求。页面核实旧请求已终结或封存、任务仍可取消后，才会再次开放取消按钮。运行 `uv run --locked pytest -q tests/test_cancellations.py` 可复现双卡排队、取消、重启与重放。

本地模拟执行方确认启动后，「我的任务」和详情显示「运行中」。确认执行结束并释放原卡后，刷新可见「已完成」或「失败」。模拟 OOM 显示「GPU 显存不足（OOM）」。在项目环境中运行 `uv run --locked pytest -q tests/test_dispatch.py -k end_is_atomic` 可复现提交、启动、结束及同卡交接。

详情有私有输出时，提交者可点击「读取私有输出」。独立验收用测试夹具写入合成输出。设置 `LABQUEUE_DEMO_A_PEER_PASSWORD` 可启用同组测试账号 `demo-a-peer`。在另一标签页退出或换号后，旧标签页会清除已显示的任务和输出。

启动时会核验目录、主库和 SQLite 日志的实际权限。若旧版本的 `data/labqueue.sqlite3` 仍有任务，先停服，通过 SQLite 备份接口将完整数据库迁入上述私有目录。核对任务数和原提交回执后，再处置旧副本。

首次在另一台 Windows 机器安装：先准备 Python ≥ 3.10 和 Node.js ≥ 20.12，再执行：

```powershell
.\scripts\bootstrap.ps1
```

脚本会在项目内安装 uv 0.13.0、按锁文件建立 Python 3.12 环境，并安装 BMAD 6.12.1。若系统禁止执行 `.ps1`，可仅对本次命令使用 `powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1`。详见 [环境说明](docs/setup.md)。

## 接下来完成作业

1. 在 Codex 中打开本仓库，开启新对话，调用 `bmad-help`。
2. 根据 [产品简报草稿](_bmad-output/planning-artifacts/product-brief.md) 运行产品简报、PRD 工作流，人工评审后完成 M1。
3. 运行架构、Epic/Story、项目上下文和实施准备检查，完成 M2，再开始故事实现。
4. 每次工作流使用新对话，持续更新需求追溯、[真实决策记录](decision-log.md) 和迭代状态，完成实验 A，并将可复现的验收证据与复盘纳入仓库。

作业仅通过 GitHub 仓库交付。不汇报、不录屏、不做实验 B，保留实验 A。

具体命令、课程门槛和交付清单见 [作业要求与流程](docs/course-requirements.md)。BMAD 技能在 AI 对话中调用，不是 PowerShell 命令。PRD 已有草稿，团队评审待完成。5 条 ADR、12 条 Story、两轮 Sprint 和实验 A 结果仍需后续完成。

## 项目约束

- 普通用户权限无法阻止其他用户绕过队列。平台应观察外部占用，在监测不明确时暂停新分配。
- 每位研究生所属研究组需要可信确认；公平策略需要兼顾组间和组内等待。
- 首次显存未知的任务需要保守准入。初期每卡最多一个平台任务，运行中记录实际显存；历史采样不能保证未来峰值。
- 不终止其他用户的进程，不把所有人的命令放到同一个 Linux 身份下执行。
- 当前本地功能使用合成账号和任务数据。真实 GPU 运行方案遵循架构评审条件。

详见 [项目上下文](_bmad-output/project-context.md) 与 [AGENTS.md](AGENTS.md)。

## 检查

```powershell
.\.tools\python\bin\uv.exe run --locked python scripts/check_env.py
.\.tools\python\bin\uv.exe run --locked ruff check .
.\.tools\python\bin\uv.exe run --locked pytest -q
node --test tests/submission_page.test.cjs tests/tasks_page.test.cjs
```

测试使用独立临时 SQLite 库和合成账号。Story 1.1 覆盖提交、权限校验、回执核对及幂等重放。Story 1.2 覆盖跨组队位、重启、只读权限和 WAL 快照一致性。Story 2.1 覆盖观测失败、漏报、占用不明、精确到期、基于最新快照恢复候选和旧库迁移。Node 测试覆盖提交反馈，以及列表和详情在乱序响应、切页和重开时的版本保护。

可单独验证本地模拟观测和任务页。

```powershell
.\.tools\python\bin\uv.exe run --locked pytest -q tests/test_submissions.py -k "observation or old_queue or task_snapshot_reads"
node --test tests/tasks_page.test.cjs
```

## 团队与 AI 使用声明

团队共四人，主要负责人为 **108Rocket** 与 **Vamsinger**，另外两人暂称 **组员 A**、**组员 B**。分工方案如下，真实姓名、学号及课程登记信息待补充。

| 成员 | 主责 | 计划工作量 |
| --- | --- | --- |
| Vamsinger | 产品需求、故事与冲刺、排队业务、范围取舍和业务验收 | 约 35% |
| 108Rocket | 架构/ADR、GPU 资源与执行机制、环境、集成和部署 | 约 35% |
| 组员 A | 交互与前端、页面验收和使用说明 | 约 15% |
| 组员 B | 自动化验收与 NFR 测试、实验 A 取证、复盘数据和安装复现 | 约 15% |

交付物负责人、四周安排、实验 A 分工及交叉评审见 [四人任务分工方案](docs/team-allocation.md)。比例是未来投入安排，不代表已完成贡献；具体开发任务在 PRD 与架构评审后拆分。当前已知 GitHub 维护者为 108Rocket。

本次环境骨架和文档由用户与 Codex 协作生成。BMAD 版本固定为 6.12.1；具体 AI 模型名称、版本、日期及各阶段用途请按实际界面和工作记录补充。本仓库保留真实人工约束和决策，不填造实验数据、评审结论或 AI 拒绝次数。
