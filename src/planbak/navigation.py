"""Virtual backup locations and per-view navigation state, independent of persistence."""

from __future__ import annotations

from dataclasses import dataclass

from .codec import BackupError, safe_path

DOMAIN_GROUPS = (
    "AppDomain",
    "AppDomainGroup",
    "AppDomainPlugin",
    "SysContainerDomain",
    "SysSharedContainerDomain",
)


def split_domain(domain: str) -> tuple[str | None, str]:
    """Only split known container families; hyphens in identifiers remain intact."""
    for group in DOMAIN_GROUPS:
        prefix = group + "-"
        if domain.startswith(prefix) and len(domain) > len(prefix):
            return group, domain[len(prefix) :]
    return None, domain


@dataclass(frozen=True)
class Location:
    domain: str | None = None
    path: str = ""
    group: str | None = None

    @property
    def is_root(self):
        return self.domain is None and self.group is None

    @property
    def address(self):
        if self.domain is not None:
            group, identifier = split_domain(self.domain)
            base = f"{group}/{identifier}" if group else self.domain
            return "/" + base + ("/" + self.path if self.path else "")
        return "/" + (self.group or "")

    @property
    def parent(self):
        if self.domain is not None:
            if self.path:
                return Location(self.domain, self.path.rpartition("/")[0])
            group, _ = split_domain(self.domain)
            return Location(group=group)
        return Location()

    @classmethod
    def from_address(cls, address: str, domains: list[str]):
        text = address.strip("/")
        if not text:
            return cls()
        first, _, rest = text.partition("/")
        # Accept original domain addresses too; display them canonically on entry.
        if first in domains:
            safe_path(rest)
            return cls(first, rest)
        if first in DOMAIN_GROUPS and any(split_domain(d)[0] == first for d in domains):
            if not rest:
                return cls(group=first)
            identifier, _, path = rest.partition("/")
            domain = first + "-" + identifier
            if domain in domains:
                safe_path(path)
                return cls(domain, path)
        raise BackupError(english="Enter a valid folder path")


RowIdentity = tuple[str, ...]


@dataclass(frozen=True)
class ViewState:
    selected: tuple[RowIdentity, ...]
    current: RowIdentity | None
    current_row: int
    anchor: RowIdentity | None
    anchor_row: int
    anchor_offset: int
    vertical: int
    horizontal: int
    loaded: int
    layout: tuple
    had_focus: bool
