"""LangGraph agent. For now: retrieve -> generate. Scheduling nodes will be added as a second branch later."""

from collections.abc import Callable
from typing import Annotated, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.prompts import SYSTEM_PROMPT, format_context
from app.store import Chunk


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    chunks: list[Chunk]


Retriever = Callable[[str], list[Chunk]]


def _retrieval_query(messages: list[AnyMessage]) -> str:
    # Include the previous user turn so follow-ups like "what stack did it use?" still retrieve well.
    user_turns = [m.content for m in messages if isinstance(m, HumanMessage)]
    return "\n".join(str(t) for t in user_turns[-2:])


def build_graph(llm: BaseChatModel, retriever: Retriever, owner: str, site: str = "", contact: str = ""):
    contact = contact or "the contact links on the website"
    def retrieve(state: State) -> dict:
        return {"chunks": retriever(_retrieval_query(state["messages"]))}

    def generate(state: State) -> dict:
        system = SystemMessage(SYSTEM_PROMPT.format(owner=owner, site=site or "this site", contact=contact, context=format_context(state["chunks"])))
        reply = llm.invoke([system, *state["messages"]])
        return {"messages": [reply]}

    g = StateGraph(State)
    g.add_node("retrieve", retrieve)
    g.add_node("generate", generate)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", END)
    return g.compile()


def default_graph():
    from langchain_google_genai import ChatGoogleGenerativeAI

    from app.config import get_settings
    from app.embeddings import embed_query
    from app.store import search

    s = get_settings()
    llm = ChatGoogleGenerativeAI(
        model=s.chat_model, google_api_key=s.google_api_key, temperature=0.3, max_output_tokens=800
    )
    return build_graph(llm, lambda q: search(embed_query(q), s.top_k), s.owner_name, s.site_url, s.contact_email)
