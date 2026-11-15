import pytest

from planbak.codec import BackupError
from planbak.navigation import DOMAIN_GROUPS, Location, split_domain


@pytest.mark.parametrize("family", DOMAIN_GROUPS)
def test_domain_split_parent_and_addresses(family):
    domain = family + "-com.example.with-hyphen"
    assert split_domain(domain) == (family, "com.example.with-hyphen")
    location = Location(domain, "Library/Preferences")
    assert location.address == f"/{family}/com.example.with-hyphen/Library/Preferences"
    assert location.parent == Location(domain, "Library")
    assert location.parent.parent.parent == Location(group=family)
    assert Location.from_address(location.address, [domain]) == location
    assert Location.from_address(f"/{domain}/Library/Preferences", [domain]) == location
    assert Location.from_address(f"/{family}", [domain]) == Location(group=family)
    assert Location(group=family).parent.is_root


@pytest.mark.parametrize("domain", ["HomeDomain", "UnknownDomain-with-hyphens", "AppDomain-"])
def test_unknown_names_are_not_split(domain):
    assert split_domain(domain) == (None, domain)
    assert Location(domain).parent.is_root
    assert Location.from_address("/" + domain, [domain]) == Location(domain)


def test_invalid_virtual_addresses():
    domains = ["AppDomain-com.example", "HomeDomain"]
    for path in [
        "/AppDomain/missing",
        "/AppDomain/com.example/../bad",
        "/Unknown",
        "/HomeDomain/../x",
    ]:
        with pytest.raises(BackupError):
            Location.from_address(path, domains)
