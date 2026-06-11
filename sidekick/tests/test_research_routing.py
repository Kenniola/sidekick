"""Tests for research per-domain source routing and verified-URL filtering."""

from sidekick.actions.research import ResearchPipeline, _WebHit


def _hit(url: str, title: str = "t") -> _WebHit:
    return _WebHit(title=title, url=url, snippet="s", source_label="Web")


def test_drops_unverified_hosts():
    """Only hosts in the trust map survive ranking (verified-URL rule)."""
    pipeline = ResearchPipeline()
    hits = [
        _hit("https://learn.microsoft.com/en-us/fabric/onelake/overview"),
        _hit("https://random-blog.example.com/post/fabric-tips"),
        _hit("https://stackoverflow.com/questions/123/fabric"),
    ]
    ranked = pipeline._rank_hits(hits, domains=None)
    assert any("learn.microsoft.com" in h.url for h in ranked)
    assert not any("example.com" in h.url for h in ranked)
    assert not any("stackoverflow.com" in h.url for h in ranked)
    assert len(ranked) == 1


def test_microsoft_ranks_first_by_default():
    """With no domain routing, Microsoft outranks partner/OSS docs."""
    pipeline = ResearchPipeline()
    hits = [
        _hit("https://docs.aws.amazon.com/AmazonS3/latest/userguide/intro.html"),
        _hit("https://learn.microsoft.com/en-us/fabric/onelake/onelake-shortcuts"),
    ]
    ranked = pipeline._rank_hits(hits, domains=None)
    assert ranked[0].url.startswith("https://learn.microsoft.com")


def test_aws_domain_promotes_aws_docs():
    """An AWS-detected question lifts AWS docs above the Microsoft baseline."""
    pipeline = ResearchPipeline()
    hits = [
        _hit("https://learn.microsoft.com/en-us/fabric/onelake/onelake-shortcuts"),
        _hit("https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-points.html"),
    ]
    ranked = pipeline._rank_hits(hits, domains=["AWS S3 Integration"])
    assert ranked[0].url.startswith("https://docs.aws.amazon.com")
    # Microsoft is not suppressed — it is still present, just second.
    assert any("learn.microsoft.com" in h.url for h in ranked)


def test_dedup_by_url():
    """Duplicate URLs are collapsed to a single ranked hit."""
    pipeline = ResearchPipeline()
    url = "https://learn.microsoft.com/en-us/fabric/onelake/onelake-overview"
    ranked = pipeline._rank_hits([_hit(url), _hit(url)], domains=None)
    assert len(ranked) == 1


def test_subdomain_match_is_anchored():
    """Trust matching is host-anchored — look-alike domains are rejected."""
    pipeline = ResearchPipeline()
    hits = [_hit("https://learn.microsoft.com.evil.example/phish")]
    ranked = pipeline._rank_hits(hits, domains=None)
    assert ranked == []


def test_extra_trusted_domains_extends_map():
    """grounding.extra_trusted_domains adds a host without editing code."""

    class _Grounding:
        repo_paths = [".github/instructions/"]
        extra_trusted_domains = {"docs.snowflake.com": 60.0}

    class _Config:
        grounding = _Grounding()

    pipeline = ResearchPipeline(config=_Config())
    hits = [_hit("https://docs.snowflake.com/en/user-guide/intro")]
    ranked = pipeline._rank_hits(hits, domains=None)
    assert len(ranked) == 1
