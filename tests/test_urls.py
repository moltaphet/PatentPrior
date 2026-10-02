"""Domain-spoofing, scheme, port and malformed-input defences (check_url view)."""

import pytest

from conftest import CONTRACT

VALID = [
    ("https://arxiv.org/abs/1801.00001", "https://arxiv.org/abs/1801.00001"),
    ("HTTPS://ARXIV.ORG/abs/1801.00001", "https://arxiv.org/abs/1801.00001"),
    ("https://arxiv.org:443/abs/1", "https://arxiv.org/abs/1"),
    ("https://export.arxiv.org/abs/1801.00001", "https://export.arxiv.org/abs/1801.00001"),
    ("https://a.b.arxiv.org/x", "https://a.b.arxiv.org/x"),
    ("https://arxiv.org/abs/1801.00001#sec2", "https://arxiv.org/abs/1801.00001"),
    ("https://arxiv.org", "https://arxiv.org"),
    ("https://arxiv.org?x=1", "https://arxiv.org?x=1"),
    ("https://patents.google.com/patent/US1234567A/en", "https://patents.google.com/patent/US1234567A/en"),
    ("https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2018", "https://patentscope.wipo.int/search/en/detail.jsf?docId=WO2018"),
    ("https://worldwide.espacenet.com/patent/search?q=pn%3DEP1", "https://worldwide.espacenet.com/patent/search?q=pn%3DEP1"),
    ("https://ieeexplore.ieee.org/document/8000000", "https://ieeexplore.ieee.org/document/8000000"),
    ("https://dl.acm.org/doi/10.1145/3000000", "https://dl.acm.org/doi/10.1145/3000000"),
    ("https://doi.org/10.1000/xyz123", "https://doi.org/10.1000/xyz123"),
    ("https://datatracker.ietf.org/doc/rfc9000", "https://datatracker.ietf.org/doc/rfc9000"),
    ("https://www.rfc-editor.org/rfc/rfc9000", "https://www.rfc-editor.org/rfc/rfc9000"),
    ("https://www.w3.org/TR/webauthn-2/", "https://www.w3.org/TR/webauthn-2/"),
    ("https://eprint.iacr.org/2019/001", "https://eprint.iacr.org/2019/001"),
    ("https://web.archive.org/web/2018/https://example.com/x", "https://web.archive.org/web/2018/https://example.com/x"),
    ("https://www.nature.com/articles/s41586-019-1666-5", "https://www.nature.com/articles/s41586-019-1666-5"),
    ("https://openreview.net/forum?id=abc", "https://openreview.net/forum?id=abc"),
    ("https://www.biorxiv.org/content/10.1101/2019.12.01", "https://www.biorxiv.org/content/10.1101/2019.12.01"),
]

SCHEMES = [
    "",
    "http://arxiv.org/abs/1",
    "ftp://arxiv.org/x",
    "//arxiv.org/x",
    "arxiv.org/abs/1",
    "javascript:alert(1)",
    "data:text/html,hello",
    "file:///etc/passwd",
    "ws://arxiv.org/x",
    "https:/arxiv.org/x",
    "https:arxiv.org/x",
    "https://",
    "https:///abs/1",
    "httpss://arxiv.org/x",
    " https://arxiv.org/x",
]

USERINFO = [
    "https://user@arxiv.org/abs/1",
    "https://user:pass@arxiv.org/",
    "https://arxiv.org@evil.com/",
    "https://evil.com@arxiv.org/",
    "https://arxiv.org:443@evil.com/",
    "https://@arxiv.org/",
    "https://:@arxiv.org/",
]

PORTS = [
    "https://arxiv.org:80/abs/1",
    "https://arxiv.org:8443/abs/1",
    "https://arxiv.org:0/abs/1",
    "https://arxiv.org:/abs/1",
    "https://arxiv.org:443x/abs/1",
    "https://arxiv.org:4430/abs/1",
    "https://arxiv.org:65536/abs/1",
]

SPOOFS = [
    "https://arxiv.org.evil.com/abs/1",
    "https://evilarxiv.org/abs/1",
    "https://arxiv.org-evil.com/abs/1",
    "https://notarxiv.org/abs/1",
    "https://arxiv.org.attacker.io/abs/1",
    "https://evil.com/arxiv.org/abs/1",
    "https://evil.com?u=arxiv.org",
    "https://evil.com#arxiv.org",
    "https://xn--arxiv-9ua.org/abs/1",
    "https://arxiv.org.evil.com./abs/1",
    "https://arxiv-org.com/abs/1",
    "https://doi.org.evil.com/10.1000/x",
    "https://example.com/abs/1",
    "https://github.io/x",
    "https://patents.google.com.evil.com/patent/US1",
    "https://google.com/patents/US1",
]

PRIVATE = [
    "https://127.0.0.1/",
    "https://[::1]/",
    "https://[2001:db8::1]/x",
    "https://169.254.169.254/latest/meta-data",
    "https://2130706433/",
    "https://0x7f000001/",
    "https://localhost/",
    "https://10.0.0.1:443/",
    "https://192.168.1.1/",
    "https://0.0.0.0/",
    "https://arxiv.org.127.0.0.1/",
    "https://internal/",
    "https://metadata.google.internal/",
]

MALFORMED = [
    "https://.arxiv.org/",
    "https://arxiv.org./abs",
    "https://arxiv..org/abs",
    "https://-arxiv.org/abs",
    "https://arxiv-.org/abs",
    "https://arxiv_.org/abs",
    "https://%61rxiv.org/abs",
    "https://arxiv.org%2f@evil.com/",
    "https://arxiv/",
    "https://ar xiv.org/abs",
    "https://arxiv.org/ab s",
    "https://arxiv.org/abs\n1",
    "https://arxiv.org/abs\t1",
    "https://arxiv.org/abs\x001",
    "https://arxiv.org\\@evil.com/",
    "https://arxiv.org/abs\\..\\x",
    "https://arxiv.org/abs/é",
    "https://аrxiv.org/abs/1",
    "https://arxiv.org/\U0001f600",
    "https://arxiv.org/" + "a" * 600,
    "https://" + "a" * 70 + ".arxiv.org/x",
]


def test_valid_urls_are_canonicalised(direct_deploy):
    c = direct_deploy(CONTRACT)
    for raw, canonical in VALID:
        assert c.check_url(raw) == canonical, raw


@pytest.mark.parametrize(
    "name,cases",
    [
        ("schemes", SCHEMES),
        ("userinfo", USERINFO),
        ("ports", PORTS),
        ("spoofs", SPOOFS),
        ("private", PRIVATE),
        ("malformed", MALFORMED),
    ],
)
def test_hostile_urls_are_refused(direct_vm, direct_deploy, name, cases):
    c = direct_deploy(CONTRACT)
    for raw in cases:
        with direct_vm.expect_revert("ERR_UNSAFE_URL"):
            c.check_url(raw)


def test_whitelist_is_exposed_and_nonempty(direct_deploy):
    c = direct_deploy(CONTRACT)
    k = c.get_constants()
    assert "arxiv.org" in k["allowed_domains"]
    assert "patents.google.com" in k["allowed_domains"]
    assert all(d == d.lower() and "/" not in d for d in k["allowed_domains"])
    assert k["challenger_bond"] == str(10**17)


def test_max_length_boundary(direct_vm, direct_deploy):
    c = direct_deploy(CONTRACT)
    base = "https://arxiv.org/"
    ok = base + "a" * (512 - len(base))
    assert c.check_url(ok) == ok
    with direct_vm.expect_revert("ERR_UNSAFE_URL"):
        c.check_url(ok + "a")
