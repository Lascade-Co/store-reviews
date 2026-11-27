# Store Reviews — Centralised Review System

Collects App Store + Google Play reviews for every app in `apps.json`, drafts
replies with **RAG + Codex**, publishes them to a **web dashboard**, and sends
approved replies back to the store. No server, no database — **GitHub Actions +
Cloudflare R2**.

## Documentation

| Doc | What it covers |
|---|---|
| **[review.md](review.md)** | **The one document to read.** Onboarding, day-to-day use, both workflows, how RAG works, state management, the architecture, secrets, troubleshooting. Start here. |
| [r2-path.md](r2-path.md) | Every R2 path (dashboard files, state, corpus pending, LanceDB) + the corpus columns. |
| [docs/review-api-responses.md](docs/review-api-responses.md) | App Store + Google Play API response shapes. |
| [AGENTS.md](AGENTS.md) · [CLAUDE.md](CLAUDE.md) | Repo conventions and the sealed-output rules. |

## Quick facts

- **Add an app:** add an object to `apps.json` + create its Infisical `/reviews` folder. First run baselines it.
- **State** lives on R2 (`<DATA_PREFIX>/state/<appcode>/<provider>.json`), not git — ETag conditional puts, merge on conflict.
- **Replies** go out via `reply-review.yml`, dispatched from the dashboard (fine-grained PAT).
- **Tests:** `PYTHONPATH=scripts python3 -m unittest discover -s tests -v` (the workflow does not run them).

> [!NOTE]
> CI output is sealed: job logs and artifacts are encrypted to the repo key ([ADR 0014](https://github.com/Lascade-Co/actions/blob/main/docs/adr/0014-keep-the-runner-public-and-seal-private-output.md)).
> Read runs at https://ci.lascade.com/?repo=store-reviews; workflow rules: [docs/sealed-output.md](https://github.com/Lascade-Co/actions/blob/main/docs/sealed-output.md).
</content>
