# Cloud code baseline — 2026-09-14

This repository starts from the current annotation platform workspace, including
its previously uncommitted preprocessing, backup, export, documentation, and
Tencent Cloud staging deployment changes. It precedes the proposed integration
of crawler scene metadata and source confidence into the annotation website.

The source workspace's preceding commit was
`fa3a959` (`Add quality queue filters`). This is an independent snapshot;
the original workspace retains its existing Git history and branches.

The baseline includes application code, PostgreSQL migrations, dependency locks,
tests, deployment templates, and project documentation. Audio, databases, exported
datasets, generated spreadsheet/archive deliverables, virtual environments, logs,
host configuration, and credentials are not part of the snapshot.

The baseline GitHub repository is private:
<https://github.com/ty4045681/egyptian-arabic-annotation-tool>.

For setup and operation, see [README.md](README.md) and the
[Tencent Cloud staging deployment record](deploy/tencent-staging/README.md).
