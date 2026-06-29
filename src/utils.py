"""Shared utilities used across the pipeline."""
import os
import re
import uuid


def _uid() -> str:
    return str(uuid.uuid4())


def _hex20() -> str:
    return uuid.uuid4().hex[:20]


def strip_json_fences(raw: str) -> str:
    """Remove markdown code fences from an LLM response."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    return cleaned.strip()


def call_api_azure(system: str, user: str, trace, name: str) -> str:
    """Call Azure OpenAI with a system+user prompt pair.

    Args:
        system: system prompt
        user: user prompt
        trace: observability trace (or None)
        name: generation name for Langfuse tracing
    """
    import observability as obs
    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ["AZURE_OPENAI_API_VERSION"],
    )
    model = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    gen = (trace or obs._Noop()).generation(name=name, model=model, input=messages)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=4096,
        temperature=0,
    )
    result = response.choices[0].message.content
    gen.end(output=result)
    return result
