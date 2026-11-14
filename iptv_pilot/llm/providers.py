from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod

from openai import OpenAI

SYSTEM_PROMPT = """你是电视节目摘要助手。仅根据给定转录生成简体中文 JSON，不添加转录中没有的事实。
输出必须是对象：summary_text 为 80-150 字；key_points 为 3-5 条字符串；entities 为实体字符串数组；follow_up 为跟进建议字符串。"""


class LLMProvider(ABC):
    @abstractmethod
    def summarize(self, transcript: str) -> dict:
        raise NotImplementedError


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str | None, model: str):
        if not api_key:
            raise RuntimeError("LLM API key is empty")
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "90")),
            max_retries=2,
        )
        self.model = model

    def summarize(self, transcript: str) -> dict:
        return self._summarize(transcript, SYSTEM_PROMPT)

    def summarize_grounded(self, payload: dict) -> dict:
        prompt = SYSTEM_PROMPT + "\n输入包含带 id 的原文段落和可选笔记。增加 evidence 数组：每项包含 point_index（要点从0起）、segment_id、quote（原文连续摘录，逐字一致）。每条要点应有1-3条证据。只能引用输入的原文 id，不能把笔记当原文，不能编造证据。没有依据的内容不要写入要点。"
        return self._summarize(json.dumps(payload, ensure_ascii=False), prompt)

    def _summarize(self, transcript: str, prompt: str) -> dict:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": transcript},
            ],
        )
        content = response.choices[0].message.content or "{}"
        result = json.loads(content)
        if "follow_up" in result:
            follow_up = result["follow_up"]
            if follow_up is None:
                result["follow_up"] = ""
            elif isinstance(follow_up, list) and all(isinstance(item, str) for item in follow_up):
                result["follow_up"] = "\n".join(follow_up)
        validate_summary(result)
        return result


class MockProvider(LLMProvider):
    def summarize_grounded(self, payload: dict) -> dict:
        source = payload["segments"][0] if payload["segments"] else {"id": "", "text": ""}
        result = self.summarize(source["text"])
        result["evidence"] = [{"point_index": 0, "segment_id": source["id"], "quote": source["text"][:120]}]
        return result

    def summarize(self, transcript: str) -> dict:
        preview = transcript.strip().replace("\n", " ")[:120]
        return {
            "summary_text": f"冒烟测试摘要：{preview}",
            "key_points": ["LLM_PROVIDER=mock，仅用于验证处理链路"],
            "entities": [],
            "follow_up": "配置真实 LLM Provider 后重新生成摘要。",
        }


def validate_summary(value: dict) -> None:
    required = {"summary_text", "key_points", "entities", "follow_up"}
    missing = required.difference(value)
    if missing:
        raise ValueError(f"LLM result missing fields: {sorted(missing)}")
    if not isinstance(value["key_points"], list) or not isinstance(value["entities"], list):
        raise ValueError("key_points and entities must be arrays")
    if not isinstance(value["summary_text"], str) or not value["summary_text"].strip():
        raise ValueError("summary_text must be a non-empty string")
    if not isinstance(value["follow_up"], str):
        raise ValueError("follow_up must be a string")


def create_provider() -> LLMProvider:
    provider = os.getenv("LLM_PROVIDER", "mock").lower()
    model = os.getenv("LLM_MODEL", "doubao-seed-1-6-250615")
    if provider == "mock":
        return MockProvider()
    if provider == "ark":
        return OpenAICompatibleProvider(
            os.getenv("ARK_API_KEY", ""),
            os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            model,
        )
    if provider == "openai":
        return OpenAICompatibleProvider(
            os.getenv("OPENAI_API_KEY", ""), os.getenv("OPENAI_BASE_URL"), model
        )
    if provider == "custom":
        return OpenAICompatibleProvider(
            os.getenv("LLM_API_KEY", ""), os.getenv("LLM_BASE_URL"), model
        )
    raise RuntimeError(f"unsupported LLM_PROVIDER={provider}")
