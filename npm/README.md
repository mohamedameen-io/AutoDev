# ai-autodev NPM

Node.js wrapper for the [ai-autodev](https://github.com/mohamedameen/autodev) Python package.

## Installation

```bash
npm install -g ai-autodev
```

Or use it directly via `npx`:

```bash
npx ai-autodev --help
```

## Requirements

- Node.js 18+
- Python 3.11+

The npm package will automatically set up a Python virtual environment and install the bundled wheel on first run.

## Commands

```bash
autodev install    # Set up the Python environment
autodev uninstall  # Remove the Python environment
autodev doctor     # Check system requirements
autodev --version  # Show version
```

All other commands proxy to the Python CLI:

```bash
autodev init [--inline] [--platform claude-code|cursor]
autodev plan <task>
autodev execute [--phase <phase>]
autodev resume
autodev status
autodev tournament <task>
```

## Manual Wheel Build

If you're developing the package, build the Python wheel first:

```bash
cd ..
pip wheel . --wheel-dir npm/wheel --no-deps
# or with uv:
uv pip wheel . --dest npm/wheel
```

Then build the npm package:

```bash
npm install
npm run build
npm link  # or: npm publish
```

## Architecture

This npm package:
1. On first run, creates a venv at `~/.config/autodev/venv`
2. Installs the bundled Python wheel into that venv
3. Proxies all commands to the Python `autodev` CLI

This design ensures the Python runtime is managed independently from Node.js.

## License

MIT - see [../LICENSE](../LICENSE)
