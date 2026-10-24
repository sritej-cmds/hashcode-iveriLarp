# Execution Safety and Finding Validation

## Authorized targets only

- Use local benchmark instances or other explicitly authorized systems.
- Maintain an explicit target allowlist.
- Reject candidates or configurations that attempt to change the approved host.
- Do not allow the LLM to choose the target, credentials, or execution limits.
- Use synthetic accounts and synthetic data for security validation.
- Do not run the benchmark against production systems without explicit authorization and a separately approved test plan.

## Budget enforcement

Enforce the following outside the LLM:
- Maximum request count.
- Maximum wall-clock duration.
- Per-request timeout.
- Bounded LLM latency, retries, and output size.
- A stop mechanism for interrupted or over-budget runs.

Record why execution stopped.

## Secrets

- Never commit API keys, tokens, passwords, or private credentials.
- Keep local secrets in an ignored environment file or an approved secret manager.
- Commit only placeholder names in `.env.example`.
- Never include secrets in prompts, model output, debug logs, or benchmark reports.

## Finding classification

### Lead

An observation that may warrant investigation but has not demonstrated a security issue.

### Needs review

Evidence is suggestive but does not meet the confirmation criteria.

### Confirmed

Reproducible evidence demonstrates a security impact that violates the expected authorization or security policy.

### Rejected

Review demonstrates that the observation is expected behavior, a duplicate, or unsupported by evidence.

## HTTP response rules

- HTTP 500 alone is not proof of a vulnerability.
- HTTP 401 or 403 alone is not proof of a vulnerability.
- HTTP 200 alone is not proof of a vulnerability.
- A timeout or crash is a lead requiring investigation, not automatic confirmation.

## Authorization testing

For a BOLA/IDOR-style test, use controlled test identities and synthetic resources. Confirm that one identity can access or modify a resource belonging to another identity contrary to the expected authorization policy.

Preserve:
- Test identities used.
- Request sequence.
- Relevant sanitized request and response evidence.
- Expected policy.
- Reproduction instructions.
- Reviewer and validation notes.

## Evidence handling

- Associate evidence with a run ID, candidate ID, and finding ID.
- Remove secrets and sensitive data before saving artifacts.
- Preserve enough sanitized evidence for reproducibility.
- Keep unconfirmed leads separate from confirmed findings.

## Stop conditions

Stop execution when the budget is exhausted, the target becomes unhealthy, authorization is unclear, or an unexpected safety issue occurs.