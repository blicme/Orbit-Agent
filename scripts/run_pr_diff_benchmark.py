"""Run EvoAgent reviewers directly against a labelled PR-diff JSONL file.

This is the fast local/benchmark entry point. It intentionally does not enforce
the production dataset gates in ``validate_real_dataset`` (300+ public PRs,
train/validation/holdout repository isolation, ``should_comment`` labels), so
synthetic datasets such as ``pr_diff_100.jsonl`` can be exercised immediately.
Scoring still uses the standard evaluation harness matching and metrics.
"""
import argparse
import json
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from evoagent.config import Settings  # noqa: E402
from evoagent.evaluation_benchmark import ContextRuleReviewer  # noqa: E402
from evoagent.evaluation_harness import (  # noqa: E402
    FixtureRepairer,
    load_jsonl,
)
from evoagent.evaluation_v2 import (  # noqa: E402
    ProductArmReviewer,
    ProductionEvaluationHarness,
)
from evoagent.llm import JsonChatClient  # noqa: E402
from evoagent.reviewer import (  # noqa: E402
    CompositeReviewer,
    LocalRuleReviewer,
    OpenAICompatibleReviewer,
)


def _resolved_llm(mode: str) -> dict:
    resolved = Settings.from_env().resolved_llm()
    if not resolved:
        raise SystemExit(
            "%s mode requires EVOAGENT_LLM_PROVIDER and its API credentials in .env"
            % mode
        )
    return resolved


def _local_reviewer():
    return CompositeReviewer([LocalRuleReviewer(), ContextRuleReviewer()])


def _openai_reviewer(timeout: int, retries: int):
    config = _resolved_llm("openai")
    return OpenAICompatibleReviewer(
        str(config["base_url"]),
        str(config["api_key"]),
        str(config["model"]),
        timeout=timeout,
        provider=str(config["provider"]),
        extra_headers=dict(config.get("headers") or {}),
        retries=retries,
    )


def _agentic_reviewer(
    token_budget: int, time_budget: int, timeout: int, retries: int,
) -> ProductArmReviewer:
    config = _resolved_llm("agentic")
    client = JsonChatClient(
        str(config["base_url"]),
        str(config["api_key"]),
        str(config["model"]),
        provider=str(config["provider"]),
        timeout=timeout,
        extra_headers=dict(config.get("headers") or {}),
        retries=retries,
    )
    return ProductArmReviewer(
        "full-agentic", client, token_budget, time_budget,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="Labelled PR diff JSONL, e.g. pr_diff_100.jsonl")
    parser.add_argument(
        "--reviewer", choices=("local", "openai", "agentic"), default="local",
        help="local is deterministic and requires no LLM; openai is one-pass LLM; "
        "agentic is the product full-agentic topology.",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Run only the first N cases; 0 means all cases.",
    )
    parser.add_argument("--line-tolerance", type=int, default=2)
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--token-budget", type=int, default=48000)
    parser.add_argument("--time-budget", type=int, default=900)
    parser.add_argument(
        "--output",
        default=os.path.join(ROOT, "output", "pr_diff_benchmark", "evaluation.json"),
    )
    args = parser.parse_args()

    cases = load_jsonl(args.dataset)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("dataset contains no cases to evaluate")

    if args.reviewer == "local":
        reviewer = _local_reviewer()
    elif args.reviewer == "openai":
        reviewer = _openai_reviewer(args.timeout, args.retries)
    else:
        reviewer = _agentic_reviewer(
            args.token_budget, args.time_budget, args.timeout, args.retries,
        )

    repairer = None if args.no_repair else FixtureRepairer()
    report = ProductionEvaluationHarness(
        line_tolerance=args.line_tolerance,
        repairer=repairer,
    ).run(reviewer, cases, reviewer.name)

    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    metrics = report["metrics"]
    print("reviewer=%s" % reviewer.name)
    print("cases=%d" % len(cases))
    print("precision=%s" % metrics["precision"])
    print("recall=%s" % metrics["recall"])
    print("f1=%s" % metrics["f1"])
    print("execution_success_rate=%s" % metrics["execution_success_rate"])
    print("safe_fix_rate=%s" % metrics["safe_fix_rate"])
    print("e2e_security_fix_rate=%s" % metrics["e2e_security_fix_rate"])
    print("report=%s" % output)


if __name__ == "__main__":
    main()
