# 小红书热点采集智能体：第三阶段

## 1. 本阶段解决的问题

第二阶段的每日任务使用 `task_type="agent"`。Cron 到点后先请求大模型，再由模型决定调用 `collect_xhs_hotspots` 工具。

实际手动验证时，Cron 路由和专属智能体工作区都已正常进入执行，但模型 `opencode/deepseek-v4-flash-free` 返回 `429 FreeUsageLimitError`。因此采集工具尚未执行，也没有生成当天快照。

“每天请求一次固定免费 API、保存前十条数据”不需要模型理解，也不应受模型额度影响。本阶段将它改为 Cron 直接调用受限工具。

## 2. 新的执行链路

```text
APScheduler 到点或手动运行
    -> CronManager._execute_once
    -> CronExecutor.execute
    -> CronExecutor._execute_tool
    -> collect_xhs_hotspots(limit=10, force_refresh=True, allow_paid_source=False)
    -> UAPI 免费接口
    -> 规范化校验
    -> 原始响应与快照写入 xhs_hotspot_collector 工作区
    -> Cron 历史记录 success/error
```

这个链路没有 `workspace.stream_query()`，也没有大模型调用。因此模型限流不再是每日热点采集的单点故障。

## 3. 核心代码与作用

### 3.1 `src/qwenpaw/app/crons/models.py`

`CronToolRequest` 定义直接工具任务的结构：

```python
class CronToolRequest(BaseModel):
    name: str = Field(min_length=1)
    arguments: Dict[str, Any] = Field(default_factory=dict)
```

`CronJobSpec.task_type` 扩展为 `"text" | "agent" | "tool"`。

当 `task_type == "tool"` 时，校验器要求必须包含 `tool`；同时会清除 `request` 和 `text`。这保证工具任务不会携带模型对话请求。

### 3.2 `src/qwenpaw/app/crons/executor.py`

`CronExecutor.execute()` 会在任务类型为 `tool` 时转入 `_execute_tool()`。

`_execute_tool()` 有三道安全检查：

1. 工具必须存在于当前智能体的 `ToolRegistry`。
2. 工具描述必须包含 `cron_safe=True`。
3. 工具必须在当前智能体的 `agent.json` 中处于启用状态。

随后它还比较 `job.tool.arguments` 与 `cron_fixed_arguments`。小红书任务必须严格使用：

```python
{
    "limit": 10,
    "force_refresh": True,
    "allow_paid_source": False,
}
```

所以手工修改 `jobs.json` 也不能让每日任务切换到付费 AIDATA，或改变采集范围。

在调用工具前，代码设置当前工作区、智能体 ID 和用户 ID 上下文。`collect_xhs_hotspots()` 因而仍然只会写入 `xhs_hotspot_collector` 的工作区，不会写到其他智能体或默认工作区。

### 3.3 `src/qwenpaw/plugins/api.py`

`PluginApi.register_tool()` 新增两个可选参数：

```python
cron_safe: bool = False
cron_fixed_arguments: Optional[Dict[str, Any]] = None
```

它们会写入 `ToolDescriptor.metadata`。默认值是关闭，因此其他插件工具不会自动获得 Cron 直接调用权限。

### 3.4 `plugins/tool/xhs-hotspot/xhs_hotspot.py`

注册 `collect_xhs_hotspots` 时显式声明：

```python
cron_safe=True
cron_fixed_arguments={
    "limit": 10,
    "force_refresh": True,
    "allow_paid_source": False,
}
```

这把“每日免费采集前十条”的业务规则放在工具声明处，Cron 执行器负责强制执行。

### 3.5 `plugins/tool/xhs-hotspot/schedule.py`

`build_daily_collection_job_payload()` 现在生成：

```python
"task_type": "tool",
"tool": {
    "name": "collect_xhs_hotspots",
    "arguments": {
        "limit": 10,
        "force_refresh": True,
        "allow_paid_source": False,
    },
},
```

任务依旧保存为每天 `0 9 * * *`、时区 `Asia/Shanghai`、默认禁用，便于首次部署时先手动验证。

## 4. 实际运行配置

已迁移并启用的任务：

- 智能体：`xhs_hotspot_collector`
- 任务 ID：`6d759051-c84b-4e72-9f05-91b9f14f410f`
- 时间：每天 09:00，`Asia/Shanghai`
- 数据源：`uapi_free`
- 状态：`enabled=true`

定时器只能在 QwenPaw 服务运行时触发。因此需要在采集时段保持：

```bash
cd /Users/xinyijiang/QwenPaw
venv/bin/qwenpaw app
```

## 5. 本次真实验证

2026-08-13 手动运行同一条 Cron 任务成功：

- 日志确认：`task_type=tool`、`cron tool`、`status=success`
- 未出现模型调用或模型限流
- 生成快照：
  `/Users/xinyijiang/.qwenpaw/workspaces/xhs_hotspot_collector/hotspot_data/xiaohongshu/2026-08-13/xhs-20260813T100932Z-cbad3b5c.snapshot.json`
- 原始响应：
  `/Users/xinyijiang/.qwenpaw/workspaces/xhs_hotspot_collector/hotspot_data/xiaohongshu/2026-08-13/xhs-20260813T100932Z-cbad3b5c.raw.json`
- 数据源：`uapi_free`
- 来源 URL：`https://uapis.cn/api/v1/misc/hotboard?type=xiaohongshu`
- 条数：10，排名连续为 1 至 10
- 快照 SHA-256：`f4ef3e8550360d12a3ea0e46c48b5ea51f40b74a2bd49d391345f61a68d28191`
- 原始响应 SHA-256：`2f07bb08d003002e89d6d525dde8995d56d113ccab34989f0a477b33394e1bfb`

Cron 当前状态显示下次运行时间为 `2026-08-14T09:00:00+08:00`。

## 6. 测试

```bash
venv/bin/python -m pytest tests/unit/app/crons tests/unit/plugins/test_xhs_hotspot_plugin.py -q
```

结果：`78 passed`。

测试覆盖：任务模型校验、正常工具调用、未声明 `cron_safe` 的工具拒绝执行、未启用工具拒绝执行、固定参数不匹配拒绝执行，以及小红书每日任务 JSON 的免费策略。
