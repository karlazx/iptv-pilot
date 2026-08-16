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
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
        )
        content = response.choices[0].message.content or "{}"
        result = json.loads(content)
        validate_summary(result)
        return result


class MockProvider(LLMProvider):
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
    provider = os.getenv("LLM_PROVIDER", "ark").lower()
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
