# 小红书热点个性化报告与邮件发送

## 设计边界

报告功能不会直接扫描、复制或发送用户完整聊天记录。用户或对话智能体应先把长期偏好整理成一段经过用户确认的摘要，例如：

```text
用户长期优先关注 AI 行业、智能体和机器人，对娱乐八卦较少关注。
```

调用 `save_xhs_report_preferences` 后，系统只保存主题、权重和用于匹配热点标题的关键词；摘要原文和完整聊天记录不会写入 `report_preferences.json`。

`generate_xhs_personalized_report(lookback_days=7)` 只读取当前智能体工作区已经保存的真实快照。若还没有连续七天数据，报告会明确写出覆盖天数不足，不会把两天数据说成七日趋势。

## 邮件发送的安全配置

邮件工具不会接收、保存或回显 SMTP 密码。先在本机终端运行以下命令，脚本会隐藏输入的 SMTP 密码或授权码，并加密写入专用智能体的 `credentials.yaml`：

```zsh
cd /Users/xinyijiang/QwenPaw
venv/bin/python scripts/configure_xhs_report_smtp.py \
  --host smtp.qq.com \
  --port 465 \
  --from-email your-account@example.com \
  --ssl
```

如服务商要求 STARTTLS，则改用 `--starttls`，不能同时使用 `--ssl`。脚本会把 `smtp_credential_ref=xhs_report_smtp` 写入邮件工具配置；`agent.json` 中只有引用名，不会出现密码。

之后的人工工作流为：先生成报告并检查报告内容和覆盖天数，再调用 `send_xhs_personalized_report_email(report_id, recipient_email)`。邮件工具只允许发送已经保存的报告，并只在工作区中记录收件人 SHA-256、发送时间、报告 ID 和传输类型；不会记录明文收件人和密码。

当前邮件发送不允许被每日 Cron 直接调用，避免未经人工确认就把内容发送到外部地址。

## 常驻服务

每日 Cron 必须在 QwenPaw 进程运行时才能触发。macOS 可以安装项目内的 LaunchAgent：

```zsh
cd /Users/xinyijiang/QwenPaw
zsh scripts/install_xhs_hotspot_launchd.sh
```

该服务登录后启动并在异常退出后重启，日志写入 `~/.qwenpaw/logs/`。电脑在计划时间完全休眠时，任务仍可能错过；需要保持联网，或在 macOS 电源设置中允许该时段唤醒。
