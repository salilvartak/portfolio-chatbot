"""LangGraph agent.

    START -> retrieve -> generate -> (tool call?) -> tools -> generate -> END

`retrieve` pulls relevant parts of Salil's content. `generate` answers from them and, when the visitor
wants a call, can call the read-only `find_open_slots` tool. The slots it finds are sent to the widget
as buttons; booking itself happens through POST /book, never through the model.
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from app.prompts import SCHEDULING_OFF, SCHEDULING_ON, SYSTEM_PROMPT, format_context
from app.store import Chunk

MAX_TOOL_ROUNDS = 2


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    chunks: list[Chunk]


Retriever = Callable[[str], list[Chunk]]
# (from_day, to_day, part_of_day) -> list of {"start": iso, "label": str}
SlotFinder = Callable[[date | None, date | None, str], list[dict]]


def _retrieval_query(messages: list[AnyMessage]) -> str:
    # Include the previous user turn so follow-ups like "what stack did it use?" still retrieve well.
    user_turns = [m.content for m in messages if isinstance(m, HumanMessage)]
    return "\n".join(str(t) for t in user_turns[-2:])


def make_slot_tool(finder: SlotFinder):
    @tool(response_format="content_and_artifact")
    def find_open_slots(from_date: str = "", to_date: str = "", time_of_day: str = "any"):
        """Find open times for a call with Salil. Read-only: it does not book anything.

        Args:
            from_date: first day to search, YYYY-MM-DD (optional).
            to_date: last day to search, YYYY-MM-DD (optional).
            time_of_day: one of any, morning, afternoon, evening, night.
        """
        try:
            lo = date.fromisoformat(from_date) if from_date else None
            hi = date.fromisoformat(to_date) if to_date else (lo if lo else None)
        except ValueError:
            return "Invalid date. Use YYYY-MM-DD.", []
        part = time_of_day if time_of_day in ("morning", "afternoon", "evening", "night") else "any"
        try:
            slots = finder(lo, hi, part)
        except Exception:
            return "The calendar couldn't be checked right now.", []
        if not slots:
            return "No open times in that range.", []
        return "Open times (shown to the visitor as buttons): " + "; ".join(s["label"] for s in slots), slots

    return find_open_slots


def build_graph(
    llm: Runnable | Callable[[list], Runnable],
    retriever: Retriever,
    owner: str,
    site: str = "",
    contact: str = "",
    slot_finder: SlotFinder | None = None,
    scheduling_info: Callable[[], dict] | None = None,
):
    """llm: a chat model, or a function that binds the given tools and returns one."""
    contact = contact or "the contact links on the website"
    tools = [make_slot_tool(slot_finder)] if slot_finder else []
    model = llm(tools) if tools and not isinstance(llm, Runnable) else llm

    def retrieve(state: State) -> dict:
        return {"chunks": retriever(_retrieval_query(state["messages"]))}

    def generate(state: State) -> dict:
        if tools:
            scheduling = SCHEDULING_ON.format(owner=owner, contact=contact, **(scheduling_info() if scheduling_info else {}))
        else:
            scheduling = SCHEDULING_OFF.format(contact=contact)
        system = SystemMessage(
            SYSTEM_PROMPT.format(
                owner=owner,
                site=site or "this site",
                contact=contact,
                scheduling=scheduling,
                context=format_context(state["chunks"]),
            )
        )
        return {"messages": [model.invoke([system, *state["messages"]])]}

    def after_generate(state: State) -> str:
        last = state["messages"][-1]
        rounds = sum(isinstance(m, ToolMessage) for m in state["messages"])
        if tools and isinstance(last, AIMessage) and last.tool_calls and rounds < MAX_TOOL_ROUNDS:
            return "tools"
        return END

    g = StateGraph(State)
    g.add_node("retrieve", retrieve)
    g.add_node("generate", generate)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "generate")
    if tools:
        g.add_node("tools", ToolNode(tools))
        g.add_conditional_edges("generate", after_generate, ["tools", END])
        g.add_edge("tools", "generate")
    else:
        g.add_edge("generate", END)
    return g.compile()


def scheduling_info_from(settings, rules) -> Callable[[], dict]:
    def info() -> dict:
        now = datetime.now(timezone.utc).astimezone(rules.tz)
        days = ", ".join(
            (now.date() + timedelta(days=i)).strftime("%a %Y-%m-%d") for i in range(rules.days_ahead + 1)
        )
        return {
            "now": now.strftime("%A %Y-%m-%d %H:%M ") + (now.tzname() or ""),
            "tz": settings.owner_timezone,
            "minutes": rules.slot_minutes,
            "hours": settings.booking_hours,
            "days": days,
        }

    return info


def default_graph():
    from langchain_google_genai import ChatGoogleGenerativeAI

    from app.booking import find_slots, fmt, rules_from
    from app.calendar_api import get_calendar
    from app.config import get_settings
    from app.embeddings import embed_query
    from app.store import search

    s = get_settings()

    def make(model: str) -> ChatGoogleGenerativeAI:
        return ChatGoogleGenerativeAI(
            model=model,
            google_api_key=s.google_api_key,
            max_output_tokens=2048,
            reasoning_effort=s.thinking_level or None,
            # Fail fast so a fallback model gets a turn: the defaults (6 retries, no timeout)
            # can hold a visitor's request open for minutes.
            max_retries=1,
            timeout=30,
        )

    names = list(dict.fromkeys([s.chat_model, *(m.strip() for m in s.fallback_models.split(",") if m.strip())]))
    models = [make(n) for n in names]

    def bound(tools: list) -> Runnable:
        return with_fallbacks([m.bind_tools(tools) for m in models])

    cal = get_calendar()
    finder = info = None
    if cal is not None:
        rules = rules_from(s)
        info = scheduling_info_from(s, rules)

        def finder(lo, hi, part):
            now = datetime.now(timezone.utc)
            return [
                {"start": t.astimezone(timezone.utc).isoformat(), "label": fmt(t, rules.tz)}
                for t in find_slots(cal, rules, now, lo, hi, part)
            ]

    return build_graph(
        bound if finder else with_fallbacks(models),
        lambda q: search(embed_query(q), s.top_k),
        s.owner_name,
        s.site_url,
        s.contact_email,
        slot_finder=finder,
        scheduling_info=info,
    )


def with_fallbacks(models: list[Runnable]) -> Runnable:
    """Try each model in order; move on when one raises (overloaded, timed out, unavailable)."""
    return models[0] if len(models) == 1 else models[0].with_fallbacks(models[1:])
