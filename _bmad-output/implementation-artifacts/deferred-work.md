- source_spec: 1-2-跨组按确认顺序排队并查看位置.md
  summary: Story 1.3 补查另一标签退出或切换账号后，旧任务画面的即时清理。
  evidence: 已确认原标签要等下一次读取被拒绝才清屏。联调覆盖账号切换、浏览器历史恢复和原账号的请求仍在途。
  resolution: Story 1.3 已覆盖跨标签清屏、历史恢复和旧响应晚到。见 [独立验收记录](spec-1-3-隔离任务详情-私有输出和取消权限.md#验收结果)。

- source_spec: spec-2-1-等待有效观测并按最新快照恢复派发.md
  summary: 统一约定 SQLite 页面版本计数达到整数上限时的行为。
  evidence: 现有任务和输出写入与观测共享整数版本。当 SQLite 整数已达到上限时，继续递增会转为浮点数，浏览器无法将其作为精确十进制版本使用。

# Story 3.2 构建审查续项 · 2026-11-26

- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-裁决已领取任务的取消与启动竞争.md`
  summary: Story 3.3 验证启动事实未知后的启动、派发阻断及完整原卡保留。
  evidence: 本轮按用户明确范围只验证取消不得封闭或释放；A12 未组合后续启动与派发，须在 3.3 取证。
- source_spec: `_bmad-output/implementation-artifacts/spec-3-2-裁决已领取任务的取消与启动竞争.md`
  summary: Story 3.3 处理未终结取消的未知证据在启动恢复时阻断整个服务的问题。
  evidence: cancellations._decide 对未知/待核对证据抛 DatabaseError，recover_cancellations 未逐请求处理，app.lifespan 启动恢复会中断并跳过后续请求；完整待核对与恢复验收为用户明确延后范围。
