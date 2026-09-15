# Development Guidelines

## Approach

- Follow the user's instructions and applicable repository conventions.
- Read relevant docs, configuration, code, and tests before editing.
- Discover the stack and commands from the repository; do not assume them.
- Inspect the working tree and preserve unrelated user changes.
- Investigate root causes before fixing symptoms.
- Resolve routine choices independently; ask only when ambiguity materially matters.
- Plan substantial work briefly, then implement and verify it through completion.

## Implementation

- Make the smallest complete change that satisfies the request.
- Match existing naming, formatting, architecture, and error-handling patterns.
- Prefer clear code and cohesive functions; reuse established utilities.
- Avoid speculative features, unnecessary abstractions, and unrelated refactoring.
- Preserve public contracts unless a change is required; explain compatibility impacts.
- Comment on intent and constraints, not obvious code behavior.
- Keep types and checks meaningful; do not suppress errors to force a passing build.
- Use the existing package manager and keep lockfiles consistent.
- Add dependencies only for a clear benefit; consider maintenance and compatibility.
- Keep environment-specific values configurable and document required settings.

## Reliability and Security

- Validate untrusted inputs and report actionable errors; never hide failures.
- Use appropriate timeouts, bounded retries, cancellation, and resource cleanup.
- Retry state-changing operations only when duplicate effects are handled safely.
- Protect data integrity with transactions or atomic writes where appropriate.
- Consider concurrency, partial failure, and recovery for affected workflows.
- Never commit or expose secrets, credentials, or sensitive user data.
- Use safe queries, process invocation, and file-path handling.
- Preserve authentication, authorization, and least-privilege boundaries.
- Do not send private data to external services without authorization.
- Optimize demonstrated bottlenecks and measure meaningful performance changes.

## Verification

- Run focused, meaningful checks first; broaden for concrete risks or required gates.
- Use repository test, lint, type-check, format, and build commands where applicable.
- Add regression coverage when it meaningfully captures a bug or behavioral change.
- Test contracts and failure paths, not copies of implementation logic.
- Never weaken assertions or remove checks merely to make tests pass.
- Distinguish new failures from pre-existing ones and report both accurately.
- Verify UI states, keyboard access, and responsive layouts when changing interfaces.
- Check affected API consumers when request or response contracts change.
- State what actually ran; distinguish mocked, integration, and end-to-end verification.
- Disclose unavailable environments and unverified behavior without claiming success.

## Git and Operations

- Keep diffs focused; avoid unrelated formatting and unnecessary generated files.
- Do not discard user work, rewrite shared history, or destroy data without authorization.
- Commit, push, publish, or deploy only when requested or authorized by the workflow.
- Confirm required checks before production-affecting or irreversible actions.
- Prepare reviewable work before seeking necessary approval; do not ask repeatedly.
- Verify external action results before claiming success.

## Documentation and Delivery

- Update docs when setup, configuration, commands, interfaces, or behavior changes.
- Keep examples accurate and clearly label proposed or unimplemented interfaces.
- Share concise progress updates during longer tasks, emphasizing findings and blockers.
- Review the final diff for accidental edits, exposed secrets, and needless complexity.
- Finish with what changed, what was verified, and any limitation or remaining action.
- Complete necessary work; if blocked, explain the blocker and what remains unfinished.
