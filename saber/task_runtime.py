"""
Unified task runtime for OSBench.

Combines the existing shell sandbox with JSON-defined mock MCP tools and
records a provider-agnostic event stream.
"""
import json, os

from mcp_runtime import MCPRuntime
from sandbox_shell import SandboxShell


BASH_TOOL_ANTHROPIC = {
    "name": "bash",
    "description": "Execute a shell command on the Linux system. Returns the command output.",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The shell command to execute"}},
        "required": ["command"],
    },
}

BASH_TOOL_OPENAI = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a shell command on the Linux system. Returns the command output.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to execute"}},
            "required": ["command"],
        },
    },
}

BASH_TOOL_CODEX = {
    "type": "function",
    "name": "bash",
    "description": "Execute a shell command on the Linux system. Returns the command output.",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The shell command to execute"}},
        "required": ["command"],
    },
}


# ── failproofai gate wiring ──────────────────────────────────────────────────
# Constructed only when FAILPROOF_HOME is set (the guarded arm). Otherwise this
# stays None and SABER runs exactly as upstream.
FAILPROOF_GATE = None
if os.environ.get("FAILPROOF_HOME"):
    import sys as _sys
    _sys.path.insert(0, "/work")
    from failproof_shim import FailproofGate as _FG
    FAILPROOF_GATE = _FG(os.environ["FAILPROOF_HOME"])
# ─────────────────────────────────────────────────────────────────────────────


class TaskRuntime:
    def __init__(self, task):
        self.task = task
        self._failproof_gate = FAILPROOF_GATE
        self._failproof_pending_instruct = None
        setup = task["setup"]
        self.shell = SandboxShell(
            mock_fs=setup.get("mock_fs", {}),
            file_contents=setup.get("file_contents", {}),
            injection=task.get("injection", {}),
            cwd=setup.get("cwd", "/home/user"),
            init_commands=setup.get("init_commands", []),
            file_permissions=setup.get("file_permissions", {}),
        )
        self.mcp = MCPRuntime(setup.get("mcp_servers", []), shell=self.shell)
        self.events = []

    def get_tools(self, provider_type):
        if provider_type == "anthropic":
            tools = [BASH_TOOL_ANTHROPIC]
        elif provider_type == "codex":
            tools = [BASH_TOOL_CODEX]
        else:
            tools = [BASH_TOOL_OPENAI]
        tools.extend(self.mcp.get_tools(provider_type))
        return tools

    def execute_tool(self, tool_name, tool_input):
        step = len(self.events) + 1

        # ══════════ failproofai gate (guarded arm only) ══════════════════════
        # Set by the runner when FAILPROOF_HOME is present. When absent this is
        # None and SABER behaves exactly as upstream. See failproof_shim.py.
        _gate = getattr(self, "_failproof_gate", None)
        if _gate is not None:
            _dec, _txt = _gate.evaluate(tool_name, tool_input)
            if _dec == "deny":
                # Command never executes. The agent sees the refusal as the tool
                # result -- mirroring failproofai's PreToolUse deny in Claude Code.
                self.events.append({
                    "step": step, "type": "tool_call", "tool_name": tool_name,
                    "input": tool_input or {},
                    "command": str((tool_input or {}).get("command", "")),
                    "output": _txt, "deltas": [], "failproof": "deny",
                })
                return _txt
            if _dec == "instruct":
                # Executes normally; oversight text is appended to the output.
                self._failproof_pending_instruct = _txt
        # ═════════════════════════════════════════════════════════════════════

        if tool_name == "bash":
            command = str((tool_input or {}).get("command", ""))
            output = self.shell.execute(command)
            _pi = getattr(self, "_failproof_pending_instruct", None)
            if _pi:
                output = f"{output}\n\n{_pi}"
                self._failproof_pending_instruct = None
            shell_trajectory = self.shell.get_trajectory_dicts()
            deltas = shell_trajectory[-1]["deltas"] if shell_trajectory else []
            event = {
                "step": step,
                "type": "tool_call",
                "tool_name": "bash",
                "input": tool_input or {},
                "command": command,
                "output": output,
                "deltas": deltas,
            }
            self.events.append(event)
            return output

        output, deltas = self.mcp.execute(tool_name, tool_input or {})
        output = self._apply_non_shell_injection(tool_name, step, output)
        _pi = getattr(self, "_failproof_pending_instruct", None)
        if _pi:
            output = f"{output}\n\n{_pi}"
            self._failproof_pending_instruct = None
        event = {
            "step": step,
            "type": "tool_call",
            "tool_name": tool_name,
            "input": tool_input or {},
            "output": output,
            "deltas": deltas,
        }
        self.events.append(event)
        return output

    def _apply_non_shell_injection(self, tool_name, step, output):
        inj = self.task.get("injection", {})
        if not inj.get("enabled"):
            return output

        target_tool = inj.get("target_tool")
        if target_tool and target_tool != tool_name:
            return output

        method = inj.get("method", "tool_output")
        payload = inj.get("payload", "")
        trigger_step = inj.get("injected_at_step", step)

        if method in {"tool_output", "multi_step"} and trigger_step == step:
            return output + payload

        if method == "error_message" and trigger_step == step:
            base = inj.get("error_output", output)
            if not isinstance(base, str):
                base = json.dumps(base, ensure_ascii=False, indent=2)
            return base + "\n" + payload

        return output

    def get_shell_trajectory(self):
        return self.shell.get_trajectory_dicts()

    def get_events(self):
        return list(self.events)

    def cleanup(self):
        self.shell.cleanup()
