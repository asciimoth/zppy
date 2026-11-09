export PYTHONPATH := justfile_directory() / "src"

default:
    @just --list

# Run Python with access to the local package, e.g. just run -c 'import zppy'.
[positional-arguments]
run *args:
    python "$@"

# Run standard-library tests. Extra arguments go to unittest discovery.
[positional-arguments]
test *args:
    python -m unittest discover -s tests -v "$@"

# Build the source archive and wheel with the tools from the Nix shell.
build:
    python -m build --no-isolation

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
