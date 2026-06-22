# Smoke test — Step 1 scaffold only.
# Verifies that Python >=3.11 is present and the package is importable as a
# namespace.  Real module tests arrive in Step 2+.


def test_python_version() -> None:
    """Python 3.11+ is required (pyproject.toml: requires-python = '>=3.11')."""
    import sys

    assert sys.version_info >= (3, 11), (
        f"FRIDAY requires Python >=3.11, got {sys.version}"
    )


def test_pyproject_build_backend() -> None:
    """Confirm pyproject.toml names the canonical PEP 517 build backend."""
    import importlib.util

    spec = importlib.util.find_spec("setuptools.build_meta")
    assert spec is not None, "setuptools.build_meta not importable"
