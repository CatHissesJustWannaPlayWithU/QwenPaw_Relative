# 小红书热点采集插件

这个插件是“社交媒体热点数据采集智能体”的第一阶段实现：它默认通过免费的 UAPI 公开接口获取小红书热搜，严格校验前十条数据，并在当前智能体工作区同时保存原始响应和规范化快照。AIDATA 仅保留为可选的付费备用来源。

## 当前能力

- 工具 `collect_xhs_hotspots`：请求数据源、校验排名和标题、保存原始 JSON 与规范化 JSON。
- 工具 `list_xhs_hotspot_snapshots`：查看当前智能体已经保存的快照摘要。
- 所有输出位于当前智能体的 `hotspot_data/xiaohongshu/` 目录，不接收任意文件路径参数。
- 每份文件都会计算 SHA-256；原始响应保留来源 URL、获取时间、状态码和安全响应头，便于复核。
- 工具 `save_xhs_report_preferences`：从用户确认过的记忆摘要中保存兴趣主题和权重，不保存聊天原文。
- 工具 `generate_xhs_personalized_report`：按兴趣权重对当前真实热点快照进行可解释排序，并保存 JSON 与 Markdown 简报。
- 工具 `send_xhs_personalized_report_email`：人工确认报告和收件人后，通过当前智能体的加密 SMTP 凭据发送纯文本简报。

## 配置方法

在 QwenPaw 中启用“小红书热点采集”插件后，直接启用工具即可使用免费默认来源：

`https://uapis.cn/api/v1/misc/hotboard?type=xiaohongshu`

该接口不需要 API Key，返回 `index`、`title`、`hot_value`、`extra.type`、可回到小红书搜索页的 `url`，以及顶层 `update_time`。它来自 [UAPI 热榜接口文档](https://uapis.cn/en/docs/api-reference/get-misc-hotboard?type=hupu)。该服务仍是第三方公开聚合接口，免费策略、可用性和限流规则都有可能变化，因此每次采集仍必须保存原始响应与来源信息。

“默认免费”在这里的准确含义是：截至本次实际联通测试，调用不需要注册、登录或 API Key，也没有向 AIDATA 发出请求；它不等于承诺永久不限量。定时任务应保持每天一次的低频调用，并把每次失败记录下来，避免把第三方服务的策略变化误判为数据缺失。

2026-08-10 已复测 60s 的 `https://60s.viki.moe/v2/rednote`：其主域名返回 HTTP 500，报错为 `Cannot read properties of undefined (reading 'map')`，所以不再作为默认来源。代码会在旧配置仍填写 `60s_free` 时明确报错，而不会静默保存异常数据。

只有在工具配置中把 `source` 改为 `aidata_paid` 时，才需要填写 AIDATA API Key 和地址 `https://aidata.vip/api/v1/data/xiaohongshu/trending`。AIDATA 文档列出的价格是：新请求 `$0.01`，30 分钟缓存结果 `$0.0005`。

## 数据可信边界

这里的数据来自第三方 API，而不是小红书官方历史档案。插件会记录来源、采集时间和数据源提供的更新时间；若数据源标记为缓存、没有更新时间、没有热度值或没有链接，会在 `warnings` 中明确提示。免费来源不需要 Key，也不会生成任何伪造数据。

## 个性化报告与邮件

个性化报告不直接读取或外发完整聊天记忆。应由用户或对话智能体先整理一段经用户确认的偏好摘要，再调用 `save_xhs_report_preferences`；插件只保存主题、权重和匹配关键词。报告会写出真实快照覆盖天数，未积累满七天时不会声称形成完整周趋势。

邮件发送只支持人工调用，不能由每日 Cron 自动触发。SMTP 密码或授权码必须通过 `scripts/configure_xhs_report_smtp.py` 写入当前智能体的加密 `credentials.yaml`；不要把密码写进 `plugin.json`、`agent.json`、聊天消息、Git 或 Word 文档。完整操作说明见 `docs/XHS_HOTSPOT_PERSONALIZED_REPORT_AND_EMAIL_zh.md`。
