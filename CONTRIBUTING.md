# Contributing to application-twin-studio

Thanks for taking the time to contribute! This guide explains how to propose changes.

By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting bugs and requesting features

- Search [existing issues](https://github.com/karuppusamym/application-twin-studio/issues) first to avoid duplicates.
- For bugs, include steps to reproduce, what you expected, what happened, and your environment
  (OS, runtime versions, relevant configuration).
- For features, describe the problem you are trying to solve before the solution you have in mind.
- Do **not** report security vulnerabilities in public issues. Contact the maintainer privately
  via [GitHub](https://github.com/karuppusamym) instead.

## Making changes

1. Fork the repository and create a branch from `main`:
   ```bash
   git checkout -b feat/short-description main
   ```
2. Follow the setup steps in the [README](README.md).
3. Keep changes focused: one logical change per pull request.
4. Add or update tests for any behaviour you change.
5. Update documentation when you change behaviour, configuration or APIs.
6. Run the checks locally before pushing:
   ```bash
   pip install -r requirements.txt   # or the setup described in the README
   pytest
   ```

## Commit messages

Use short, imperative summaries, ideally following [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add export to CSV
fix: handle empty result sets
docs: clarify local setup
```

## Pull requests

- Target the `main` branch.
- Describe **what** changed and **why**, and link related issues (`Closes #123`).
- Make sure CI passes and resolve review comments before requesting another review.
- Never commit secrets, credentials, `.env` files or real customer data.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE) that covers this project.
