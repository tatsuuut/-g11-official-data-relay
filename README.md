# G11 Official Data Relay

This public repository contains only the scheduler and safety checks used to
start the private G11 runtime on GitHub-hosted runners.

It intentionally contains no prediction logic, coefficients, model files,
race predictions, runtime state, credentials, or purchase/notification logic.

## Security boundary

- The private runtime is checked out at run time with a fine-grained,
  repository-scoped token stored as `G11_PRIVATE_REPO_TOKEN`.
- The token must have read-only Contents access to only
  `tatsuuut/boat-brain-lab`.
- Workflows never run on `pull_request` or `pull_request_target`.
- Runtime state may leave the runner only after encryption by the private
  runtime.
- The public-surface audit runs before any private checkout.
- Logs must not print environment variables, tokens, source files, model files,
  or decrypted state.

## Required repository configuration

1. Create this repository as public under `tatsuuut` with the exact name
   `-g11-official-data-relay`.
2. Add the Actions secret `G11_PRIVATE_REPO_TOKEN` using a fine-grained token
   limited to `tatsuuut/boat-brain-lab`, Contents: read-only.
3. Add the Actions secret `G11_RELAY_STATE_KEY` using a random 32-byte value.
4. Keep the default branch named `main`.
5. Do not enable workflows from forks and do not add a
   `pull_request_target` trigger.
6. Set the Actions variable `G11_RELAY_ENABLED=true` only after the private
   entrypoint and both required secrets have passed their connection test.

The scheduled job deliberately fails closed until both required secrets and
the private `g11.relay.v1` entrypoint are available.
