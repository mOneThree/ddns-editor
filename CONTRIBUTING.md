# Contributing to DDNS Editor

First off, thank you for considering contributing to DDNS Editor! It's people like you that make this tool better for everyone.

## How Can I Contribute?

### Reporting Bugs

If you find a bug, please check the existing issues to see if it has already been reported. If not, open a new issue using the **Bug Report** template. Include as much detail as possible:
* Steps to reproduce the bug
* Expected behavior vs. actual behavior
* Your environment (OS, Docker version, browser)
* Relevant logs (redact any sensitive tokens/passwords)

### Suggesting Enhancements

Have an idea for a new feature or a way to improve the UI? 
1. Check the `ENHANCEMENT_BACKLOG.md` and `PROVIDER_BACKLOG.md` to see if it's already planned.
2. If it's new, open an issue using the **Feature Request** template to discuss it before you start writing code.

### Adding New Providers

If you want to add support for a provider currently listed in `PROVIDER_BACKLOG.md` (or a new one supported by `ddns-updater`):
1. Review how existing providers are implemented in `app/app.py` (look at the `PROVIDER_SCHEMAS` dictionary).
2. Update the frontend form logic in `app/templates/index.html` to handle the new provider's fields.
3. If the provider has an API that allows testing credentials without making a DNS change, consider adding a `test_connection` implementation in `app/app.py`.

### Pull Requests

1. **Fork the repository** and create your branch from `main`.
2. **Install dependencies**: `pip install -r requirements.txt -r requirements-dev.txt`
3. **Make your changes**. Keep commits focused and provide clear commit messages.
4. **Run the tests**: Ensure all tests pass by running `pytest tests/ -v`. If you add new functionality, please add tests for it.
5. **Update documentation**: If you change configuration options or features, update the `README.md` accordingly.
6. **Submit a Pull Request**: Use the provided PR template.

## Development Setup

See the "Building locally" section in the `README.md` for instructions on how to run the app locally using Docker or directly with Python.

## Code Style

* We use standard Python formatting. Please ensure your code is clean and readable.
* The frontend uses Bootstrap 5; try to stick to existing UI patterns and classes for consistency.

## Code of Conduct

Please note that this project is released with a [Contributor Code of Conduct](CODE_OF_CONDUCT.md). By participating in this project you agree to abide by its terms.
