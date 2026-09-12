# Security policy

Do not commit any of the following:

- P3 / PL120 / Abeken source or coefficients
- TRUE AI source, model files, feature schemas, or SHAP output
- morning, predeadline, or night prediction payloads
- decrypted runtime state
- GitHub tokens, encryption keys, cookies, or credentials
- exported Actions logs or artifacts

The only accepted public payloads are scheduler configuration, integrity
checks, and documentation. Suspected exposure requires disabling the workflow,
revoking the affected credential, and rotating the relay state key.
