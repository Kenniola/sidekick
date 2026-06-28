"""Tests for config deep-merge and dict→dataclass parsing (Phase 3)."""

from __future__ import annotations

from sidekick.config import _deep_merge, _parse_config


class TestDeepMerge:
    def test_override_replaces_scalar(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_adds_new_key(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_nested_dicts_merge_recursively(self):
        base = {"queue": {"fast_lane_max": 3, "deep_lane_max": 1}}
        over = {"queue": {"fast_lane_max": 5}}
        assert _deep_merge(base, over) == {
            "queue": {"fast_lane_max": 5, "deep_lane_max": 1}
        }

    def test_lists_replaced_wholesale_not_concatenated(self):
        assert _deep_merge({"domains": ["a", "b"]}, {"domains": ["c"]}) == {
            "domains": ["c"]
        }

    def test_does_not_mutate_base(self):
        base = {"a": {"x": 1}}
        _deep_merge(base, {"a": {"y": 2}})
        assert base == {"a": {"x": 1}}


class TestParseConfig:
    def test_empty_dict_yields_defaults(self):
        c = _parse_config({})
        assert c.customer == "General"
        assert c.queue.fast_lane_max == 3
        assert c.speech.backend == "whisper"

    def test_flat_consultant_string_becomes_list(self):
        assert _parse_config({"consultant": "Alice"}).consultant_names == ["Alice"]

    def test_nested_participants_used_when_no_flat_key(self):
        c = _parse_config({"participants": {"consultant": ["Bob"]}})
        assert c.consultant_names == ["Bob"]

    def test_flat_consultant_overrides_nested(self):
        c = _parse_config(
            {"consultant": "Alice", "participants": {"consultant": ["Bob"]}}
        )
        assert c.consultant_names == ["Alice"]

    def test_legacy_azure_backend_normalised_to_whisper(self):
        assert _parse_config({"speech": {"backend": "azure"}}).speech.backend == "whisper"

    def test_capture_microphone_defaults_false(self):
        assert _parse_config({}).speech.capture_microphone is False

    def test_capture_microphone_enabled_from_yaml(self):
        c = _parse_config({"speech": {"capture_microphone": True}})
        assert c.speech.capture_microphone is True

    def test_capture_microphone_truthy_string_coerced(self):
        c = _parse_config({"speech": {"capture_microphone": "yes"}})
        assert c.speech.capture_microphone is True

    def test_glossary_defaults_empty(self):
        assert _parse_config({}).glossary == []

    def test_glossary_parsed_and_trimmed(self):
        c = _parse_config({"glossary": ["Denodo", "  OneLake  ", "", "   "]})
        assert c.glossary == ["Denodo", "OneLake"]

    def test_stt_corrections_defaults_empty(self):
        assert _parse_config({}).stt_corrections == {}

    def test_stt_corrections_parsed_and_filtered(self):
        c = _parse_config(
            {"stt_corrections": {"on lake": "OneLake", "": "x", "y": ""}}
        )
        assert c.stt_corrections == {"on lake": "OneLake"}

    def test_models_section_parsed(self):
        c = _parse_config({"models": {"fast": ["copilot:x"]}})
        assert c.models.fast == ["copilot:x"]

    def test_models_default_when_section_absent(self):
        c = _parse_config({})
        assert c.models.fast  # falls back to code default chain

    def test_notifications_sound_lowercased(self):
        assert _parse_config({"notifications": {"sound": "CHIME"}}).notifications.sound == "chime"

    def test_queue_overrides_applied(self):
        c = _parse_config({"queue": {"fast_lane_max": 9, "deep_lane_max": 4}})
        assert c.queue.fast_lane_max == 9
        assert c.queue.deep_lane_max == 4
