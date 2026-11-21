import json
import os
import tempfile
import unittest

from evoagent.agentic_core import BoundedRole, ModeRouterReviewer
from evoagent.diff_parser import parse_unified_diff
from evoagent.llm import decode_json_object
from evoagent.runtime import AgentTool, ToolRegistry
from evoagent.store import TaskStore
from evoagent.telemetry import ExecutionLedger


DIFF = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+eval(user_input)\n"


class JsonExtractionTests(unittest.TestCase):
    def test_accepts_extra_data_after_the_first_object(self):
        self.assertEqual(
            {"action": "final"},
            decode_json_object('{"action":"final"}\n{"extra":1}'),
        )

    def test_repairs_truncated_object(self):
        self.assertEqual(
            {"findings": [{"path": "a.py", "line": 1}]},
            decode_json_object('{"findings":[{"path":"a.py","line":1}'),
        )

    def test_strips_markdown_fence_and_prose(self):
        self.assertEqual(
            {"action": "final", "findings": []},
            decode_json_object('Here:\n```json\n{"action":"final","findings":[]}\n```'),
        )


class _BudgetClient:
    provider = "fake"
    model = "fake"

    def complete_json(self, role, _system, user, ledger=None, max_tokens=None):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        if ledger:
            ledger.record_model(
                role, self.provider, self.model,
                {"prompt_tokens": 4000, "completion_tokens": 50}, 1,
            )
        if "final_instruction" in task:
            return {"action": "final", "findings": []}
        return {"action": "tool", "tool": "ping", "arguments": {}}


class BoundedRoleTests(unittest.TestCase):
    def test_budget_exhaustion_finishes_instead_of_raising(self):
        client = _BudgetClient()
        registry = ToolRegistry([
            AgentTool("ping", "ping", {"type": "object", "properties": {}}, lambda: {"ok": True}),
        ])
        ledger = ExecutionLedger("agentic")
        role = BoundedRole(
            "lead", "system", client, token_budget=5000,
            time_budget=60, max_steps=4,
        )

        result = role.run(json.dumps({"phase": "delegate"}), registry, ledger)

        self.assertTrue(result.get("_wrapped_up"))
        events = {item["event"] for item in ledger.agent_traces["lead"]}
        self.assertIn("wrap_up_started", events)
        self.assertIn("tool_observation", events)

    def test_payload_without_action_field_is_treated_as_final(self):
        class _PayloadClient:
            provider = "fake"
            model = "fake"

            def complete_json(self, role, _system, _user, ledger=None, max_tokens=None):
                if ledger:
                    ledger.record_model(role, "fake", "fake", {}, 1)
                return {"findings": []}

        ledger = ExecutionLedger("agentic")
        role = BoundedRole("lead", "system", _PayloadClient(), 8000, 60)
        result = role.run(json.dumps({"phase": "delegate"}), ToolRegistry(), ledger)
        self.assertEqual([], result["findings"])

    def test_phase_payload_wins_over_an_unrelated_type_field(self):
        class _PayloadClient:
            provider = "fake"
            model = "fake"

            def complete_json(self, role, _system, _user, ledger=None, max_tokens=None):
                if ledger:
                    ledger.record_model(role, "fake", "fake", {}, 1)
                return {
                    "type": "answer",
                    "accepted_finding_indices": [], "confidence_adjustments": [],
                    "resolution_summary": "done",
                }

        ledger = ExecutionLedger("agentic")
        role = BoundedRole("lead", "system", _PayloadClient(), 8000, 60)
        result = role.run(json.dumps({"phase": "finalize"}), ToolRegistry(), ledger)
        self.assertEqual([], result["accepted_finding_indices"])


class _SinglePassClient:
    provider = "fake"
    model = "fake"

    def __init__(self):
        self.calls = []

    def complete_json(self, role, _system, user, ledger=None, max_tokens=None):
        self.calls.append(role)
        if ledger:
            ledger.record_model(role, self.provider, self.model, {}, 1)
        return {
            "action": "final",
            "findings": [],
        }


class SinglePassTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = TaskStore(self.path)
        self.store.create("task", "org/repo", 1, {
            "mode": "agentic",
            "enabled_agents": ["lead", "security", "correctness-reliability", "critic"],
        })

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_small_diff_uses_one_model_call(self):
        client = _SinglePassClient()
        reviewer = ModeRouterReviewer(self.store, client, review_effort="auto")

        findings = reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )
        summary = reviewer.collaboration_summary("task")

        self.assertEqual(1, len(client.calls))
        self.assertEqual("single-pass", summary["collaboration"]["protocol"])
        self.assertEqual(1, summary["execution"]["llm_calls"])
        self.assertIn("SEC-EVAL", {item.rule_id for item in findings})

    def test_model_failure_falls_back_to_rule_findings(self):
        class _FailingClient:
            provider = "fake"
            model = "fake"

            def complete_json(self, *_args, **_kwargs):
                raise RuntimeError("model unavailable")

        reviewer = ModeRouterReviewer(self.store, _FailingClient(), review_effort="auto")
        findings = reviewer.review_with_context(
            "task", DIFF, parse_unified_diff(DIFF), "org/repo"
        )
        self.assertIn("SEC-EVAL", {item.rule_id for item in findings})


if __name__ == "__main__":
    unittest.main()