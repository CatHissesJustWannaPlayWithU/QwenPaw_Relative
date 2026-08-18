# 小红书热点采集智能体：第二阶段（每日任务规格）说明

## 本阶段目标

第二阶段不直接创建一个正在运行的定时任务，而是先把它需要的“任务合同”固化为代码并通过 QwenPaw 的 Cron 模型校验。这样可以先人工运行一次，确认真实数据、文件和来源都正确，再开启每天自动执行，避免空跑或产生非预期费用。

## 新增内容

### `plugins/tool/xhs-hotspot/schedule.py`

`build_daily_collection_job_payload()` 生成能提交给 `POST /api/cron/jobs` 的 JSON：

- 默认时间：每天 `09:00`；
- 时区：`Asia/Shanghai`；
- 默认 `enabled=false`；
- 任务类型：`agent`，由 QwenPaw 现有 CronExecutor 把严格指令交给指定智能体；
- `target.user_id` 与 `target.session_id` 必填，分别标识任务以哪个用户身份运行、把结果写进哪个逻辑会话；
- `share_session=false`，Cron 使用自己的独立会话，不污染普通聊天记录；
- `save_result_to_inbox=true` 且 `silent=false`，方便在控制台、收件箱和任务历史中审查每天结果；
- `max_concurrency=1`，上一次尚未结束时不会并发产生第二次采集；
- `misfire_grace_seconds=600`，错过计划时间十分钟内仍可补跑；
- `timeout_seconds=120`，超过两分钟即记录失败，不会无限挂起。

### 严格任务指令

`DAILY_COLLECTION_PROMPT` 规定运行时：

1. 只调用一次 `collect_xhs_hotspots`；
2. 固定参数为 `limit=10`、`force_refresh=true`、`allow_paid_source=false`；
3. 不使用浏览器、网页爬取或其他工具；
4. 失败时只报告失败，不编造榜单；
5. 成功时报告快照 ID、来源更新时间、两份保存路径、前三条标题和警告。

QwenPaw 现有 Cron 是“智能体任务”而不是一个任意 Python 函数调度器，所以这段指令是它约束工具调用的核心。生产部署应使用一个专门的热点采集智能体，并只为该智能体启用本插件的三个工具；这样才适合将 `tool_safety=false` 用于无人值守的低风险数据请求。

### 付费防护

`tools._build_provider_client()` 新增 `allow_paid_source`。即使工具配置被改成 `aidata_paid`，调用方仍必须在本次调用显式传入 `allow_paid_source=true` 才能创建 AIDATA 客户端。每日 Cron 指令固定传 `false`，所以任务不会意外触发付费备用源。

## 实际启用顺序

1. 安装插件，并仅给专用采集智能体启用小红书热点工具；
2. 用 `build_xhs_daily_collection_job` 生成默认关闭的 JSON，填写该智能体所属用户的 `user_id` 与一个专用 `session_id`；
3. 提交 JSON 到 `/api/cron/jobs`，仍保持关闭；
4. 用 `POST /api/cron/jobs/{job_id}/run` 手动执行一次；
5. 核对返回的 `ok=true`、前十名、`raw_path`、`snapshot_path`、两个 SHA-256 和任务历史；
6. 确认无误后才把该 Job 更新为 `enabled=true`。

## 关键边界

- 每日任务的执行需要 QwenPaw 服务在运行；电脑休眠或进程停止期间不会自行采集。
- 免费 UAPI 是第三方聚合服务，返回成功并不等于小红书官方背书；原始响应和来源 URL 必须长期保留。
- 这里的 `user_id` 必须来自服务器已认证的用户记录，不能用前端显示名替代。
- 这一步还没有创建实际 Job，也没有生成 Excel。连续保存多个真实快照后，才进入汇总和导出阶段。
