---
name: LabQueue
description: 研究生在共享 GPU 上提交并跟踪自己任务的中文版桌面 Web。
status: draft
updated: 2026-11-24
sources:
  - ../../PRD.md
  - ../../prds/prd-LabQueue-2026-11-18/prd.md
  - ../../epics.md
  - ../../architecture.md
  - ../../../../docs/m3-acceptance.md
colors:
  canvas: '#F5F5F5'
  surface: '#FFFFFF'
  ink: '#202124'
  muted: '#525660'
  control-border: '#747780'
  divider: '#D5D7DC'
  focus: '#234F91'
  notice-surface: '#EFEFF1'
typography:
  title:
    fontFamily: 'system-ui, Microsoft YaHei, sans-serif'
    fontSize: 24px
    fontWeight: '600'
    lineHeight: '1.35'
  body:
    fontFamily: 'system-ui, Microsoft YaHei, sans-serif'
    fontSize: 16px
    lineHeight: '1.5'
  meta:
    fontFamily: 'system-ui, Microsoft YaHei, sans-serif'
    fontSize: 14px
    lineHeight: '1.5'
rounded:
  control: 4px
  panel: 6px
spacing:
  inline: 8px
  item: 12px
  group: 16px
  section: 24px
components:
  primary-action:
    background: '{colors.ink}'
    foreground: '{colors.surface}'
    radius: '{rounded.control}'
  status-label:
    foreground: '{colors.ink}'
    background: '{colors.notice-surface}'
    radius: '{rounded.control}'
  status-notice:
    foreground: '{colors.ink}'
    background: '{colors.notice-surface}'
    padding: '{spacing.group}'
---

# LabQueue M3 视觉设计

**M3 默认方案 · AI 设计建议，待人工评审。** 沿用候选 A 的视觉令牌和三页排布，候选 B 保留。参照 [M3 验收计划](../../../../docs/m3-acceptance.md) 的 Epic 2 模拟场景。

## Brand & Style · 品牌与风格

| 方向 | 视觉选择 | 评审时比较 |
| --- | --- | --- |
| A · 中性工作台 `[ASSUMPTION]` | 浅底、细分隔线和紧凑表格。按钮强调提交，任务状态、原因与下一步由文字层级区分。 | 同一屏多个任务时，能否迅速找到自己的队位和当前阻塞？ |
| B · 分区工作台 `[ASSUMPTION]` | 浅底加低饱和冷色区分内容区，任务行留白更宽，详情的状态反馈独立成面板。沿用同一状态标签和文案。 | 监测异常和执行待核对同时出现时，能否更快看出各自影响？ |

## Colors · 色彩

候选 A 使用 `{colors.canvas}` 与 `{colors.surface}` 区分页面和内容区。正文用 `{colors.ink}`，时间等辅助信息用 `{colors.muted}`。获准、运行、完成和失败沿用同一文字标签样式。排队原因、监测提醒与失败诊断使用可读文本。普通文字以 4.5:1 为对比目标，必需的控件边界和焦点提示以 3:1 为目标。实际对比度随实现复核。

## Typography · 字体

`{typography.title}` 用于页标题，`{typography.body}` 用于表单标签、队位和状态，`{typography.meta}` 用于时区及更新时间等辅助信息。数值和时间按列对齐。长任务名和原因可换行，完整内容在详情可读。

## Layout & Spacing · 布局与间距

顶部并排放置两个导航入口，页面标题置于内容区首行。提交页按填写顺序纵向排列。我的任务页由上到下放本次读取反馈和本人任务表。详情依次显示任务状态、该状态的解释及可用动作、自己的任务配置。队位与等待原因同组。监测提醒与任务状态、执行待核对分行，可同时展示。列表以「—」表示无当前队位，详情省略当前队位。字段内间距用 `{spacing.inline}`，控件组用 `{spacing.group}`，区块之间用 `{spacing.section}`。桌面窗口变窄或放大时优先保留可读的状态、原因和按钮。

三页排布见 [低保真框图](wireframes.md#三页框图)。

## Shapes · 形状

`{rounded.control}` 用于按钮、输入框和状态标签，`{rounded.panel}` 用于提示区和确认框。边界靠间距、分隔线和文字层级建立。

## Components · 组件

| 组件 | 视觉规则 |
| --- | --- |
| 顶部导航 | 并排显示两个文字链接。当前页有文字样式和下划线。 |
| 任务表单 | 常驻字段标签靠近输入框，说明和错误紧跟字段。可编辑控件使用 `{colors.control-border}` 提示边界。 |
| 反馈区 | 使用 `{components.status-notice.background}` 浅底和可读标题。监测提醒、页面读取失败和执行待核对各有独立文字。 |
| 任务表 | 表头、行分隔线和可打开本人任务详情的名称构成扫描顺序。当前状态和原因可换行，行间用 `{colors.divider}` 分隔。 |
| 任务状态区 | 使用 `{components.status-label.background}` 浅底标签，依次显示状态、本人队位或诊断及可用动作。上次确认的内容有文字标识，本人诊断可换行。 |
| 操作按钮 | 提交按钮用 `{components.primary-action.background}` 与 `{components.primary-action.foreground}`。刷新、取消入口使用文字与边框，不可用原因在旁边明确说明。所有可操作元素显示 `{colors.focus}` 的键盘焦点，刷新反馈不改变按钮位置。 |
| 取消确认框 | 依次呈现标题、任务名、截止条件和两个有明确动词的按钮。焦点轮廓使用 `{colors.focus}`。 |
| 任务配置 | 使用标签和值的行式布局。长命令或路径可以阅读完整内容，和状态提示保持可见间隔。 |

## Do's and Don'ts · 使用规则

- 状态字样、原因与操作保持可见，图标和颜色仅作辅助。
- 排队状态使用队位和等待原因。终态在任务标题附近展示，并保留失败诊断的阅读空间。
- 刷新失败、监测异常和执行状态待核对各有独立文案，时间标签写清含义与时区。
