from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Literal, cast

from deepagents import create_deep_agent
from deepagents.backends.protocol import SandboxBackendProtocol
from langchain.agents.middleware import AgentState, ModelCallLimitMiddleware
from langchain.agents.middleware.types import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.constants import Send
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import RunnableConfig
from langgraph.pregel import Pregel
from langgraph.runtime import Runtime
from pydantic import BaseModel

import agent.tools as agent_tools

from .dashboard.options import gate_fable_model
from .dashboard.team_settings import get_team_fable_enabled
from .middleware import (
    ExcludeToolsMiddleware,
    RepairOrphanedToolCallsMiddleware,
    SanitizeFireworksMessagesMiddleware,
    SanitizeThinkingBlocksMiddleware,
    SanitizeToolInputsMiddleware,
    SlackAssistantStatusMiddleware,
    TimeoutWrapupMiddleware,
    ToolErrorMiddleware,
    check_message_queue_before_model,
    refresh_github_proxy_before_model,
    settle_review_check_on_exit,
)
from .middleware.prepare_run import PrepareRunState
from .review.adversarial import (
    AdversarialState,
    FinderOutput,
    FinderRun,
    GateOutput,
    VerdictBatch,
    apply_independence,
    configured_model_pair,
    dedupe_candidates,
    finding_description,
    gate_triggers,
    merge_kept_candidates,
    publication_blocker,
    reset_run_state,
    validate_verdicts,
)
from .review.diff import materialize_review_diff
from .review.findings import (
    REVIEW_FINDING_CAP,
    SEVERITY_ORDER,
    Severity,
    get_thread_metadata,
    set_reviewer_thread_metadata,
)
from .reviewer import (
    REVIEW_STAGE_TOOL_NAMES,
    REVIEWER_EVAL_PROMPT_SUFFIX,
    REVIEWER_PROMPT_TEMPLATE,
    PrepareReviewerRunMiddleware,
    _build_first_review_context,
    _cached_gateway_enabled,
    _cached_review_profile_name,
    _cached_reviewer_team_defaults,
    _ensure_reviewer_sandbox_for_thread,
    _format_parent_review_context,
    _make_model_or_defer,
    _repo_checkout_note,
    _schedule_diff_grouping,
    gather_review_context,
)
from .runtime import (
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_RECURSION_LIMIT,
    MODEL_CALL_RECURSION_LIMIT,
    get_cached_sandbox_backend,
    graph_loaded_for_execution,
)
from .utils.agent_definitions import build_subagents, load_agent_definition
from .utils.github_checks import incomplete_review_check_result
from .utils.model import DEFAULT_LLM_REASONING, provider_model_kwargs
from .utils.stage_profiles import resolve_stage_profile
from .utils.tracing import REVIEW_TRACING_PROJECT, traced_graph_factory

logger = logging.getLogger(__name__)
_ADVERSARIAL_FAILURE_CLASSES = frozenset(
    {
        "prepare failed",
        "finder fanout incomplete or failed",
        "adjudication failed",
        "pre-publish gate failed",
        "record/publish failed",
        "adjudication verdicts must cover every candidate ID exactly once",
    }
)
RESERVED_SUBAGENT_TOOLS = frozenset(
    {
        "add_finding",
        "update_finding",
        "publish_review",
        "resolve_finding_thread",
        "reply_to_finding_thread",
    }
)
_DEFINITION = load_agent_definition("reviewer-adversarial")
_STAGE_TOOLS = [
    getattr(agent_tools, name)
    for name in _DEFINITION.tools
    if name in {"web_search", "fetch_url", "http_request"}
]


def _render_parent_prompt(
    *,
    working_dir: str,
    repo_owner: str,
    repo_name: str,
    pr_number: int | str,
    repo_checkout_note: str,
) -> str:
    body = _DEFINITION.body.format(
        working_dir=working_dir,
        repo_owner=repo_owner,
        repo_name=repo_name,
        pr_number=pr_number,
        review_finding_cap=REVIEW_FINDING_CAP,
        repo_checkout_note=repo_checkout_note,
    )
    return f"{_DEFINITION.shared}\n\n{body}" if _DEFINITION.shared else body


_ = _render_parent_prompt(
    working_dir="", repo_owner="", repo_name="", pr_number=0, repo_checkout_note=""
)


async def _prepare_context(
    thread_id: str,
    configurable: dict[str, Any],
    *,
    materialize_path: bool = False,
    use_gateway: bool = False,
) -> dict[str, Any]:
    if (
        configurable.get("re_review")
        or configurable.get("reviewer_event")
        or configurable.get("last_reviewed_sha")
    ):
        raise RuntimeError(
            "reviewer_adversarial v1 handles first reviews only; re-review and "
            "finding-reply dispatch belong to the stock reviewer"
        )
    bundle = await gather_review_context(thread_id, configurable, diff_mode="adversarial")
    review_context = ""
    if isinstance(bundle.pr_number, int):
        review_context = _build_first_review_context(
            pr_url=bundle.pr_url,
            repo_owner=bundle.repo_owner,
            repo_name=bundle.repo_name,
            pr_number=bundle.pr_number,
            base_sha=bundle.base_sha,
            head_sha=bundle.head_sha,
            pr_title=bundle.pr_title,
            pr_body=bundle.pr_body,
            existing_threads_block="",
            include_historical_guidance=False,
        )
    working_dir = f"{bundle.work_dir}/{bundle.repo_name}" if bundle.repo_name else bundle.work_dir
    checkout_note = _repo_checkout_note(
        repo_ready=bundle.repo_ready,
        working_dir=working_dir,
        repo_owner=bundle.repo_owner,
        repo_name=bundle.repo_name,
        pr_number=bundle.pr_number,
        head_sha=bundle.head_sha,
    )
    prompt = _render_parent_prompt(
        working_dir=working_dir,
        repo_owner=bundle.repo_owner,
        repo_name=bundle.repo_name,
        pr_number=bundle.pr_number,
        repo_checkout_note=checkout_note,
    )
    context_parts = [checkout_note]
    if bundle.reviewer_eval:
        prompt = f"{prompt}\n{REVIEWER_EVAL_PROMPT_SUFFIX}"
        context_parts.append(REVIEWER_EVAL_PROMPT_SUFFIX)
    if review_context:
        prompt = f"{prompt}\n\n{review_context}"
        context_parts.append(review_context)
    parent_review_context = _format_parent_review_context(
        bundle, include_repo_style=not bundle.reviewer_eval
    )
    if parent_review_context:
        prompt = f"{prompt}\n\n{parent_review_context}"
    prepared = {
        "work_dir": bundle.work_dir,
        "working_dir": working_dir,
        "rendered_system_prompt": prompt,
        "stage_context": "\n\n".join(context_parts),
        "parent_review_context": parent_review_context,
        "diff_text": bundle.diff_text,
        "diff_line_set": bundle.diff_line_set,
        "pr_title": bundle.pr_title,
        "agents_md_content": bundle.agents_md_content,
        "scoped_agents_md": bundle.scoped_agents_md,
        "skill_sources": bundle.skill_sources,
    }
    if materialize_path:
        materialized = await materialize_review_diff(
            bundle.sandbox_backend,
            work_dir=working_dir,
            base_ref=bundle.base_sha or "base",
            head_ref=bundle.head_sha or "head",
            merge_base=True,
            diff_text=bundle.diff_text,
        )
        prepared["diff_path"] = materialized.path
    await _schedule_diff_grouping(
        configurable=configurable,
        use_gateway=use_gateway,
        thread_id=thread_id,
        head_sha=bundle.head_sha,
        diff_text=bundle.diff_text,
    )
    return prepared


class PrepareAdversarialReviewerRunMiddleware(PrepareReviewerRunMiddleware):
    async def _prepare(self, state: PrepareRunState, runtime: Runtime) -> dict[str, Any]:
        del state, runtime
        prepared = await _prepare_context(
            self._thread_id,
            self._config.get("configurable") or {},
            use_gateway=self._use_gateway,
        )
        return {
            key: prepared[key]
            for key in ("work_dir", "rendered_system_prompt", "diff_text", "diff_line_set")
        }


async def _run_stage(
    graph: Pregel,
    prompt: str,
    config: RunnableConfig,
    timeout: float = 50 * 60,
) -> BaseModel:
    result = await asyncio.wait_for(
        graph.ainvoke({"messages": [{"role": "user", "content": prompt}]}, config=config),
        timeout=timeout,
    )
    structured = result.get("structured_response") if isinstance(result, dict) else None
    if not isinstance(structured, BaseModel):
        # An empty message list means the stage graph ran no nodes at all, which
        # is a different failure from a model that answered without the tool.
        logger.error(
            "Bounded reviewer stage returned no structured response: "
            "result_keys=%s message_count=%s configurable_keys=%s",
            sorted(result) if isinstance(result, dict) else type(result).__name__,
            len(result["messages"]) if isinstance(result, dict) and "messages" in result else None,
            sorted(config.get("configurable") or {}),
        )
        raise RuntimeError("bounded reviewer stage returned no structured response")
    return structured


def _judgment_context(state: AdversarialState) -> str:
    stage_context = state.get("stage_context", "")
    parent_review_context = state.get("parent_review_context", "")
    if not parent_review_context:
        return stage_context
    return f"{stage_context}\n\n{parent_review_context}" if stage_context else parent_review_context


def _active_finder_names(
    finder_names: list[str],
    *,
    agents_md_content: str | None,
    scoped_agents_md: Mapping[str, str],
) -> list[str]:
    if agents_md_content or scoped_agents_md:
        return list(finder_names)
    logger.info("Skipping conventions finder: no repository instructions loaded")
    return [name for name in finder_names if name != "conventions"]


def _finder_payload(name: str, state: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "finder_name": name,
        "diff_path": state["diff_path"],
        "working_dir": state["working_dir"],
        "stage_context": state.get("stage_context", ""),
        "skill_sources": state.get("skill_sources", []),
    }
    if name == "conventions":
        payload["agents_md_content"] = state.get("agents_md_content")
        payload["scoped_agents_md"] = state.get("scoped_agents_md", {})
    return payload


def _finder_prompt(state: Mapping[str, Any]) -> str:
    name = cast(str, state["finder_name"])
    prompt = (
        f"Review the complete materialized diff at {state['diff_path']} against "
        f"the checkout at {state['working_dir']}. Review context: "
        f"{state.get('stage_context', '')}. Return only structured candidate defects."
    )
    skill_sources = cast(list[str], state.get("skill_sources", []))
    if skill_sources:
        prompt += (
            "\n\nRepository reviewer skills are available at these read-only source roots:\n"
            + "\n".join(f"- {path}" for path in skill_sources)
            + "\nGrep these roots for the SKILL.md matching the area you are reviewing; "
            "do not load unrelated skills."
        )
    if name != "conventions":
        return prompt
    instruction_sections: list[str] = []
    root_instructions = state.get("agents_md_content")
    if isinstance(root_instructions, str) and root_instructions:
        instruction_sections.append("### Root repository instructions\n" + root_instructions)
    scoped_instructions = cast(dict[str, str], state.get("scoped_agents_md", {}))
    instruction_sections.extend(
        f"### Scoped repository instructions: {path}\n{content}"
        for path, content in sorted(
            scoped_instructions.items(), key=lambda item: (item[0].count("/"), item[0])
        )
    )
    if instruction_sections:
        prompt += "\n\nRepository instructions to enforce:\n\n" + "\n\n".join(instruction_sections)
    return prompt


def _stage_middleware(extra: list[AgentMiddleware[Any, Any, Any]] | None = None):
    return [
        *(extra or []),
        SanitizeToolInputsMiddleware(),
        ModelCallLimitMiddleware(run_limit=MODEL_CALL_RECURSION_LIMIT, exit_behavior="end"),
        ToolErrorMiddleware(),
        refresh_github_proxy_before_model,
        check_message_queue_before_model,
        SlackAssistantStatusMiddleware(),
        TimeoutWrapupMiddleware(),
        SanitizeFireworksMessagesMiddleware(),
        SanitizeThinkingBlocksMiddleware(),
        RepairOrphanedToolCallsMiddleware(),
    ]


def _bounded_agent(
    *,
    model: BaseChatModel,
    response_format: type[BaseModel],
    backend: Any,
    prompt: str = "",
    tools: list[Any] | None = None,
    middleware: list[AgentMiddleware[Any, Any, Any]] | None = None,
) -> Pregel:
    return create_deep_agent(
        model=model,
        system_prompt=prompt,
        tools=tools or [],
        backend=backend,
        # Stage sub-agents must not join the spine's persistence.
        checkpointer=False,
        response_format=ToolStrategy(response_format),
        middleware=cast(list[AgentMiddleware[Any, Any, Any]], _stage_middleware(middleware)),
    )


async def get_reviewer_adversarial_agent(config: RunnableConfig) -> Pregel:
    """Build the checkpointable adversarial reviewer StateGraph."""
    config = config.copy()
    configurable = dict(config.get("configurable") or {})
    config["configurable"] = configurable
    config.setdefault("recursion_limit", DEFAULT_RECURSION_LIMIT)
    thread_id = configurable.get("thread_id")
    if thread_id is None or not graph_loaded_for_execution(config):
        return create_deep_agent(system_prompt="", tools=[]).with_config(config)

    is_eval = configurable.get("reviewer_eval") is True or configurable.get("eval") is True
    profile_name = await _cached_review_profile_name()
    profile = resolve_stage_profile(
        "review",
        profile_name,
        allowed_tools=REVIEW_STAGE_TOOL_NAMES,
        fallback_body=REVIEWER_PROMPT_TEMPLATE,
    )
    if profile.name != "default":
        logger.info(
            "Ignoring review profile body %r for the adversarial reviewer; model, "
            "reasoning effort, and tool pins still apply",
            profile.name,
        )

    configured = configured_model_pair(configurable, is_eval, "reviewer_adversarial", "reviewer")
    if configured:
        model_id, effort = configured
        subagent_model_id, subagent_effort = configured
    else:
        (
            (model_id, effort),
            (subagent_model_id, subagent_effort),
        ) = await _cached_reviewer_team_defaults()
        if profile.model is not None:
            model_id = subagent_model_id = profile.model
            effort = subagent_effort = profile.reasoning_effort
    configured_subagent = configured_model_pair(
        configurable, is_eval, "reviewer_adversarial_subagent", "reviewer_subagent"
    )
    if configured_subagent:
        subagent_model_id, subagent_effort = configured_subagent

    fable_enabled = await get_team_fable_enabled()
    model_id, effort = gate_fable_model(model_id, effort, fable_enabled=fable_enabled)
    subagent_model_id, subagent_effort = gate_fable_model(
        subagent_model_id, subagent_effort, fable_enabled=fable_enabled
    )
    use_gateway = await _cached_gateway_enabled()
    parent_model = _make_model_or_defer(
        model_id,
        use_gateway=use_gateway,
        **provider_model_kwargs(
            model_id,
            effort,
            max_tokens=DEFAULT_LLM_MAX_TOKENS,
            openai_reasoning_default=DEFAULT_LLM_REASONING,
        ),
    )
    subagent_model = _make_model_or_defer(
        subagent_model_id,
        use_gateway=use_gateway,
        **provider_model_kwargs(
            subagent_model_id,
            subagent_effort,
            max_tokens=DEFAULT_LLM_MAX_TOKENS,
            openai_reasoning_default=DEFAULT_LLM_REASONING,
        ),
    )

    async def reconnect_backend(
        _thread_id: str = thread_id,
        _configurable: dict[str, Any] = configurable,
    ) -> SandboxBackendProtocol:
        backend, _ = await _ensure_reviewer_sandbox_for_thread(_thread_id, _configurable)
        return backend

    def backend_factory(_runtime: object, _thread_id: str = thread_id):
        return get_cached_sandbox_backend(_thread_id, reconnect=reconnect_backend)

    specs = build_subagents(
        _DEFINITION, model=subagent_model, reserved_tools=RESERVED_SUBAGENT_TOOLS
    )
    allowed = frozenset(profile.tools) if profile.tools is not None else None
    stage_agents: dict[str, Pregel] = {}
    for spec in specs:
        name = str(spec["name"])
        if name == "general-purpose":
            continue
        middleware = list(spec.get("middleware", []))
        if allowed is not None:
            response_tool = (
                VerdictBatch.__name__ if name == "adjudicator" else FinderOutput.__name__
            )
            middleware.append(ExcludeToolsMiddleware(allowed=allowed | {response_tool}))
        middleware.append(ExcludeToolsMiddleware(excluded=frozenset({"task"})))
        stage_agents[name] = _bounded_agent(
            model=cast(BaseChatModel, spec.get("model", subagent_model)),
            response_format=VerdictBatch if name == "adjudicator" else FinderOutput,
            backend=backend_factory,
            tools=cast(list[Any], spec.get("tools", [])),
            middleware=cast(list[AgentMiddleware[Any, Any, Any]], middleware),
        )
    repo = configurable.get("repo")
    repo_owner = str(repo.get("owner", "")) if isinstance(repo, dict) else ""
    repo_name = str(repo.get("name", "")) if isinstance(repo, dict) else ""
    parent_prompt = _render_parent_prompt(
        working_dir="the checkout supplied by the task",
        repo_owner=repo_owner,
        repo_name=repo_name,
        pr_number=configurable.get("pr_number", ""),
        repo_checkout_note="Inspect the task-supplied checkout and materialized diff.",
    )
    parent_filters = (
        [ExcludeToolsMiddleware(allowed=allowed | {VerdictBatch.__name__, GateOutput.__name__})]
        if allowed is not None
        else []
    )
    parent_filters.append(ExcludeToolsMiddleware(excluded=frozenset({"task"})))
    parent_adjudicator = _bounded_agent(
        model=parent_model,
        response_format=VerdictBatch,
        backend=backend_factory,
        prompt=parent_prompt,
        tools=_STAGE_TOOLS,
        middleware=cast(list[AgentMiddleware[Any, Any, Any]], parent_filters),
    )
    gate_agent = _bounded_agent(
        model=parent_model,
        response_format=GateOutput,
        backend=backend_factory,
        prompt=(f"{_DEFINITION.shared}\n\n" if _DEFINITION.shared else "")
        + "You are the final bounded verification pass for an adversarial code review.",
        tools=_STAGE_TOOLS,
        middleware=cast(list[AgentMiddleware[Any, Any, Any]], parent_filters),
    )
    finder_names = sorted(name for name in stage_agents if name != "adjudicator")

    async def prepare(state: AdversarialState) -> dict[str, Any]:
        del state
        try:
            prepared = await _prepare_context(
                cast(str, thread_id),
                configurable,
                materialize_path=True,
                use_gateway=use_gateway,
            )
            active_finder_names = _active_finder_names(
                finder_names,
                agents_md_content=cast(str | None, prepared.get("agents_md_content")),
                scoped_agents_md=cast(dict[str, str], prepared.get("scoped_agents_md", {})),
            )
            return reset_run_state(prepared, active_finder_names)
        except Exception as exc:
            return {"error": f"prepare failed: {exc}"}

    def fanout(state: AdversarialState) -> list[Send] | Literal["fail"]:
        if state.get("error"):
            return "fail"
        data = cast(dict[str, Any], state)
        return [Send("find", _finder_payload(name, data)) for name in data["finders_expected"]]

    async def find(state: AdversarialState) -> dict[str, Any]:
        data = cast(dict[str, Any], state)
        name = cast(str, data["finder_name"])
        prompt = _finder_prompt(data)
        try:
            output = cast(FinderOutput, await _run_stage(stage_agents[name], prompt, config))
            result: FinderRun = {
                "finder": name,
                "candidates": [item.model_dump() for item in output.candidates],
                "error": None,
            }
        except Exception as exc:
            result = {
                "finder": name,
                "candidates": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {"finder_results": [result]}

    async def dedupe(state: AdversarialState) -> dict[str, Any]:
        results = state.get("finder_results", [])
        result_names = [item["finder"] for item in results]
        if (
            len(result_names) != len(set(result_names))
            or set(result_names) != set(state.get("finders_expected", []))
            or any(item["error"] for item in results)
        ):
            return {"error": "finder fanout incomplete or failed"}
        return {
            "candidates": dedupe_candidates(
                [candidate for item in results for candidate in item["candidates"]]
            )
        }

    async def adjudicate(state: AdversarialState) -> dict[str, Any]:
        if state.get("error"):
            return {}
        candidates = state.get("candidates", [])
        if not candidates:
            return {"verdicts": [], "kept_candidates": []}
        prompt = (
            f"Adjudicate every candidate exactly once by candidate_id against diff "
            f"{state.get('diff_path', '')} and checkout {state.get('working_dir', '')}. "
            f"Review context: {_judgment_context(state)}. Candidates: {candidates}"
        )
        try:
            agent = stage_agents.get("adjudicator", parent_adjudicator)
            output = cast(VerdictBatch, await _run_stage(agent, prompt, config))
            verdicts = [item.model_dump() for item in output.verdicts]
            by_id = validate_verdicts(candidates, verdicts)
            kept = merge_kept_candidates(
                [
                    item
                    for item in candidates
                    if by_id[item["candidate_id"]].verdict == "keep-confirmed"
                ]
            )
            return {"verdicts": verdicts, "kept_candidates": kept}
        except Exception as exc:
            return {"error": f"adjudication failed: {exc}"}

    async def prepublish(state: AdversarialState) -> dict[str, Any]:
        adjudicated = list(state.get("kept_candidates", []))
        kept = list(adjudicated)
        triggers, _ = gate_triggers(state.get("diff_text", ""), kept)
        if not triggers:
            return {"gate_triggers": [], "kept_candidates": kept}
        rewalk = [
            trigger
            for trigger in triggers
            if trigger in {"zero-findings", "uncovered-major-prefix"}
        ]
        additions: list[dict[str, Any]] = []
        gate_verdicts: list[dict[str, Any]] = []
        try:
            if rewalk:
                output = cast(
                    GateOutput,
                    await _run_stage(
                        gate_agent,
                        f"Run only these re-read checks: {rewalk}. Re-read diff "
                        f"{state.get('diff_path', '')} and checkout "
                        f"{state.get('working_dir', '')} with PR title "
                        f"{state.get('pr_title', '')!r}. Return only candidates and leave "
                        "independence empty. Review context: "
                        f"{_judgment_context(state)}. Current confirmed candidates: {kept}.",
                        config,
                    ),
                )
                additions = dedupe_candidates([item.model_dump() for item in output.candidates])
                for index, item in enumerate(additions):
                    item["candidate_id"] = f"g{index + 1}"
                if additions:
                    agent = stage_agents.get("adjudicator", parent_adjudicator)
                    verdict_output = cast(
                        VerdictBatch,
                        await _run_stage(
                            agent,
                            f"Adjudicate every gate candidate exactly once with review context "
                            f"{_judgment_context(state)}: {additions}",
                            config,
                        ),
                    )
                    gate_verdicts = [item.model_dump() for item in verdict_output.verdicts]
                    by_id = validate_verdicts(additions, gate_verdicts)
                    kept.extend(
                        item
                        for item in additions
                        if by_id[item["candidate_id"]].verdict == "keep-confirmed"
                    )
                    kept = merge_kept_candidates(kept)
            _, collisions = gate_triggers(state.get("diff_text", ""), kept)
            if collisions:
                if "same-file-independence" not in triggers:
                    triggers.append("same-file-independence")
                output = cast(
                    GateOutput,
                    await _run_stage(
                        gate_agent,
                        "Judge whether each same-file candidate group has independent failure "
                        "modes. Return no candidates and one independence decision per listed "
                        "group. keep_candidate_ids must name survivors from that group and must "
                        "not be empty for a non-independent group. Review context: "
                        f"{_judgment_context(state)}. Groups: {collisions}. Candidates: {kept}",
                        config,
                    ),
                )
                kept = apply_independence(kept, collisions, output.independence)
            return {
                "gate_triggers": triggers,
                "gate_candidates": additions,
                "gate_verdicts": gate_verdicts,
                "kept_candidates": kept,
            }
        except Exception as exc:
            if adjudicated:
                logger.exception(
                    "Pre-publish gate failed; publishing adjudicated candidates unchanged"
                )
                return {"gate_triggers": triggers, "kept_candidates": adjudicated}
            return {"error": f"pre-publish gate failed: {exc}", "gate_triggers": triggers}

    async def record_publish(state: AdversarialState) -> dict[str, Any]:
        if blocker := publication_blocker(state):
            return {"error": blocker}
        ordered = sorted(
            state.get("kept_candidates", []),
            key=lambda item: (
                -SEVERITY_ORDER[cast(Severity, item["severity"])],
                item["file"],
                item["start_line"],
            ),
        )[:REVIEW_FINDING_CAP]
        try:
            for candidate in ordered:
                result = await agent_tools.add_finding(
                    severity=candidate["severity"],
                    confidence="high",
                    category=candidate["category"],
                    file=candidate["file"],
                    title=" ".join(candidate["failure_mode"].split()[:10]),
                    description=finding_description(candidate),
                    start_line=candidate["start_line"],
                    end_line=candidate["end_line"],
                    side=candidate["side"],
                    state=cast(dict[str, Any], state),
                )
                if not result.get("success"):
                    raise RuntimeError(str(result.get("error") or "finding write rejected"))
            publication = await agent_tools.publish_review(state=cast(dict[str, Any], state))
            if not publication.get("success"):
                raise RuntimeError(str(publication.get("error") or "publish failed"))
            return {"publication": publication}
        except Exception as exc:
            return {"error": f"record/publish failed: {exc}"}

    async def settle(state: AdversarialState, runtime: Runtime) -> dict[str, Any]:
        # Every failure route lands here, and the graph still exits "success", so
        # this is the only place a swallowed spine error can reach the log.
        if error := state.get("error"):
            finder_errors = {
                item["finder"]: item["error"]
                for item in state.get("finder_results", [])
                if item["error"]
            }
            logger.error(
                "Adversarial review ended without publishing: %s finder_errors=%s",
                error,
                finder_errors or None,
            )
            try:
                metadata = await get_thread_metadata(cast(str, thread_id))
                check_run_id = metadata.get("review_check_run_id")
                deferred = metadata.get("review_check_deferred_result")
                pending = metadata.get("review_check_pending_result")
                failure_class = error.partition(":")[0]
                if (
                    isinstance(check_run_id, int)
                    and not (
                        isinstance(deferred, dict)
                        and deferred.get("review_check_run_id") == check_run_id
                    )
                    and not isinstance(pending, dict)
                    and failure_class in _ADVERSARIAL_FAILURE_CLASSES
                ):
                    conclusion, title, summary = incomplete_review_check_result()
                    await set_reviewer_thread_metadata(
                        cast(str, thread_id),
                        extra={
                            "review_check_pending_result": {
                                "conclusion": conclusion,
                                "title": title,
                                "summary": f"{summary} Failure class: {failure_class}.",
                            }
                        },
                    )
            except Exception:
                logger.exception("Failed to record adversarial review failure class")
        await settle_review_check_on_exit.aafter_agent(cast(AgentState, state), runtime)
        return {}

    def failed(state: AdversarialState) -> Literal["fail", "continue"]:
        return "fail" if state.get("error") else "continue"

    builder = StateGraph(AdversarialState)
    builder.add_node("prepare", prepare)
    builder.add_node("find", find)
    builder.add_node("dedupe", dedupe)
    builder.add_node("adjudicate", adjudicate)
    builder.add_node("prepublish", prepublish)
    builder.add_node("record_publish", record_publish)
    builder.add_node("settle", settle)
    builder.add_edge(START, "prepare")
    builder.add_conditional_edges("prepare", fanout, {"fail": "settle"})
    builder.add_edge("find", "dedupe")
    builder.add_conditional_edges("dedupe", failed, {"fail": "settle", "continue": "adjudicate"})
    builder.add_conditional_edges(
        "adjudicate", failed, {"fail": "settle", "continue": "prepublish"}
    )
    builder.add_conditional_edges(
        "prepublish", failed, {"fail": "settle", "continue": "record_publish"}
    )
    builder.add_conditional_edges(
        "record_publish", failed, {"fail": "settle", "continue": "settle"}
    )
    builder.add_edge("settle", END)
    return builder.compile(name="reviewer_adversarial").with_config(config)


traced_reviewer_adversarial = traced_graph_factory(
    get_reviewer_adversarial_agent, REVIEW_TRACING_PROJECT
)
