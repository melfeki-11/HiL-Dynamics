#!/usr/bin/env python3
"""Smoke test the configured LiteLLM-compatible proxy.

Credentials stay out of files. The script reads the LiteLLM key from
ANTHROPIC_AUTH_TOKEN, LITELLM_PROXY_API_KEY, LITELLM_API_KEY, OPENAI_API_KEY, or
the AWS Secrets Manager entry named by LITELLM_AWS_SECRET_ID and
LITELLM_AWS_SECRET_KEY.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from urllib.parse import urlparse

import litellm

DEFAULT_BASE_URL = ""
DEFAULT_MODEL = "bedrock/qwen.qwen3-32b-v1:0"
AWS_SECRET_ID = os.getenv("LITELLM_AWS_SECRET_ID") or os.getenv("AWS_SECRET_ID")
AWS_SECRET_KEY = os.getenv("LITELLM_AWS_SECRET_KEY") or os.getenv("AWS_SECRET_KEY_NAME")
AWS_REGION = os.getenv("AWS_REGION")


def get_base_url() -> str:
    base_url = (
        os.getenv("LITELLM_BASE_URL")
        or os.getenv("ANTHROPIC_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    if not base_url:
        sys.exit("Missing LITELLM_BASE_URL or ANTHROPIC_BASE_URL. Source the local credential/config file before probing.")
    return base_url


def get_api_key() -> str | None:
    env_key = (
        os.getenv("ANTHROPIC_AUTH_TOKEN")
        or os.getenv("LITELLM_PROXY_API_KEY")
        or os.getenv("LITELLM_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    if env_key:
        return env_key
    if not AWS_SECRET_ID or not AWS_SECRET_KEY:
        return None

    try:
        import boto3

        session = boto3.Session(
            profile_name=os.getenv("AWS_PROFILE"),
            region_name=AWS_REGION,
        )
        client = session.client("secretsmanager")
        response = client.get_secret_value(SecretId=AWS_SECRET_ID)
        return json.loads(response["SecretString"]).get(AWS_SECRET_KEY)
    except Exception as exc:
        print(f"Warning: could not load LiteLLM key from AWS Secrets Manager: {exc}")
        return None


def resolve_provider_model(model: str) -> tuple[str, str]:
    if "/" in model:
        return tuple(model.split("/", 1))  # type: ignore[return-value]
    provider = "anthropic" if model.startswith("claude") else "openai"
    return provider, model


def main() -> int:
    base_url = get_base_url()
    api_key = get_api_key()
    model = os.getenv("LITELLM_MODEL", DEFAULT_MODEL)

    if not api_key:
        sys.exit(
            "Missing ANTHROPIC_AUTH_TOKEN, LITELLM_PROXY_API_KEY, LITELLM_API_KEY, "
            "OPENAI_API_KEY, or configured AWS Secrets Manager key."
        )

    host = urlparse(base_url).hostname
    try:
        socket.getaddrinfo(host, 443)
    except OSError as exc:
        sys.exit(f"LiteLLM DNS failed for {host}: {exc}")

    provider, model_name = resolve_provider_model(model)
    response = litellm.completion(
        model=f"litellm_proxy/{provider}/{model_name}",
        messages=[{"role": "user", "content": "Reply with exactly PONG."}],
        api_key=api_key,
        base_url=base_url,
        max_tokens=8,
    )

    print(f"LiteLLM proxy reachable: {base_url}")
    print(f"Model: {provider}/{model_name}")
    print("LiteLLM works:", response.choices[0].message.content)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.exit(f"LiteLLM call failed: {exc}")
