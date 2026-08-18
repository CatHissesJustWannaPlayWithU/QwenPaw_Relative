#!/usr/bin/env python3
"""交互式写入小红书简报 SMTP 加密凭据，不接受命令行密码参数。"""

from __future__ import annotations

import argparse
import asyncio
import getpass

from qwenpaw.config.config import BuiltinToolConfig, load_agent_config, save_agent_config
from qwenpaw.constant import WORKING_DIR
from qwenpaw.drivers.credentials.store import AsyncCredentialStore
from qwenpaw.drivers.credentials.types import CredentialRecord


EMAIL_TOOL = "send_xhs_personalized_report_email"


def _parser() -> argparse.ArgumentParser:
    """创建 SMTP 配置命令的参数解析器，并强制用户选择一种加密传输方式。"""
    parser = argparse.ArgumentParser(
        description="为小红书个性化简报配置加密 SMTP 凭据。"
    )
    parser.add_argument("--agent-id", default="xhs_hotspot_collector")
    parser.add_argument("--credential-ref", default="xhs_report_smtp")
    parser.add_argument("--host", required=True, help="例如 smtp.qq.com")
    parser.add_argument("--port", type=int, default=465)
    parser.add_argument("--from-email", required=True)
    parser.add_argument("--from-name", default="小红书热点简报")
    parser.add_argument("--username", default="", help="留空时交互输入")
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--ssl", action="store_true", help="使用 SMTP SSL")
    transport.add_argument("--starttls", action="store_true", help="使用 SMTP STARTTLS")
    return parser


async def _configure(args: argparse.Namespace) -> None:
    """加密保存 SMTP 凭据，并把凭据引用写入指定智能体的邮件工具配置。"""
    username = args.username.strip() or input("SMTP 用户名：").strip()
    password = getpass.getpass("SMTP 密码或授权码（不会显示或写入终端历史）：")
    if not username or not password:
        raise ValueError("SMTP 用户名和密码或授权码不能为空")
    if not 1 <= args.port <= 65535:
        raise ValueError("SMTP 端口必须在 1 到 65535 之间")

    workspace_dir = WORKING_DIR / "workspaces" / args.agent_id
    store = AsyncCredentialStore(workspace_dir / "credentials.yaml")
    await store.put(
        CredentialRecord(
            ref=args.credential_ref,
            kind="smtp",
            public={
                "host": args.host.strip(),
                "port": args.port,
                "from_email": args.from_email.strip(),
                "from_name": args.from_name.strip(),
                "use_ssl": bool(args.ssl),
                "use_starttls": bool(args.starttls),
            },
            secrets={"username": username, "password": password},
            meta={"purpose": "xhs_personalized_report_email"},
        )
    )
    agent_config = load_agent_config(args.agent_id)
    if agent_config.tools is None:
        raise RuntimeError("该智能体没有工具配置，无法写入 SMTP 凭据引用")
    tool_config = agent_config.tools.builtin_tools.get(EMAIL_TOOL)
    if tool_config is None:
        tool_config = BuiltinToolConfig(
            name=EMAIL_TOOL,
            enabled=True,
            description="人工确认后发送小红书个性化热点简报。",
            icon="✉️",
        )
        agent_config.tools.builtin_tools[EMAIL_TOOL] = tool_config
    tool_config.enabled = True
    tool_config.config = {"smtp_credential_ref": args.credential_ref}
    save_agent_config(args.agent_id, agent_config)
    print(f"已保存加密 SMTP 凭据引用：{args.credential_ref}")
    print(f"凭据文件：{workspace_dir / 'credentials.yaml'}")
    print("密码和授权码不会显示在本脚本输出、agent.json 或报告文件中。")


def main() -> None:
    """解析终端参数并运行异步 SMTP 配置流程，作为脚本的命令行入口。"""
    args = _parser().parse_args()
    asyncio.run(_configure(args))


if __name__ == "__main__":
    main()
