# Pull Request Checklist

## Scope

- [ ] User-visible behavior changed
- [ ] Documentation updated
- [ ] Tests added/updated
- [ ] No behavior changes (docs/process only)

## Description

1. Why this change is needed.
2. Files changed and impact.
3. Verification commands run.

## Pre-merge checks

- [ ] `ruff check src tests`
- [ ] `pytest -q`
- [ ] If protocol files changed: `./scripts/gen_protocol.sh` was run (if applicable)

## User impact

- [ ] Backward compatible for existing `/codex` workflows
- [ ] No migration needed
- [ ] New onboarding/doc workflow documented

## Risks

- What may break?
- What is the rollback plan?
