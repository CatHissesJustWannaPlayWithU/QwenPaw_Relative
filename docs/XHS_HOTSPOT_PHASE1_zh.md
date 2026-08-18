# 小红书热点采集智能体：第一阶段说明

## 这一阶段完成了什么

第一阶段只搭建“真实数据进入系统前的安全通道”，没有伪造任何小红书热点，也没有设置定时任务。完成后，智能体默认可通过免费来源，或在需要时通过已配置 API Key 的备用来源：

1. 请求第三方热点 API；
2. 只接受可识别的热点列表；
3. 严格校验前十条排名、标题和重复情况；
4. 保存原始响应与规范化快照；
5. 为两份文件生成 SHA-256，保留可追溯来源；
6. 只写入当前智能体工作区，避免跨智能体混放数据。

## 数据源策略：免费优先

默认来源是 UAPI 公开接口：`https://uapis.cn/api/v1/misc/hotboard?type=xiaohongshu`。它不需要 API Key，已实际验证可返回小红书热点的 `index`、`title`、`hot_value`、`extra.type`、`url` 和顶层 `update_time` 字段。它是第三方聚合服务，不是小红书官方历史档案，所以系统仍会记录来源 URL、采集时间、原始响应和 SHA-256；当免费策略或服务可用性变化时，可以手动切换到 AIDATA，而无需改动采集、校验和存储逻辑。

这里的“免费”只表示本次实际调用没有要求注册、登录或 API Key，且没有触发 AIDATA 的付费接口；它不是“永远不限量”的承诺。后续定时任务会按每天一次的低频策略调用，并对 HTTP 状态、返回结构和保存结果做记录，以便发现第三方服务策略变化或临时故障。

此前评估过的 60s 开源 API 在 2026-08-10 复测时返回 HTTP 500，正文错误是 `Cannot read properties of undefined (reading 'map')`。这代表其服务端把非列表数据当作列表调用 JavaScript 的 `.map()`，属于服务端或其上游数据异常，而不是浏览器配置错误。因此代码已经不再把 60s 设为默认来源；旧配置若仍使用 `60s_free` 会得到明确错误，而不会产生看似成功的空数据文件。

AIDATA 是备用来源而不是默认来源。其完整地址为 `https://aidata.vip/api/v1/data/xiaohongshu/trending`。直接在浏览器打开会因为没有携带 `Authorization: Bearer <API Key>` 请求头而得到 `401 Unauthorized`；这是鉴权结果，不代表地址失效。只有把工具配置的 `source` 设置为 `aidata_paid` 后，代码才会读取 AIDATA Key 并发起付费请求。

## 文件与职责

### `plugins/tool/xhs-hotspot/plugin.json`

这是插件清单。它声明两个工具：`collect_xhs_hotspots` 和 `list_xhs_hotspot_snapshots`，还声明 API Key、数据源地址和超时时间三项配置。API Key 类型是 `password`，因此配置界面不应把它当普通文本公开显示。

### `plugins/tool/xhs-hotspot/xhs_hotspot.py`

`XiaohongshuHotspotPlugin.register()` 是插件入口。它把 Python 函数注册为可由 QwenPaw 智能体调用的工具；它本身不负责请求网络或写文件。

### `plugins/tool/xhs-hotspot/provider.py`

`UapiHotspotClient.fetch()` 是默认网络边界，不需要 API Key；它校验返回的平台类型是 `xiaohongshu` 且 `list` 是热点列表。`AidataHotspotClient.fetch()` 是可选网络边界，只有选择付费来源才会把 API Key 放入 `Authorization: Bearer ...` 请求头，并传入 `refresh=true`。两个客户端都会处理网络错误、429 和 5xx 重试，只保留安全响应头，绝不把请求头或 Key 写入原始快照。

### `plugins/tool/xhs-hotspot/core.py`

`normalize_snapshot()` 是数据质量关卡。它先从 `items`、`hot_list`、`list`、`data` 等常见字段中找出列表，再标准化 `rank`、`title`、`hot_value`、`hot_text`、`trend_label`、`url` 等字段。UAPI 的 `hot_value`，例如 `920.8w`，会被转换成数值 `9208000`，同时保留原始显示文本；其 `extra.type` 会成为趋势标签。随后必须满足：

- 有至少十条有效记录；
- 前十名恰好是 1 到 10；
- 前十条标题不能重复。

否则抛出 `SnapshotValidationError`，存储层不会被调用。这里的逻辑保证“Excel 里的第一名到第十名”不是接口异常导致的伪数据。

### `plugins/tool/xhs-hotspot/storage.py`

`save_snapshot()` 把同一次采集写成两份文件：

- `*.raw.json`：第三方响应与安全元数据；
- `*.snapshot.json`：统一字段、校验结果、警告和榜单指纹。

文件路径是 `<当前智能体工作区>/hotspot_data/xiaohongshu/YYYY-MM-DD/`。`_atomic_write()` 先写临时文件，再原子替换，避免程序中断留下半份 JSON。`SHA-256` 用于之后证明文件未被悄悄修改。

### `plugins/tool/xhs-hotspot/tools.py`

`collect_xhs_hotspots()` 把整个流程串起来：读取工具配置 -> 选择 `uapi_free` 或 `aidata_paid` 来源 -> 请求来源 -> 规范化并校验 -> 保存 -> 返回摘要。旧的 `60s_free` 配置会被明确拒绝，防止服务端 500 被误当成可用数据。

关键点是 `_workspace_dir()` 通过 `get_current_workspace_dir()` 获得当前智能体工作区。调用方不能传入一个任意磁盘路径，所以热点数据不会被工具误写到另一个智能体的数据区。

`list_xhs_hotspot_snapshots()` 用于检查连续采集结果；它同样只读取当前工作区。

## 一次成功采集的逻辑

```text
智能体调用 collect_xhs_hotspots
    -> 读取当前智能体的数据源配置
    -> 免费来源直接 GET UAPI；付费来源才带 Bearer Key 和 refresh=true
    -> normalize_snapshot 校验前十条
    -> save_snapshot 保存 raw + snapshot
    -> 返回榜单、文件位置、SHA-256 与警告
```

## 测试范围

`tests/unit/plugins/test_xhs_hotspot_plugin.py` 覆盖了：前十条规范化、重复标题拒绝、排名不连续拒绝、双文件保存与 SHA-256、请求头和 `refresh=true`、插件工具注册。

## 尚未做的内容

本阶段没有：每天的 Cron、跨天去重、Excel 报表、七日分析。这些内容必须建立在连续、真实、可追溯的快照之上，不能现在提前编造七天历史。
