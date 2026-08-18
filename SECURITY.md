# Security and privacy

GitHub Radar is intentionally read-only.

- It queries only public repository metadata through GitHub REST `GET`, an internally constructed GraphQL `query`, and public GitHub Trending pages.
- It rejects GraphQL mutations and GitHub API endpoints outside its small allowlist.
- It never reads, prints, copies, or stores GitHub tokens. Authentication is delegated to the existing `gh` login.
- It stores only public repository metadata and Star observations in a local SQLite database.
- It does not send reports or snapshots to third-party services.

Please report a vulnerability through GitHub's private vulnerability reporting feature. Do not include credentials in a report.
