"""Ollama 本地模型适配；对项目其他模块仍仅暴露 ROS Action。"""

from ai_edge.common import error_codes as Codes

import json
import socket
from ai_edge.common.catalog import catalog_ids
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from ai_edge.semantic.semantic_contract import (
    SCHEMA,
    SemanticError,
    reconcile_candidate,
    validate_candidate,
)
from ai_edge.semantic.semantic_prompt import build_messages, build_interaction_messages
from ai_edge.semantic.interaction_contract import (
    INTERACTION_SCHEMA,
    validate_interaction,
)
from ai_edge.semantic.entity_grounding import ground_interaction
from ai_edge.common.inference_config import LLM_DEFAULTS, validate_llm


class OllamaBackend:
    """标准库 HTTP 客户端，无需额外安装 ollama Python SDK。"""

    def __init__(
        self,
        base_url=LLM_DEFAULTS["ollama_url"],
        model=LLM_DEFAULTS["model"],
        timeout=LLM_DEFAULTS["request_timeout"],
        catalog=None,
        *,
        temperature=LLM_DEFAULTS["temperature"],
        num_ctx=LLM_DEFAULTS["num_ctx"],
        num_predict=LLM_DEFAULTS["num_predict"],
        keep_alive=LLM_DEFAULTS["keep_alive"],
    ):
        validate_llm(
            dict(
                LLM_DEFAULTS,
                ollama_url=base_url,
                model=model,
                request_timeout=timeout,
                temperature=temperature,
                num_ctx=num_ctx,
                num_predict=num_predict,
                keep_alive=keep_alive,
            )
        )
        self.base_url, self.model, self.timeout = base_url.rstrip("/"), model, timeout
        self.options = dict(
            temperature=temperature, num_ctx=num_ctx, num_predict=num_predict
        )
        self.keep_alive = keep_alive
        self.catalog = catalog
        # 本地模型调用不经过系统代理，避免 WSL 的代理配置干扰回环连接。
        self.opener = build_opener(ProxyHandler({}))

    def post(self, endpoint, payload, timeout=None):
        request = Request(
            self.base_url + endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=timeout or self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise SemanticError(
                    Codes.INVALID_MODEL_OUTPUT, "Ollama 响应超过大小上限"
                )
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("响应必须为 JSON 对象")
            return data
        except HTTPError as exc:
            code = (
                Codes.LLM_MODEL_UNAVAILABLE
                if exc.code == 404
                else Codes.LLM_UNAVAILABLE
            )
            raise SemanticError(code, f"Ollama 请求失败，HTTP {exc.code}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise SemanticError(Codes.PARSE_TIMEOUT, "Ollama 推理等待超时") from exc
        except URLError as exc:
            raise SemanticError(
                Codes.LLM_UNAVAILABLE, f"无法连接 Ollama：{exc.reason}"
            ) from exc
        except (ValueError, UnicodeError) as exc:
            raise SemanticError(
                Codes.INVALID_MODEL_OUTPUT, "Ollama 返回了无效 JSON"
            ) from exc

    def check_model(self):
        """只检查已安装模型，不自动下载模型或改动 Ollama 服务配置。"""
        self.post("/api/show", {"model": self.model}, timeout=5.0)

    def entities(self, stations, objects):
        entities = None
        if self.catalog is not None:
            snapshot = self.catalog.read(stations, objects)
            active_stations, active_objects = catalog_ids(snapshot)
            if not set(stations).issubset(active_stations) or not set(objects).issubset(
                active_objects
            ):
                raise SemanticError(
                    Codes.INVALID_REQUEST,
                    "请求白名单包含已停用或不存在的基础资料，请刷新配置",
                )
            entities = {
                kind: [
                    row for row in snapshot[kind] if row["enabled"] and row["id"] in ids
                ]
                for kind, ids in (("stations", stations), ("objects", objects))
            }
        return entities

    def generate(self, messages, schema):
        # UTF-8 字节数是 token 数的保守上界；给消息模板与输出留出上下文余量。
        if sum(len(item["content"].encode("utf-8")) for item in messages) > 16000:
            raise SemanticError(
                Codes.INVALID_REQUEST, "输入、历史和白名单超过本地模型的上下文预算"
            )
        data = self.post(
            "/api/chat",
            {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "think": False,
                "format": schema,
                "keep_alive": self.keep_alive,
                "options": self.options,
            },
        )
        try:
            if data.get("done") is not True or data.get("done_reason") == "length":
                raise ValueError("模型输出未完整结束")
            content = data["message"]["content"]
            if not isinstance(content, str) or len(content) > 262144:
                raise ValueError("模型内容类型或长度不正确")
            candidate = json.loads(content)
        except (KeyError, TypeError, ValueError) as exc:
            raise SemanticError(
                Codes.INVALID_MODEL_OUTPUT, "模型未返回完整的任务 JSON"
            ) from exc
        return candidate

    def parse(self, text, stations, objects, context=(), partial=None):
        messages = build_messages(
            text,
            stations,
            objects,
            list(context),
            partial,
            self.entities(stations, objects),
        )
        candidate = self.generate(messages, SCHEMA)
        # 先校验原始输出，再补回模型遗漏的已有字段，最后重新检查完整候选。
        checked = validate_candidate(candidate, stations, objects)
        merged = reconcile_candidate(checked.candidate, partial)
        return validate_candidate(merged, stations, objects)

    def interact(self, text, stations, objects, context=(), partial=None):
        entities = self.entities(stations, objects)
        if partial is not None:
            from ai_edge.semantic.explicit_commands import fill_explicit_answer

            completed = fill_explicit_answer(text, partial, stations, objects, entities)
            if completed is not None:
                return completed
        if partial is None:
            from ai_edge.semantic.explicit_commands import compile_explicit_commands

            compiled = compile_explicit_commands(text, stations, objects, entities)
            if compiled is not None:
                return compiled
        if partial is not None:
            # 待补充任务会诱导小模型把“你好/你在哪”也当成补参。
            # 先隔离旧任务识别本轮闲聊、只读查询，真正补参时才带入候选。
            current = build_interaction_messages(
                text, stations, objects, [], None, entities
            )
            routed_output = self.generate(current, INTERACTION_SCHEMA)
            if isinstance(routed_output, dict) and routed_output.get("intent") in (
                "CHAT",
                "QUERY",
            ):
                routed = validate_interaction(
                    ground_interaction(
                        routed_output, text, stations, objects, entities
                    ),
                    stations,
                    objects,
                )
                if routed.task_answer is None:
                    return routed
        messages = build_interaction_messages(
            text,
            stations,
            objects,
            list(context),
            partial,
            entities,
        )
        output = ground_interaction(
            self.generate(messages, INTERACTION_SCHEMA),
            text,
            stations,
            objects,
            entities,
            partial,
        )
        answer = validate_interaction(output, stations, objects, partial)
        if (
            answer.task_answer
            and not answer.task_answer.missing_fields
            and answer.intent != "CLARIFY"
        ):
            from ai_edge.task_drafts.actions import validate_action_plan

            validate_action_plan(answer.task_answer.candidate, stations, objects)
        return answer
