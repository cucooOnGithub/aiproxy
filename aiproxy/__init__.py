"""aiproxy — anonymizing reverse proxy for LLM APIs."""

# Version comes from the latest git tag via setuptools-scm, written at
# build/install time into _version.py. Fall back to installed package
# metadata (for a normal install), then to a dev placeholder when running
# from a source tree that was never built or installed.
try:
    from aiproxy._version import version as __version__
except Exception:  # noqa: BLE001
    try:
        from importlib.metadata import PackageNotFoundError, version as _pkg_version
        try:
            __version__ = _pkg_version("aiproxy")
        except PackageNotFoundError:
            __version__ = "0.0.0+unknown"
    except Exception:  # noqa: BLE001
        __version__ = "0.0.0+unknown"
