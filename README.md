# LabQueue

面向研究生的共用 GPU 排队项目：一台服务器、两张 L20、多个研究组，普通用户权限部署。

当前交付是 **BMAD 作业环境与项目骨架**。本地首页、健康接口可以运行；账号、队列、GPU 监测和任务执行尚未实现。没有运行任何训练或 GPU 实验。

## 本地启动

已搭建的 Windows 环境：

```powershell
Set-Location 'F:\Postgraduate Eng\Homework\LabQueue'
.\scripts\dev.ps1
```

访问 <http://127.0.0.1:8000>，API 文档在 <http://127.0.0.1:8000/docs>。关闭终端中的服务按 `Ctrl+C`。

首次在另一台 Windows 机器安装：先准备 Python ≥ 3.10 和 Node.js ≥ 20.12，再执行：

```powershell
.\scripts\bootstrap.ps1
```

脚本会在项目内安装 uv 0.13.0、按锁文件建立 Python 3.12 环境，并安装 BMAD 6.12.1。若系统禁止执行 `.ps1`，可仅对本次命令使用 `powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1`。详见 [环境说明](docs/setup.md)。

## 接下来完成作业

1. 在 Codex 中打开本仓库，开启新对话，调用 `bmad-help`。
2. 根据 [产品简报草稿](_bmad-output/planning-artifacts/product-brief.md) 运行产品简报、PRD 工作流，人工评审后完成 M1。
3. 运行架构、Epic/Story、项目上下文和实施准备检查，完成 M2，再开始故事实现。
4. 每次工作流使用新对话，持续更新需求追溯、[真实决策记录](decision-log.md) 和迭代状态，完成实验 A、实验 B 及最终演示。

具体命令、课程门槛和交付清单见 [作业要求与流程](docs/course-requirements.md)。BMAD 技能在 AI 对话中调用，不是 PowerShell 命令。课程要求的 PRD、5 条 ADR、12 条 Story、两轮 Sprint 和实验结果仍需后续真实完成。

## 项目约束

- 普通用户权限无法阻止其他用户绕过队列。平台应观察外部占用，在监测不明确时暂停新分配。
- 每位研究生所属研究组需要可信确认；公平策略需要兼顾组间和组内等待。
- 首次显存未知的任务需要保守准入。初期每卡最多一个平台任务，运行中记录实际显存；历史采样不能保证未来峰值。
- 不终止其他用户的进程，不把所有人的命令放到同一个 Linux 身份下执行。
- 本阶段只在本地搭建和检查环境，真实 GPU 运行方案在架构评审时确定。

详见 [项目上下文](_bmad-output/project-context.md) 与 [AGENTS.md](AGENTS.md)。

## 检查

```powershell
.\.tools\python\bin\uv.exe run --locked python scripts/check_env.py
.\.tools\python\bin\uv.exe run --locked ruff check .
```

这是启动和代码检查，不代表真实服务器调度、公平性或 NFR 验证通过。

## 团队与 AI 使用声明

团队要求 3–4 人；姓名、学号和分工待补充。当前已知 GitHub 维护者：108Rocket。

本次环境骨架和文档由用户与 Codex 协作生成。BMAD 版本固定为 6.12.1；具体 AI 模型名称、版本、日期及各阶段用途请按实际界面和工作记录补充。本仓库保留真实人工约束和决策，不填造实验数据、评审结论或 AI 拒绝次数。
