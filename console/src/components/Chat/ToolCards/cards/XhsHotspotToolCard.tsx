import React from "react";
import {
  CheckCircleOutlined,
  DownloadOutlined,
  FileTextOutlined,
  MailOutlined,
  SyncOutlined,
} from "@ant-design/icons";
import type { ToolCallContent } from "../shared/types";
import { ToolCardShell } from "../shared";
import { shortFileName, toDisplayUrl } from "../shared/utils";
import styles from "../shared/toolCards.module.less";

export interface XhsHotspotToolCardProps {
  content: ToolCallContent;
  isStreaming?: boolean;
}

type ToolResult = Record<string, unknown>;

/** 从工具返回的 JSON、文本块或嵌套 output 中读取可展示的结果。 */
function parseToolResult(value: unknown, depth = 0): ToolResult {
  if (depth > 4 || value == null) return {};
  if (typeof value === "string") {
    try {
      return parseToolResult(JSON.parse(value), depth + 1);
    } catch {
      return {};
    }
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const parsed = parseToolResult(item, depth + 1);
      if (Object.keys(parsed).length > 0) return parsed;
    }
    return {};
  }
  if (typeof value !== "object") return {};

  const record = value as ToolResult;
  if ("ok" in record || "report_id" in record || "report_file_path" in record) {
    return record;
  }
  if (typeof record.output === "string" || record.output) {
    return parseToolResult(record.output, depth + 1);
  }
  if (typeof record.text === "string") {
    return parseToolResult(record.text, depth + 1);
  }
  if (record.data) return parseToolResult(record.data, depth + 1);
  return record;
}

function readableError(result: ToolResult): string {
  const error = typeof result.error === "string" ? result.error : "";
  if (error.includes("还没有可用于生成报告的热点快照")) {
    return "还没有可用的热点数据，请先完成一次采集后再生成简报。";
  }
  if (error.includes("模型") || error.includes("Quota")) {
    return "当前模型暂时不可用，请稍后重试。";
  }
  return "本次操作未完成，请稍后重试。";
}

function toolTitle(name: string, done: boolean): string {
  const action = done ? "已完成" : "正在处理";
  const labels: Record<string, string> = {
    collect_xhs_hotspots: "小红书热点采集",
    list_xhs_hotspot_snapshots: "热点数据检查",
    build_xhs_daily_collection_job: "每日采集任务准备",
    save_xhs_report_preferences: "关注方向已更新",
    generate_xhs_personalized_report: "小红书热点简报",
    create_xhs_briefing: "小红书热点简报",
    list_xhs_personalized_reports: "已生成简报",
    send_xhs_personalized_report_email: "简报邮件发送",
  };
  return `${labels[name] || "小红书热点服务"}${action}`;
}

function reportBody(result: ToolResult): React.ReactNode {
  const availableDays = Number(result.available_days || 0);
  const requestedDays = Number(result.requested_days || 0);
  const filePath = typeof result.report_file_path === "string" ? result.report_file_path : "";
  const fileName =
    (typeof result.report_file_name === "string" && result.report_file_name) ||
    shortFileName(filePath) ||
    "小红书热点简报.xlsx";
  const mode = result.briefing_mode === "综合版" ? "综合版" : "个性化";

  return (
    <div className={styles.xhsResultCard}>
      <p>{mode}简报已根据已保存的真实热点数据整理完成。</p>
      <p className={styles.xhsResultMeta}>
        数据覆盖：最近请求 {requestedDays} 天，已获得 {availableDays} 天有效快照。
      </p>
      {filePath && (
        <a
          className={styles.xhsDownloadLink}
          href={toDisplayUrl(filePath)}
          download={fileName}
        >
          <DownloadOutlined /> 下载 Excel 简报
        </a>
      )}
    </div>
  );
}

function emailBody(result: ToolResult): React.ReactNode {
  const recipient = typeof result.recipient === "string" ? result.recipient : "指定邮箱";
  const hasExcelAttachment = typeof result.attachment_file_name === "string";
  return (
    <div className={styles.xhsResultCard}>
      <p>简报已发送至 {recipient}。</p>
      <p className={styles.xhsResultMeta}>
        {hasExcelAttachment ? "邮件已附带 Excel 简报。" : "邮件正文使用已确认的简报内容。"}
      </p>
    </div>
  );
}

function collectionBody(result: ToolResult): React.ReactNode {
  const topItems = Array.isArray(result.top_items) ? result.top_items : [];
  const count = topItems.length || Number(result.item_count || 0);
  return (
    <div className={styles.xhsResultCard}>
      <p>已保存 {count} 条小红书热点数据，可用于生成后续简报。</p>
      <p className={styles.xhsResultMeta}>数据已保留来源与快照，便于后续人工核验。</p>
    </div>
  );
}

const XhsHotspotToolCard: React.FC<XhsHotspotToolCardProps> = ({
  content,
  isStreaming,
}) => {
  const result = parseToolResult(content.result);
  const isReport = new Set([
    "create_xhs_briefing",
    "generate_xhs_personalized_report",
  ]).has(content.name);
  const isEmail = content.name === "send_xhs_personalized_report_email";
  const isCollection = content.name === "collect_xhs_hotspots";
  const safeContent =
    content.status === "error"
      ? { ...content, params: {}, result: readableError(result) }
      : content;
  const icon = isEmail ? <MailOutlined /> : isReport ? <FileTextOutlined /> : isCollection ? <SyncOutlined /> : <CheckCircleOutlined />;
  const body = isReport
    ? reportBody(result)
    : isEmail
      ? emailBody(result)
      : isCollection
        ? collectionBody(result)
        : <div className={styles.xhsResultCard}><p>操作已完成。</p></div>;

  return (
    <ToolCardShell
      content={safeContent}
      isStreaming={isStreaming}
      icon={icon}
      title={toolTitle(content.name, content.status === "done")}
      defaultOpen={content.status === "done" && (isReport || isEmail)}
    >
      {content.status === "error" ? undefined : body}
    </ToolCardShell>
  );
};

export default XhsHotspotToolCard;
