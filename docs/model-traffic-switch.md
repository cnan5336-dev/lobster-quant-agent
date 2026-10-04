# 可选的 Codex 流量开关

此功能需要显式初始化。仅安装项目不会修改原有模型配置。当前适配范围是原生本机 OpenClaw 的 `main` agent：关闭时使用现有 `deepseek-official/deepseek-v4-flash`，开启时使用本机 `http://127.0.0.1:8317/v1` 上的 `cliproxy/gpt-6-luna`。其他模型、远程网关或容器配置需要另行适配。

运行时适配器已对照 OpenClaw `2026.7.1-2` 验证。它依赖该版本的配置 schema 和状态存储，其他版本不保证可用；无法确认兼容时会明确拒绝，不绕过检查。初次安装先保持关闭，再在真实终端运行隐藏输入程序：

```bash
python3 python/lobster_quant_agent/cli.py model codex off
python3 scripts/prepare-cliproxy-key.py
```

输入程序只接受用户手动输入的新客户端密钥，不读取现有代理配置，也不会启动请求或开启开关。完成兼容性检查后，再按需开启。

如果代理返回客户端鉴权错误，需要重录已有输入时，明确使用 `python3 scripts/prepare-cliproxy-key.py --replace`。它仍要求真实终端和两次隐藏输入；不会读取旧密钥，确认原文件未被其他操作改变后才原子替换。应输入 CLIProxyAPI 的客户端 API key，而非 OAuth token、管理密码或 DeepSeek 密钥。该命令不会重置代理服务的密钥，也不会影响其他客户端。

```bash
python3 python/lobster_quant_agent/cli.py model codex status
python3 python/lobster_quant_agent/cli.py model codex on
python3 python/lobster_quant_agent/cli.py model codex off
```

`model` 不带参数等同于状态查询，不产生模型请求。`nl "开启Codex流量"`、`nl "关闭Codex流量"` 和 `nl "Codex流量状态"` 也是对应入口。自然语言指令只匹配明确的完整指令；解释、引用和否定句不会被当作开关操作。

初始化及切换需要本机网关可用并支持配置热加载；控制器通过网关实际运行配置确认生效。不能只凭配置文件已写入就报告切换成功。网关不可达、已有会话/后台任务存在冲突覆盖项、配置损坏或不支持的运行环境均会明确失败。配置已写入但确认失败时，应先查询状态，避免盲目重试。

关闭后，受控请求只走 DeepSeek；开启后只走代理，不使用插件原有 `primaryModel` / `fallbackModels` 绕过开关。开关只决定后续请求，不取消已经发出的请求。代理失联或额度不足时不会自动回退到其他模型，可直接从本机终端执行 off，控制命令本身不需要模型响应。

代理客户端密钥由用户在本机交互式隐藏输入，存放在 OpenClaw 私有目录的 `0600` 文件，通过文件 SecretRef 引用。不要把密钥放在聊天、命令参数、版本库或示例配置中；不要从已有代理配置复制 OAuth 凭据。切换关闭时使用禁用标记，避免遗留的显式请求仍带有效客户端凭据。

安装此模块但未初始化的用户继续沿用原有模型路由。初始化后策略文件缺失或损坏将阻止受控请求，不静默恢复旧备用路径。状态与测试输出不包含凭据或会话正文。

验证范围和本机实测结果请查看交付的接入验收记录。合成测试可验证传输兼容性，不能证明账户未来额度、上游可用性或真实交易时段的策略效果。
