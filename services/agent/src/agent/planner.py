"""Planners turn (question, retrieved catalog terms) into a candidate aggregation pipeline.

Two implementations, same interface:
  AdkPlanner     - Google ADK LlmAgent (Gemini by default, Claude if AGENT_MODEL=claude-*).
  CatalogPlanner - deterministic: top retrieved term + keyword slot filling. No LLM, no key.
                   It's the offline baseline for the eval and the fallback when no key is set.
Either way the output is UNTRUSTED and goes through guard.validate() before execution.
"""

import json
import re
import uuid
from typing import Any, Protocol, get_args

from bson import json_util
from ledgerlens_common.models import Category, Channel, Currency
from ledgerlens_semantic.catalog import Term
from pydantic import BaseModel, Field


class Plan(BaseModel):
    collection: str
    pipeline: Any  # validated later by guard.validate, deliberately not trusted here
    terms_used: list[str]
    planner: str


class Planner(Protocol):
    async def plan(self, question: str, terms: list[Term], role: str) -> Plan: ...


# --- deterministic baseline -------------------------------------------------------------

COUNTRY_NAMES = {"united states": "US", "usa": "US", "uk": "GB", "united kingdom": "GB", "britain": "GB",
                 "germany": "DE", "france": "FR", "india": "IN", "japan": "JP", "singapore": "SG",
                 "australia": "AU", "canada": "CA"}  # fmt: skip
CHANNEL_WORDS = {"card present": "card_present", "in-store": "card_present", "in store": "card_present",
                 "online": "online", "mobile": "mobile", "atm": "atm"}  # fmt: skip


def slot_filters(question: str) -> dict[str, Any]:
    """Extract simple equality filters (category, channel, currency, country) from the question."""
    q = question.lower()
    found: dict[str, Any] = {}
    for cat in get_args(Category):
        if re.search(rf"\b{cat.replace('_', '[ _]')}\b", q):
            found["category"] = cat
    for word, channel in CHANNEL_WORDS.items():
        if re.search(rf"\b{word}\b", q):
            found["channel"] = channel
    for cur in get_args(Currency):
        if re.search(rf"\b{cur.lower()}\b", q):
            found["currency"] = cur
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf"\b(in|from) {name}\b", q):
            found["country"] = code
    return found


class CatalogPlanner:
    name = "catalog"

    async def plan(self, question: str, terms: list[Term], role: str) -> Plan:
        term = terms[0]  # highest similarity
        # Slot filters only make sense on the raw transactions collection.
        match = dict(term.filter or {})
        if term.source == "transactions":
            match |= slot_filters(question)
        stages: list[dict[str, Any]] = [{"$match": match}] if match else []
        stages += term.pipeline or [{"$count": "count"}]
        return Plan(collection=term.source, pipeline=stages, terms_used=[term.name], planner=self.name)


# --- Google ADK LLM planner -------------------------------------------------------------

SCHEMA_HINT = """Collection `transactions` documents:
  _id (txn id), customer_id (PII), merchant, category, amount (Decimal128), currency (ISO 4217),
  country (ISO alpha-2), channel (card_present|online|mobile|atm), timestamp (UTC datetime),
  risk: {score (0-100), flagged (bool), reasons: [{code: VELOCITY|AMOUNT_ZSCORE|COUNTRY_JUMP, detail}]}
  (risk is missing if the txn was not scored yet: treat as not flagged).
Categories: """ + ", ".join(get_args(Category)) + """
Never add or compare amounts across different currencies; group by currency instead."""

INSTRUCTION = """You translate a bank analyst's question into ONE read-only MongoDB aggregation pipeline.
Use ONLY the business-term definitions provided (cite the ones you use by name in terms_used).
Allowed stages: $match $group $project $sort $limit $skip $count $unwind $addFields $set $unset
$bucket $sortByCount $facet $replaceRoot. Never use $out, $merge, $function, $where, $accumulator.
Return pipeline_json as a JSON array string. Dates must be ISO strings inside {"$date": "..."}."""


class LlmPlan(BaseModel):
    collection: str = Field(description="collection to aggregate on")
    pipeline_json: str = Field(description="the aggregation pipeline as a JSON array string")
    terms_used: list[str] = Field(description="names of the catalog terms used")


def build_model(model: str) -> Any:
    """Gemini via model string (GOOGLE_API_KEY); Claude via ADK's Anthropic integration (ANTHROPIC_API_KEY)."""
    if model.startswith("claude"):
        from google.adk.models.anthropic_llm import AnthropicLlm

        return AnthropicLlm(model=model)
    return model


class AdkPlanner:
    name = "adk"

    def __init__(self, model: str) -> None:
        from google.adk.agents import LlmAgent
        from google.adk.runners import InMemoryRunner
        from google.genai import types

        self._types = types
        self.agent = LlmAgent(
            name="ledgerlens_query_planner",
            model=build_model(model),
            instruction=INSTRUCTION,
            output_schema=LlmPlan,
            generate_content_config=types.GenerateContentConfig(temperature=0),
        )
        self.runner = InMemoryRunner(agent=self.agent, app_name="ledgerlens-agent")

    async def plan(self, question: str, terms: list[Term], role: str) -> Plan:
        catalog = [t.model_dump(include={"name", "description", "source", "formula", "filter", "pipeline",
                                         "sensitivity"}) for t in terms]  # fmt: skip
        prompt = (f"{SCHEMA_HINT}\n\nBusiness terms:\n{json.dumps(catalog, indent=1)}\n\n"
                  f"Analyst role: {role}\nQuestion: {question}")  # fmt: skip
        session_id = uuid.uuid4().hex  # one throwaway session per question: no cross-user memory
        await self.runner.session_service.create_session(
            app_name="ledgerlens-agent", user_id="planner", session_id=session_id
        )
        text = ""
        message = self._types.Content(role="user", parts=[self._types.Part(text=prompt)])
        async for event in self.runner.run_async(user_id="planner", session_id=session_id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                text = "".join(p.text or "" for p in event.content.parts)
        out = LlmPlan.model_validate_json(text)
        try:
            pipeline = json_util.loads(out.pipeline_json)  # turns {"$date": ...} into real datetimes
        except (ValueError, TypeError):
            pipeline = out.pipeline_json  # guard rejects non-lists with a clear reason
        return Plan(collection=out.collection, pipeline=pipeline, terms_used=out.terms_used, planner=self.name)
