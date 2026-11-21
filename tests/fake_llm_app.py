"""Run the real FastAPI app with a deterministic fake LLM for HTTP tests only."""
import time

from app import main, product_api
from app.llm import Action, Plan


class FakeLLM:
    def plan(self, text, *args):
        time.sleep(0.1)
        actions = [Action(device_id="light", values={"brightness": 30})]
        if "частично" in text:
            actions.append(Action(device_id="curtains", values={"position": 0}))
        return Plan(outcome="done", reason="", actions=actions)


product_api.LLMClient = FakeLLM
app = main.app
