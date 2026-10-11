# 本次环境检查记录

日期：2026-10-11。执行位置：用户指定 Windows 本地项目目录；只验证环境和代码，不涉及 V100、L20 任务、训练或性能实验。

| 检查 | 实际结果 | 边界 |
| --- | --- | --- |
| BMAD 安装 | 6.12.1，core + bmm，29 个 Codex 技能 | 未执行产品/架构工作流 |
| BMAD 配置 | 配置与工作流自定义解析器能运行，交流及文档为 Chinese | 草稿没有变成已评审产物 |
| Python 环境 | 3.12.14，`uv sync --locked` 成功 | 无训练库或 GPU 实验 |
| Windows 安装脚本 | 指定已有 Python、`-SkipBmad` 运行成功 | 不声称在新机器完整冷安装实测 |
| PowerShell 脚本 | bootstrap、env、dev 语法解析通过 | 本机检查 |
| 启动脚本 | `scripts/dev.ps1 -Port 18080` 实际启动 Uvicorn，检查后 Ctrl+C 停止 | 开发服务只绑定回环地址 |
| 实际 HTTP | 首页、`/health`、`/openapi.json` 均成功，首页 HTTP 200 | 没有调度业务接口 |
| 执行边界 | 健康接口 execution=false，monitoring=not-implemented；OpenAPI 没有写入方法 | 尚未实现 GPU 执行器 |
| 源码检查 | Ruff lint、格式检查、Python 编译通过 | 第三方 BMAD/uv 不纳入本项目 lint |

当前 Starlette 对 httpx 测试适配器显示弃用提示，检查仍通过。BMAD 固定课程版本带有 npm 的 glob 弃用提示。本次保留课程版本与已锁定的可用依赖，后续依赖调整需更新锁文件并复查。

未验证：真实 GPU 监测权限、平台外占用竞态、组公平、任务隔离、OOM、租约恢复、性能 NFR、实验 A/B。上述验证必须在功能实现及对应评审之后开展。
