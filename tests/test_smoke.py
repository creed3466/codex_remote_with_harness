"""Day-1 smoke test: protocol codegen produced the V2 types we'll consume."""

from __future__ import annotations

import importlib

import pytest


def test_package_imports() -> None:
    mod = importlib.import_module("codex_rc")
    assert mod.__version__


def test_protocol_module_present() -> None:
    importlib.import_module("codex_rc.protocol.v2")


@pytest.mark.parametrize(
    "symbol",
    [
        # streaming output
        "AgentMessageDeltaNotification",
        "ReasoningTextDeltaNotification",
        "TurnDiffUpdatedNotification",
        "CommandExecOutputDeltaNotification",
        # approval flow (V2 Guardian model)
        "GuardianApprovalReview",
        "GuardianApprovalReviewStatus",
        "ItemGuardianApprovalReviewStartedNotification",
        "ItemGuardianApprovalReviewCompletedNotification",
        "AskForApproval",
        # exec
        "CommandExecParams",
        # lifecycle
        "ContextCompactedNotification",
        "ErrorNotification",
        "ThreadStatusChangedNotification",
        # remote control
        "RemoteControlStatusChangedNotification",
    ],
)
def test_v2_symbol_present(symbol: str) -> None:
    v2 = importlib.import_module("codex_rc.protocol.v2")
    assert hasattr(v2, symbol), f"V2 protocol missing {symbol!r}"
