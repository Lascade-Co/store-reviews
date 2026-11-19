> [!WARNING]
> **This repository is frozen: private, archived, and its GitHub Actions are stopped.**
>
> It was public, and GitHub makes every Actions log and artifact of a public
> repository readable by anyone. These workflows were leaking private data
> (business numbers, internal reports, credentials-adjacent output) through
> their run logs, so on 2026-11-20 the repository was made private and
> archived, which stops all workflow runs and write access.
>
> **Do not unarchive it or make it public again until its workflows are
> sealed** the way [Lascade-Co/actions](https://github.com/Lascade-Co/actions)
> is: every job's output and every artifact encrypted with the repo key, and
> a policy test that keeps it that way. See
> [ADR 0014](https://github.com/Lascade-Co/actions/blob/main/docs/adr/0014-keep-the-runner-public-and-seal-private-output.md)
> and [docs/sealed-output.md](https://github.com/Lascade-Co/actions/blob/main/docs/sealed-output.md).
>
> To restore: seal every workflow, add the `REPO_AGE_KEY` secret, unarchive,
> re-enable Actions, and only then make the repository public.

