# pyright: strict

from __future__ import annotations

import atexit
import importlib.metadata as importlib_metadata
import logging
import re
import subprocess
import sys

import attrs
import click
import packaging.requirements
from more_itertools import always_iterable
from typing_extensions import IO, TypeAlias

WHITELIST = frozenset(
    {
        "pip",
        "setuptools",
        "wheel",
        "more-iterools",
        "packaging",
        "attrs",
        "click",
    }
)

BASE_PIP_ARGS = [sys.executable, "-m", "pip"]


class _PipRemoveFilter(logging.Filterer):
    def __init__(self, verbose_level: int) -> None:
        self._verbose_level = verbose_level

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno in (logging.DEBUG, logging.WARNING):
            return self._verbose_level >= 2
        elif record.levelno == logging.INFO:
            return self._verbose_level >= 1
        else:
            return True


_log_fileobj: None | IO[str] = None


class _PipRemoveLogger(logging.Logger):
    def __init__(self, quiet: bool = False, verbose_level: int = 1) -> None:
        super().__init__(__name__, logging.NOTSET)
        self.addFilter(_PipRemoveFilter(verbose_level))
        hf = logging.StreamHandler()
        f = logging.Formatter(fmt="[pip-remove] [{levelname}]: {message}", style="{")
        hf.setFormatter(f)
        self.addHandler(hf)
        if _log_fileobj is not None:
            hf = logging.StreamHandler(_log_fileobj)
            hf.setFormatter(f)
            self.addHandler(hf)

        if quiet:
            self.disable()

    def enable(self) -> None:
        if self.disabled:
            self.disabled = False

    def disable(self) -> None:
        if not self.disabled:
            self.disabled = True

    def isEnabledFor(self, level: int) -> bool:  # noqa: ARG002
        return True


_logger: _PipRemoveLogger | None = None


def setup_logging(quiet: bool, verbosity: int, logfile: IO[str] | None = None) -> None:
    global _logger, _log_fileobj
    _logger = _PipRemoveLogger(quiet, verbosity)
    if logfile:
        _log_fileobj = logfile
        atexit.register(_log_fileobj.close)


@attrs.define
class PackageNotFound(Exception):
    pkg: str

    def __str__(self) -> str:
        return "Package %r not found." % self.pkg


_DistributionDict: TypeAlias = dict[str, importlib_metadata.Distribution]
__dists: _DistributionDict | None = None
__alias_to_names: dict[str, str] = {}
__pip_version__: str | None = None


def pip_version() -> str:
    global __pip_version__
    if __pip_version__ is None:
        from pip import __version__

        __pip_version__ = __version__
    return __pip_version__


def distributions_as_dict() -> _DistributionDict:
    global __dists
    if __dists is None:
        dists = importlib_metadata.distributions()
        __dists = {d.name: d for d in dists}
        if _logger:
            _logger.debug(f"{len(__dists)} packages found.")
    return __dists


def get_distribution(name: str) -> importlib_metadata.Distribution:
    dists = distributions_as_dict()
    try:
        if _logger:
            _logger.debug(f"Attempting to package {name}")
        d = dists[name]
    except KeyError:
        try:
            name = __alias_to_names[name]
            if _logger:
                _logger.debug(f"True package name is {name}")
        except KeyError:
            d = importlib_metadata.distribution(name)
            __alias_to_names[name] = d.name
        else:
            d = dists[name]
    if _logger:
        _logger.debug(f"Got package {d.name} version {d.version}")
    return d


def does_pkg_exists(pkg: str) -> bool:
    """Verify the `pkg` is exists within this environment."""
    try:
        get_distribution(pkg)
        return True
    except importlib_metadata.PackageNotFoundError:
        if _logger:
            _logger.debug(f"cannot found {pkg}")
        return False


def execpip(*args: str) -> None:
    """Execute pip with `args`."""
    if _logger:
        _logger.debug(f"pip version: {pip_version()}")
    out = subprocess.run([*BASE_PIP_ARGS, *args], check=False)
    if _logger:
        _logger.debug(f"exit code is {out.returncode}")


def choice(message: str) -> bool:
    CHOICE_YES_REGEX = re.compile(r"^([y]|yes)$", re.DOTALL | re.IGNORECASE)
    CHOICE_NO_REGEX = re.compile(r"^([n]|no)$", re.DOTALL | re.IGNORECASE)
    ANSWERS = {CHOICE_YES_REGEX: True, CHOICE_NO_REGEX: False}

    message = f"{message} (y/n/yes/no): "
    while True:
        if answer := input(message).strip():
            for pat, result in ANSWERS.items():
                if pat.match(answer):
                    return result
            print("error: not a choice, must be y, n, yes or no")


def get_requirements(
    dist: importlib_metadata.Distribution,
) -> tuple[packaging.requirements.Requirement, ...]:
    return tuple(
        map(packaging.requirements.Requirement, always_iterable(dist.requires))
    )


@attrs.define(kw_only=True)
class DependencyData:
    target: str
    whitelisted: set[tuple[str, str]] = attrs.field(factory=set, init=False)
    # pkg: deps
    safe_to_removed: dict[str, set[str]] = attrs.field(factory=dict, init=False)
    # pkg: {pkg_dep: mods}
    package_depenencies_required_by: dict[str, dict[str, set[str]]] = attrs.field(
        factory=dict, init=False
    )
    this_requires_by: set[str] = attrs.field(factory=set, init=False)
    # {(mod, dep), ...}
    never_installed: set[tuple[str, str]] = attrs.field(factory=set, init=False)
    analyzed_packages: set[str] = attrs.field(factory=set, init=False)


class PackageDependencyUninstalltionResolver:
    # safe to removed
    # itself a dependency of
    # or a package's dependency is used on other packages.

    def __init__(self, *, target: str) -> None:
        if not does_pkg_exists(target):
            raise ValueError(f"Package '{target}' not found")
        self._depenency_data = DependencyData(target=target)

    @property
    def depenency_data(self) -> DependencyData:
        return self._depenency_data

    def _analyze_package_dependencies(self, package: str):
        """Analyze a single `package`."""
        # do not check if...
        if package in WHITELIST or package in self.depenency_data.analyzed_packages:
            if _logger:
                _logger.debug(f"{package} already analyzed, skipping...")
            return

        # else...

        package_to_requirements = {
            mod: tuple(d.name for d in get_requirements(dist))
            for mod, dist in distributions_as_dict().items()
        }

        if _logger:
            _logger.debug(f"pulling requirements from {package}")

        try:
            requirements = package_to_requirements[package]
        except KeyError:
            package = get_distribution(package).name
            try:
                requirements = package_to_requirements[package]
            except KeyError:
                raise RuntimeError("we fail again") from None

        if _logger:
            _logger.debug(f"got {len(requirements)} depenencies")

        self.depenency_data.safe_to_removed.setdefault(package, set())
        self.depenency_data.package_depenencies_required_by.setdefault(package, {})

        vaild_requirements: set[str] = set()
        for entry in requirements:
            if not does_pkg_exists(entry):
                self.depenency_data.never_installed.add((package, entry))
            elif entry in WHITELIST:
                self.depenency_data.whitelisted.add((package, entry))
            else:
                vaild_requirements.add(entry)

        if _logger:
            _logger.debug("analyzing depenencies...")

        if package == self.depenency_data.target:
            for package_to_check, requirements in package_to_requirements.items():
                if (
                    package in requirements
                    and package_to_check != package
                    and package_to_check not in WHITELIST
                ):
                    self.depenency_data.this_requires_by.add(package_to_check)

        for depenency in vaild_requirements:
            for package_to_check, requirements in package_to_requirements.items():
                if (
                    depenency in requirements
                    and package_to_check != package
                    and package_to_check not in vaild_requirements
                    and package_to_check not in WHITELIST
                    and package_to_check != self.depenency_data.target
                ):
                    (
                        self.depenency_data.package_depenencies_required_by[package]
                        .setdefault(depenency, set())
                        .add(package_to_check)
                    )

        self.depenency_data.safe_to_removed[package] = {
            d
            for d in vaild_requirements
            if d not in self.depenency_data.package_depenencies_required_by[package]
        }

        self.depenency_data.analyzed_packages.add(package)

        if _logger:
            _logger.debug(f"analyzed {package}")

    def analyze_recursively(self) -> bool:
        """Analyze a `target` in `DependencyData` recursively."""
        target = self.depenency_data.target
        self._analyze_package_dependencies(target)
        to_removed = self.depenency_data.safe_to_removed.get(target, set())
        if not to_removed:
            if _logger:
                _logger.info("No package could not be found to be removed...")
            return False

        for t in to_removed:
            self._analyze_package_dependencies(t)

        for i in self.depenency_data.this_requires_by.copy():
            for mdep, mdep_deps in self.depenency_data.safe_to_removed.items():
                if i in mdep_deps:
                    if _logger:
                        _logger.warning(
                            f"NOTICE: {i} used {target} but also a dependency of {mdep} (which is a dependency of {target})"
                        )
                    self.depenency_data.this_requires_by.remove(i)
        return True


class PipRemoveCLI:
    def __init__(self) -> None:
        self._yes = None
        self._quiet = None
        self._verbose = None

    def _print_results(self, resolver: PackageDependencyUninstalltionResolver) -> None:
        """Print the results of the analysis."""

        def format_indent(level: int) -> str:
            return (" " * (level - 2)) + "- "

        this_requires_by = resolver.depenency_data.this_requires_by
        if this_requires_by and _logger:
            print("The following packages used this as a dependency:")
            for i in sorted(this_requires_by):
                print(format_indent(4) + i)
        never_installed = resolver.depenency_data.never_installed
        if never_installed and _logger:
            print(
                "The following dependencies of this modules/packages is not installed:"
            )
            for mod, dep in never_installed:
                print(format_indent(4) + f"{dep} --> a dependency of {mod}")
        package_dep_requird_by = resolver.depenency_data.package_depenencies_required_by
        if package_dep_requird_by and _logger:
            doit = False
            for _, deps in package_dep_requird_by.items():
                if deps:
                    doit = True
                    break
            if doit:
                print(
                    "The following dependencies of this module/package is used on other modules/packages:"
                )
                for mod, deps in package_dep_requird_by.items():
                    for mdep, used in deps.items():
                        print(
                            format_indent(4)
                            + f"{mdep} (a dependency of {mod}) --> used by {', '.join(used)}"
                        )
        wl = resolver.depenency_data.whitelisted
        if wl and _logger:
            print("The following dependencies of this module/package is whitelisted:")
            for mod, dep in wl:
                print(format_indent(4) + f"{dep} (a dependency of {mod})")
        removed = resolver.depenency_data.safe_to_removed
        if removed and _logger:
            doit = False
            for _, deps in removed.items():
                if deps:
                    doit = True
                    break
            if doit:
                print(
                    "The following dependencies of this module/package will be REMOVED:"
                )
                for mod, deps in removed.items():
                    for dep in deps:
                        print(format_indent(4) + f"{dep} (a dependency of {mod})")

    def _remove_packages(
        self, resolver: PackageDependencyUninstalltionResolver
    ) -> None:
        uninstalled: set[str] = set()
        for deps in resolver.depenency_data.safe_to_removed.values():
            uninstalled.update(deps)
        uninstalled.add(resolver.depenency_data.target)
        if _logger:
            _logger.info("uninstalling packages...")
        base = ["uninstall", "--yes"]
        if self._quiet:
            base.append("--quiet")
        execpip(*base, *uninstalled)

    def _main(self, resolver: PackageDependencyUninstalltionResolver) -> None:
        rv = resolver.analyze_recursively()
        if not rv:
            return
        self._print_results(resolver)
        if self._quiet or self._yes:  # noqa: SIM108
            yes = True
        else:
            yes = choice("Continue to uninstall?")

        if yes:
            self._remove_packages(resolver)

    def main(
        self,
        package: tuple[str, ...],
        yes: bool,
        quiet: bool,
        verbose: int,
        log_file: IO[str],
    ) -> int:
        args = package
        self._quiet = quiet
        self._verbose = verbose
        self._yes = yes
        setup_logging(quiet, verbose, log_file)
        for t in args:
            resolver = PackageDependencyUninstalltionResolver(target=t)
            self._main(resolver)
        return 0


@click.command()
@click.argument("package", nargs=-1)
@click.option("-y", "--yes", is_flag=True, help="Auto-confirm uninstalltion")
@click.option("-q", "--quiet", is_flag=True, help="Suppress output")
@click.option("-v", "--verbose", help="Increase output", count=True)
@click.option("--log-file", type=click.File(mode="w"))
def main(
    package: tuple[str, ...],
    yes: bool,
    quiet: bool,
    verbose: int,
    log_file: IO[str],
) -> int:
    """Uninstall PACKAGE recursively."""
    cli = PipRemoveCLI()
    return cli.main(package, yes, quiet, verbose, log_file)


if __name__ == "__main__":
    sys.exit(main())
