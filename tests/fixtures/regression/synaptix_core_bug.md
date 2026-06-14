## Summary

The Synaptix demo email-draft workflow (`emma.workflows.email_assistant`) fails during the **first step** (`fetch_unread`) when `LLM_PROVIDER=mistral`. The terminal error is Mistral HTTP **429 `rate_limited` (code 1300)** on `POST /v1/chat/completions` — not a workflow timeout.

Gmail access succeeds; the failure occurs on a subsequent Mistral chat completion inside the `ToolingAgent.run` → LangChain `AgentExecutor` path.

## Impact

- Demo email drafting cannot proceed past unread fetch (triage, KB lookup, compose, and draft steps are never reached).
- Reproduces with a connected mailbox / real `principal_id` (`demo/test_mistral_tooling_fetch.py --principal-id`, `DemoApplication.run_email_draft_workflow`).
- Isolated `get_llm(tier="sub").invoke()` smoke/stress tests pass — the API key and LLM factory are not the root cause.

## Reproduction

```bash
# Demo path
LLM_PROVIDER=mistral demo with connected mailbox → fails at fetch_unread

# Minimal probe
demo/test_mistral_tooling_fetch.py --principal-id <uuid>  # fails 429 after successful fetch

# Control (passes)
demo/test_mistral_llm.py
demo/test_mistral_tooling_fetch.py  # without --principal-id
```

**Observed error:**
```
Error executing tooling task: Error response 429 while fetching https://api.mistral.ai/v1/chat/completions: {"type":"rate_limited","code":"1300", ...}
```
