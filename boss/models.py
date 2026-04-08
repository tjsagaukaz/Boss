from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents import Model, ModelProvider, OpenAIChatCompletionsModel, RunConfig
from openai import AsyncOpenAI

try:
    from agents import OpenAIProvider, OpenAIResponsesCompactionSession, SessionSettings, SQLiteSession
except ImportError:
    OpenAIProvider = None
    OpenAIResponsesCompactionSession = None
    SessionSettings = None
    SQLiteSession = None

try:
    from agents import OpenAIResponsesModel
except ImportError:
    OpenAIResponsesModel = None

from boss.config import settings


def _get_client() -> AsyncOpenAI:
    if not settings.cloud_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    return AsyncOpenAI(api_key=settings.cloud_api_key)


_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = _get_client()
    return _client


class CloudModelProvider(ModelProvider):
    """Route model names through the installed OpenAI provider surface with fallback."""

    def __init__(self, *, mode: str | None = None):
        self._mode = resolve_provider_mode(mode)

    def get_model(self, model_name: str | None) -> Model:
        delegate = _get_delegate_provider(self._mode)
        if delegate is not None:
            return delegate.get_model(model_name)
        return OpenAIChatCompletionsModel(
            model=model_name or settings.general_model,
            openai_client=get_client(),
        )


@dataclass(frozen=True)
class RunExecutionOptions:
    run_config: RunConfig
    session: Any | None = None


def supports_responses_mode() -> bool:
    return OpenAIProvider is not None and OpenAIResponsesModel is not None


def resolve_provider_mode(mode: str | None = None) -> str:
    requested = (mode or settings.provider_mode or "auto").strip().lower()
    if requested == "auto":
        return "responses" if supports_responses_mode() else "chat_completions"
    if requested == "responses_websocket" and not supports_responses_mode():
        return "chat_completions"
    if requested == "responses" and not supports_responses_mode():
        return "chat_completions"
    if requested not in {"chat_completions", "responses", "responses_websocket"}:
        return "responses" if supports_responses_mode() else "chat_completions"
    return requested


def provider_uses_responses(mode: str | None = None) -> bool:
    return resolve_provider_mode(mode) in {"responses", "responses_websocket"}


def build_session_settings() -> SessionSettings | None:
    if SessionSettings is None:
        return None
    return SessionSettings(limit=settings.provider_session_limit)


def resolve_provider_session_mode() -> str:
    requested = (settings.provider_session_mode or "disabled").strip().lower()
    if requested not in {"disabled", "local_sqlite", "responses_compaction"}:
        return "disabled"
    if requested == "responses_compaction" and not supports_responses_mode():
        return "local_sqlite" if SQLiteSession is not None else "disabled"
    if requested == "local_sqlite" and SQLiteSession is None:
        return "disabled"
    if requested == "responses_compaction" and SQLiteSession is None:
        return "disabled"
    return requested


def build_run_execution_options(
    *,
    session_id: str | None = None,
    workflow_name: str = "Boss workflow",
    trace_metadata: dict[str, Any] | None = None,
) -> RunExecutionOptions:
    run_config = RunConfig(
        model_provider=CloudModelProvider(),
        tracing_disabled=not settings.tracing_enabled,
        workflow_name=workflow_name,
        trace_metadata=trace_metadata,
        session_settings=build_session_settings(),
    )
    return RunExecutionOptions(
        run_config=run_config,
        session=build_runner_session(session_id),
    )


def build_runner_session(session_id: str | None):
    if not session_id:
        return None

    session_mode = resolve_provider_session_mode()
    if session_mode == "disabled" or SQLiteSession is None:
        return None

    settings.provider_session_db_file.parent.mkdir(parents=True, exist_ok=True)
    base_session = SQLiteSession(
        session_id=session_id,
        db_path=settings.provider_session_db_file,
        session_settings=build_session_settings(),
    )

    if session_mode != "responses_compaction":
        return base_session

    if OpenAIResponsesCompactionSession is None or not provider_uses_responses():
        return base_session

    return OpenAIResponsesCompactionSession(
        session_id=session_id,
        underlying_session=base_session,
        client=get_client(),
        model=settings.provider_compaction_model,
        should_trigger_compaction=_should_trigger_compaction,
    )


def _get_delegate_provider(mode: str) -> ModelProvider | None:
    if OpenAIProvider is None:
        return None

    return OpenAIProvider(
        openai_client=get_client(),
        use_responses=mode in {"responses", "responses_websocket"},
        use_responses_websocket=mode == "responses_websocket",
    )


def _should_trigger_compaction(context: dict[str, Any]) -> bool:
    candidate_items = context.get("compaction_candidate_items")
    if not isinstance(candidate_items, list):
        return False
    return len(candidate_items) >= settings.provider_compaction_threshold