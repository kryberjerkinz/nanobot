"""User-only approval commands for the homelab-tools approval gate.

/pending, /approve <id>, /deny <id> are handled here, by code, before any model sees the message. A model can
queue an action (via a tool) but cannot approve it, because there is no tool for approving.
Requires the `homelab-tools` package in the same environment; otherwise the commands explain that.
"""

from __future__ import annotations

import asyncio

from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter


def _reply(ctx: CommandContext, text: str) -> OutboundMessage:
    return OutboundMessage(
        channel=ctx.msg.channel,
        chat_id=ctx.msg.chat_id,
        content=text,
        metadata={**dict(ctx.msg.metadata or {}), "render_as": "text"},
    )


def _approvals():
    try:
        from homelab_tools.common import approvals
    except ImportError:
        return None
    return approvals


async def cmd_pending(ctx: CommandContext) -> OutboundMessage:
    a = _approvals()
    if a is None:
        return _reply(ctx, "homelab-tools is not installed here, so there is nothing to approve.")
    return _reply(ctx, await asyncio.to_thread(a.render_pending))


async def cmd_approve(ctx: CommandContext) -> OutboundMessage:
    a = _approvals()
    if a is None:
        return _reply(ctx, "homelab-tools is not installed here, so there is nothing to approve.")
    approval_id = ctx.args.strip()
    if not approval_id:
        return _reply(ctx, "Usage: /approve <id>   (see /pending)")
    approver = f"{ctx.msg.channel}:{getattr(ctx.msg, 'sender_id', '') or ctx.msg.chat_id}"
    return _reply(ctx, await asyncio.to_thread(a.approve, approval_id, approver))


async def cmd_deny(ctx: CommandContext) -> OutboundMessage:
    a = _approvals()
    if a is None:
        return _reply(ctx, "homelab-tools is not installed here, so there is nothing to deny.")
    approval_id = ctx.args.strip()
    if not approval_id:
        return _reply(ctx, "Usage: /deny <id>   (see /pending)")
    approver = f"{ctx.msg.channel}:{getattr(ctx.msg, 'sender_id', '') or ctx.msg.chat_id}"
    return _reply(ctx, await asyncio.to_thread(a.deny, approval_id, approver))


def register_approval_commands(router: CommandRouter) -> None:
    router.exact("/pending", cmd_pending)
    router.exact("/approve", cmd_approve)
    router.prefix("/approve ", cmd_approve)
    router.exact("/deny", cmd_deny)
    router.prefix("/deny ", cmd_deny)
