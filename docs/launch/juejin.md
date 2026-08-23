# 掘金教程草稿

标题建议：

> 从 OpenClaw Tool Plugin 到隐私安全的量化研究助手：打包、合成演示与 fail-closed 设计

正文草稿：

> Lobster Quant Agent 是一个 OpenClaw Tool Plugin。TypeScript 层只负责暴露 `lobster_quant`、校验配置并通过显式参数启动 Python 研究内核；行情适配、报告模板、提醒规则和简化回测位于 Python 层。
>
> ```text
> OpenClaw channel → lobster_quant → TypeScript adapter → Python research core
> ```
>
> ## 1. 插件包最小契约
>
> 候选包同时提供 `package.json`、`openclaw.plugin.json` 和构建后的 `dist/index.js`。`package.json` 分开声明插件版本、Plugin API 下限、构建时 OpenClaw/SDK 版本以及未来的 ClawHub 安装来源；`openclaw.plugin.json` 只保存加载代码前必须知道的工具和配置 Schema。
>
> ```bash
> npm run plugin:check
> npm run pack:check
> npx --yes clawhub@0.23.3 package validate .
> npx --yes clawhub@0.23.3 package publish . --family code-plugin --dry-run
> ```
>
> 最后一条是 dry-run，不会发布。真正 publish、tag、push 和 GitHub Release 都应该是不同的人工确认门。
>
> ## 2. 为什么不使用 postinstall 自动跑 pip
>
> OpenClaw 托管插件安装会忽略生命周期脚本，这是安全边界。候选包因此用 Python 标准库完成普通 JSON/text HTTP 请求，避免“插件安装成功，但第一次调用因为缺 requests 失败”。AkShare 保留为源码安装的增强适配器；缺少时返回清楚的“暂缺”。
>
> ## 3. 合成演示如何避免隐私泄露
>
> `docs/demo/synthetic-market-data.json` 是唯一输入，`scripts/generate_synthetic_demo.py` 确定性生成 SVG。CI/本地验证通过 `--check` 防止图片与来源漂移。资产没有真实账户、持仓、聊天、通知目标或实时结果。
>
> ```bash
> npm run demo:generate
> npm run demo:check
> ```
>
> ## 4. 提醒为何要 fail closed
>
> 频道名称不等于发送授权。只有 `notificationTargets` 中存在明确的本地目标时，Python 层才会调用 `openclaw message send`；否则直接返回 `skipped`。安装和验证流程也禁止发送测试消息。
>
> ## 5. 离线首用烟雾测试
>
> ```bash
> python3 scripts/first_use_smoke.py
> ```
>
> 该脚本只使用临时状态目录与合成数据，验证固定报告、回测和缺少通知目标时拒绝发送，不访问真实频道或账户。
>
> 源码：https://github.com/cnan5336-dev/lobster-quant-agent
>
> 项目只做研究和提醒，不连接券商、不执行订单；公开行情与回测均有局限。
