"""Tests for the derived Whisper vocabulary prior (Phase 5b).

The vocabulary is *derived* from material Sidekick already holds (config,
grounding, and LLM-corrected in-session text) rather than hand-authored, and it
adapts over the call. These tests lock that behaviour: extraction picks domain
proper nouns/acronyms and drops filler; seeding and in-session ``update`` build
and rank the prior; and an empty vocabulary renders ``None`` so transcription
keeps Whisper's default behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

from sidekick.transcript.vocabulary import (
    Vocabulary,
    config_seed_text,
    extract_terms,
)


# ---------------------------------------------------------------------------
# extract_terms
# ---------------------------------------------------------------------------


class TestExtractTerms:
    def test_picks_proper_nouns_and_acronyms(self):
        text = (
            "We connect to Denodo and surface views into Microsoft Fabric. "
            "The CDIO team and AWS migration matter to HMRC."
        )
        terms = extract_terms(text)
        for expected in ("Denodo", "Microsoft", "Fabric", "CDIO", "AWS", "HMRC"):
            assert expected in terms

    def test_drops_common_stopwords(self):
        terms = extract_terms("The team thinks this is great and really useful.")
        lowered = {t.lower() for t in terms}
        assert "the" not in lowered
        assert "this" not in lowered
        assert "great" not in lowered

    def test_keeps_acronym_with_digits(self):
        terms = extract_terms("We run PG16 on the estate and use S3 buckets.")
        assert "PG16" in terms
        assert "S3" in terms

    def test_blocklisted_caps_words_excluded(self):
        # "THE" in caps is filler, not a term.
        terms = extract_terms("THE DATA must flow. Denodo helps.")
        assert "THE" not in terms
        assert "Denodo" in terms

    def test_dedupes_preserving_first_seen_order(self):
        terms = extract_terms("Fabric Fabric Denodo Fabric Denodo")
        assert terms.count("Fabric") == 1
        assert terms.count("Denodo") == 1

    def test_empty_text_returns_empty(self):
        assert extract_terms("") == []
        assert extract_terms(None) == []  # type: ignore[arg-type]

    def test_respects_limit(self):
        words = [f"Term{a}{b}" for a in "abcde" for b in "abcdefghijklmnopqrst"]
        assert len(words) == 100
        assert len(extract_terms(" ".join(words), limit=10)) == 10


# ---------------------------------------------------------------------------
# Vocabulary — seeding, update, ranking, prompt rendering
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_empty_vocabulary_renders_none(self):
        v = Vocabulary()
        assert v.initial_prompt() is None
        assert len(v) == 0

    def test_seed_adds_terms_to_prompt(self):
        v = Vocabulary()
        v.seed("Microsoft Fabric and Power BI on AWS")
        prompt = v.initial_prompt()
        assert prompt is not None
        assert prompt.startswith("Glossary:")
        assert "Fabric" in prompt
        assert "AWS" in prompt

    def test_update_promotes_in_session_term(self):
        # A term that only appears mid-call (Denodo) must enter the prior once
        # the analyst surfaces it correctly in key_facts/research.
        v = Vocabulary()
        v.seed("Microsoft Fabric")
        assert "Denodo" not in (v.initial_prompt() or "")

        v.update(["HMRC uses Denodo for data virtualization."])
        assert "Denodo" in v.terms()

    def test_update_accepts_single_string(self):
        v = Vocabulary()
        v.update("Posit and Denodo on the estate")
        assert "Posit" in v.terms()
        assert "Denodo" in v.terms()

    def test_update_none_is_noop(self):
        v = Vocabulary()
        v.update(None)
        assert len(v) == 0

    def test_update_outranks_seed(self):
        # In-session terms carry higher weight than seed terms, so a repeatedly
        # corrected proper noun rises to the top of the bounded prompt.
        v = Vocabulary(max_terms=2)
        v.seed("Alpha Bravo Charlie Delta")  # four seed terms, weight 1 each
        v.update(["Denodo"])  # weight 2
        v.update(["Denodo"])  # weight 4 total
        top = v.terms()
        assert top[0] == "Denodo"
        assert len(top) == 2

    def test_terms_capped_at_max(self):
        v = Vocabulary(max_terms=3)
        words = [f"Term{a}{b}" for a in "abcd" for b in "abcde"]
        v.seed(" ".join(words))
        assert len(v.terms()) == 3

    def test_prompt_respects_char_budget(self):
        v = Vocabulary(max_terms=100, max_prompt_chars=40)
        words = [f"Termname{a}{b}" for a in "abcde" for b in "abcdefghij"]
        v.seed(" ".join(words))
        prompt = v.initial_prompt()
        assert prompt is not None
        assert len(prompt) <= 41  # budget + trailing period tolerance
        assert prompt.endswith(".")


# ---------------------------------------------------------------------------
# config_seed_text
# ---------------------------------------------------------------------------


class TestConfigSeedText:
    def test_none_config_returns_empty(self):
        assert config_seed_text(None) == ""

    def test_builds_from_customer_description_domains(self):
        cfg = SimpleNamespace(
            customer="HMRC",
            description="Fabric adoption and AWS integration",
            domains=["Microsoft Fabric", "Power BI", "PostgreSQL"],
        )
        text = config_seed_text(cfg)
        assert "HMRC" in text
        assert "Fabric" in text
        assert "PostgreSQL" in text

    def test_seed_from_config_feeds_vocabulary(self):
        cfg = SimpleNamespace(
            customer="HMRC",
            description="AWS S3 and Denodo virtualization",
            domains=["Microsoft Fabric"],
        )
        v = Vocabulary()
        v.seed(config_seed_text(cfg))
        terms = v.terms()
        assert "HMRC" in terms
        assert "Denodo" in terms
        assert "Fabric" in terms


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
