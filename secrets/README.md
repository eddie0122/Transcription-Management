# Secrets

Create the LLM API key file before `docker compose up` (Compose mounts it at
`/run/secrets/llm_api_key`):

```bash
mkdir -p secrets
printf '%s' 'TBA' > secrets/llm_api_key.txt
chmod 600 secrets/llm_api_key.txt
```

`TBA` is a placeholder only — replace it with the real credential before
enabling translation. The file is excluded from source control by
`.gitignore`; protect it on the host. Keys entered later in the UI are stored
in the application's protected credential store (macOS Keychain natively; a
0600-permission file store inside the `app_data` volume in Docker), never in
frontend bundles or logs.
