set dotenv-load
export PYTHONPATH := justfile_directory() / "src"
version := `python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'`

default:
    @just --list

# Show the native subprocess and forked Python worker trees through zpg.
run:
    zpg --renderer tree python -u examples/mock.py

# Run standard-library tests. Extra arguments go to unittest discovery.
[positional-arguments]
test *args:
    python -m unittest discover -s tests -v "$@"

# Build the source archive and wheel with the tools from the Nix shell.
build:
    python -m build --no-isolation

# Validate only the current release files, including the README rendering.
check-dist:
    twine check --strict dist/zppy-{{ version }}.tar.gz dist/zppy-{{ version }}-py3-none-any.whl

# Upload the checked release files to PyPI with PYPI_TOKEN from the environment.
publish: check-dist
    @TWINE_PASSWORD="${PYPI_TOKEN:?Set PYPI_TOKEN in .env or the environment}" twine upload --non-interactive --repository-url https://upload.pypi.org/legacy/ --username __token__ dist/zppy-{{ version }}.tar.gz dist/zppy-{{ version }}-py3-none-any.whl

check: lint typos test
    ruff format --check .
    nixfmt --check flake.nix
    just --fmt --check

lint:
    ruff check .

typos:
    typos

fmt:
    ruff format .
    nixfmt flake.nix
    just --fmt

hooks:
    pre-commit run --all-files

# Check the package and hooks in the Nix build sandbox.
check-nix:
    nix flake check

# Create a commit with a Conventional Commits message.
commit:
    cz commit
