# 知乎教程草稿

标题建议：

> 如何把 OpenClaw 配成只做研究、不碰交易权限的 A 股/美股助手？

正文草稿：

> 我做 Lobster Quant Agent 时先定了一个反常识的目标：功能可以多，但权限必须少。它能查询 A 股与延迟美股快照、生成盘前/盘后报告、做条件提醒和简化回测，但代码里没有券商连接，也没有下单、改单或撤单入口。
>
> ## 一、为什么只支持 OpenClaw
>
> 频道、模型选择、插件配置和消息发送都由 OpenClaw 管理。Codex 或 Claude Code可以协助安装，Python CLI 也方便做确定性测试，但它们不被包装成另一个独立运行时。这样可以让权限边界和运维入口保持一致。
>
> ## 二、安装前先看清四条边界
>
> - 公开行情可能延迟、限流、缺失或字段变化；
> - 回测是简化历史模拟，不等于真实可成交收益；
> - 通知目标是私密本地配置，缺失时必须 fail closed；
> - 项目不连接券商，也不接受真实持仓或账号截图作为公开 Issue 材料。
>
> ## 三、从源码安装候选版
>
> ```bash
> git clone https://github.com/cnan5336-dev/lobster-quant-agent.git
> cd lobster-quant-agent
> ./scripts/install.sh
> ```
>
> 安装器会创建仓库内的 Python 虚拟环境、安装依赖、验证并链接插件，然后写入没有凭据和通知目标的安全默认配置。它不会发送消息或启动盯盘。
>
> ## 四、先跑合成演示
>
> ```bash
> python3 python/lobster_quant_agent/cli.py demo
> ```
>
> 这一步不请求行情、不写真实观察池，也不需要频道凭据。输出来自仓库内的合成 JSON，方便先确认研究、提醒和回测的输出形态。
>
> ## 五、再做无发送验证
>
> ```bash
> openclaw config validate
> openclaw plugins inspect lobster-quant-agent --runtime --json
> ./scripts/validate.sh
> ```
>
> 等以上检查通过，再由用户自己在 OpenClaw 中配置 Telegram、微信或 QQ。主动提醒默认关闭；只有用户明确填写本地目标后才启用对应频道。
>
> ## 六、实现上的一个取舍
>
> OpenClaw 的托管插件安装会禁用 npm 生命周期脚本，因此不能假设安装时自动跑 pip。候选包把普通 HTTP 请求改成 Python 标准库适配器；只有 AkShare 增强路径是可选项，缺少时返回“暂缺”，而不是偷偷安装依赖或把数据发到别处。
>
> 项目地址：https://github.com/cnan5336-dev/lobster-quant-agent
>
> 本文只介绍软件设计和研究工作流，不构成投资建议。文中的截图和数值均为合成演示。

发布前重新执行文中的所有命令；如果 ClawHub 已公开且通过扫描，可增加官方安装命令，否则不要声称已上架。
