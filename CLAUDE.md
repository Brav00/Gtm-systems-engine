# GTM Systems Engineering Repository Rules

- **Architecture:** Use Port & Adapter (Protocol/ABC) boundaries and Pydantic JSON contracts. Never leak raw vendor payloads into core orchestration logic.
- **Secrets:** Never hardcode API keys or commit `.env` files. Read secrets from environment variables or GitHub Secrets.
- **Mobile-Friendly Changes:** Keep edits scoped and modular. Always run `pytest` after modifying logic and summarize exact file changes clearly.
- **Quality Gates:** Every ingestion or audit step must emit structured JSON with explicit pass/fail status and reasons.
