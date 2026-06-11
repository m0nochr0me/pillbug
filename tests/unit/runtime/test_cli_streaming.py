"""CLI channel stream_response: incremental printing with a single prefix and trailing newline."""

from __future__ import annotations

import pytest

from app.runtime.channels import CliChannel
from app.schema.messages import InboundMessage


def _inbound() -> InboundMessage:
    return InboundMessage(
        channel_name="cli",
        conversation_id="default",
        user_id="local-user",
        text="hi",
    )


async def test_stream_prints_prefix_once_and_trailing_newline(capsys):
    channel = CliChannel()

    async with channel.stream_response(_inbound()) as emit:
        await emit("Hel")
        await emit("")
        await emit("lo")

    assert capsys.readouterr().out == "pillbug> Hello\n"


async def test_stream_without_output_prints_nothing(capsys):
    channel = CliChannel()

    async with channel.stream_response(_inbound()):
        pass

    assert capsys.readouterr().out == ""


async def test_stream_exception_still_terminates_partial_line(capsys):
    channel = CliChannel()

    with pytest.raises(RuntimeError, match="turn failed"):
        async with channel.stream_response(_inbound()) as emit:
            await emit("partial")
            raise RuntimeError("turn failed")

    # The partial output is closed with a newline so the next prompt starts clean.
    assert capsys.readouterr().out == "pillbug> partial\n"
