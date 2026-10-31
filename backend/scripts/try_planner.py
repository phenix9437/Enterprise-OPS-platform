"""Try the planner against the REAL OpenAI API without starting the server or database.

    cd backend
    export OPENAI_API_KEY=sk-...
    python -m scripts.try_planner "Send payment reminders to customers with invoices overdue by 30+ days"
"""
import asyncio
import sys

from app.core.config import get_settings
from app.llm.client import OpenAIClient
from app.services.planner import create_plan, render_reply


async def main(instruction: str) -> None:
    s = get_settings()
    if not s.openai_api_key:
        sys.exit("Set OPENAI_API_KEY first")
    llm = OpenAIClient(s.openai_api_key, s.openai_model, s.llm_timeout_seconds, s.llm_sdk_max_retries)
    out = await create_plan(llm, instruction, history=[])
    print(render_reply(out.plan))
    print(f"\nmodel={out.model} calls={out.calls} tokens={out.prompt_tokens}+{out.completion_tokens} "
          f"latency={out.latency_ms}ms")
    print(out.plan.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "Summarise this month's overdue invoices"))
