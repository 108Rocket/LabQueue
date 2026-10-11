# 环境说明

本次按用户要求在 Windows 本地搭建，目录 `F:\Postgraduate Eng\Homework\LabQueue`。没有连接 V100，没有运行 GPU 任务或训练。

## 版本与可复现安装

使用 Python 3.12、uv 0.13.0、BMAD 6.12.1。实际应用依赖以 `uv.lock` 为准，BMAD/npm 依赖以 `package-lock.json` 为准。Node 至少 20.12；本机 Node 24.19.0，npm 11.17.0。

Windows 首次执行：

```powershell
.\scripts\bootstrap.ps1
```

如果 `python` 不是可用 Python，可指定已有解释器绝对路径：

```powershell
.\scripts\bootstrap.ps1 -PythonCommand 'C:\path\to\python.exe'
```

当前机器使用已有的 Codex 依赖运行时作为 Python 3.12.14 基础解释器；运行时路径未写入可移植脚本或锁文件。uv 可以在新机器上自行配置 Python 3.12。`.venv` 和 `.tools` 均不提交。

在已安装环境中加载当前终端的 uv：

```powershell
. .\scripts\env.ps1
uv --version
uv run --locked python scripts/check_env.py
```

BMAD 的 Python 辅助脚本调用 `uv`。在新开的 Codex 终端也加载上述环境，或根据 `AGENTS.md` 使用项目 uv 的绝对路径。脚本只修改当前终端 PATH，不修改系统 PATH。

若 PowerShell 阻止运行项目脚本，可针对该次运行使用 `powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1`。也可直接调用项目 uv；无需更改系统执行策略。

## Linux 普通用户安装参考

以下是后续部署参考，未在服务器上执行。先以普通用户准备 Python、Node 和 uv，再在克隆的仓库执行：

```bash
uv sync --locked --python 3.12
npm ci
npm run bmad:install
uv run --locked python scripts/check_env.py
uv run --locked uvicorn labqueue.app:app --host 127.0.0.1 --port 8000
```

无需管理员权限；默认没有外网监听、数据库迁移、GPU 探测或任务启动。SQLite 存储、账号、每用户代理与 NVML 权限在架构阶段确定。不要为搭建该环境安装 CUDA、PyTorch 或启动探测训练。

## BMAD 文件

- `_bmad/` 保存共享脚本、配置和清单；`.agents/skills/` 保存 Codex 技能。这些安装文件纳入 Git，克隆者可直接阅读和使用。
- BMAD 第三方许可证保留在 `docs/third-party/BMAD-LICENSE.txt`；本项目自身的开源许可证尚未由维护者选定。
- `_bmad/custom/config.toml` 固定团队中文配置；个人 `config.user.toml` 被 Git 忽略。
- 更新 BMAD 时保持课程版本 6.12.1，先核对课程要求与变更，不自动升级到 latest。
- `_bmad-output/planning-artifacts/` 存放规划产物；`implementation-artifacts/` 后续存 Story、Sprint 状态及实现证据。
- `product-brief.md`、`project-context.md` 当前是草稿。其他必交产物尚未完成，不用空壳文件伪装完成。

## GitHub

目标仓库是 <https://github.com/108Rocket/LabQueue>。本机已用现有 SSH 身份认证为 108Rocket，仓库使用 SSH origin。私钥只保留在本机 SSH 目录中；Git 的机器身份配置位于本机 `.git/config`，不随仓库提交。

换一台机器时使用自己的 GitHub 访问凭据。不要将私钥、token、服务器账号密码或 `.env` 放入仓库。后续推送仍需用户对当次操作的明确请求。

本机 F 盘文件系统不记录 Git 所需的文件所有权。本次仅为 `F:/Postgraduate Eng/Homework/LabQueue` 添加了一条用户级 `safe.directory` 精确例外；没有使用通配符信任其他目录。Git 用户名、邮箱和 SSH 身份选择仅配置在本仓库。

## 验证范围

`check_env.py` 检查实际导入、首页渲染、健康接口和未开放写入/执行 API 的边界。Ruff 只检查本项目代码，跳过 BMAD 第三方文件。本次检查不验证公平算法、GPU 观测、任务隔离、取消、性能或实验 A/B。
