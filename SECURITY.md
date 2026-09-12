# Security policy

Do not commit any of the following:

- P3 / PL120 / Abeken source or coefficients
- TRUE AI source, model files, feature schemas, or SHAP output
- morning, predeadline, or night prediction payloads
- decrypted runtime state
- GitHub tokens, cache passphrases, cookies, or credentials
- exported Actions logs or artifacts

The only accepted public payloads are scheduler configuration, integrity
checks, and documentation. Suspected exposure requires disabling the workflow
and rotating the repository-scoped credential; encrypted state is then rebuilt
from the next morning acquisition.
