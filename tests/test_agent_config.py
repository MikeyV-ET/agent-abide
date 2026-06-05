"""Agent config validation tests.

Verifies each agent's awareness.json and gaze.json are well-formed
and consistent. Parametrized over the agent roster from agents.json.

These are live config tests -- they read the actual agent state on disk.
Failures indicate misconfiguration, not code bugs.

Run: pytest test_agent_config.py -v
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

AGENTS_HOME = os.path.expanduser("~/agents")
AGENTS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'agents.json')

VALID_AWARENESS_MODES = {"doorbell", "pending", "drop"}


def get_agent_names():
    """Read agent roster from agents.json."""
    with open(AGENTS_JSON) as f:
        data = json.load(f)
    return list(data["agents"].keys())


def agent_asdaaas_dir(name):
    return os.path.join(AGENTS_HOME, name, "asdaaas")


def agent_adapters_dir(name):
    return os.path.join(agent_asdaaas_dir(name), "adapters")


def read_json(path):
    with open(path) as f:
        return json.load(f)


# ============================================================================
# CV: Config Validation -- gaze.json
# ============================================================================

class TestGazeConfig:
    """CV1-CV2: Validate gaze.json format for all agents."""

    @pytest.fixture(params=get_agent_names())
    def agent(self, request):
        return request.param

    def test_cv1_gaze_target_is_valid_adapter(self, agent):
        """CV1: Gaze speech target must be a known adapter or None (gaze off)."""
        gaze_path = os.path.join(agent_asdaaas_dir(agent), "gaze.json")
        if not os.path.exists(gaze_path):
            pytest.skip(f"{agent} has no gaze.json")
        gaze = read_json(gaze_path)
        speech = gaze.get("speech")
        if speech is None:
            return  # gaze off
        target = speech.get("target")
        assert target, f"{agent}: gaze speech has no target"
        adapter_dir = os.path.join(agent_adapters_dir(agent), target)
        assert os.path.isdir(adapter_dir), (
            f"{agent}: gaze target '{target}' has no adapter directory at {adapter_dir}"
        )

    def test_cv2_non_irc_adapter_has_no_room(self, agent):
        """CV2: Non-IRC adapters should not have a room param (issue #14 regression)."""
        gaze_path = os.path.join(agent_asdaaas_dir(agent), "gaze.json")
        if not os.path.exists(gaze_path):
            pytest.skip(f"{agent} has no gaze.json")
        gaze = read_json(gaze_path)
        speech = gaze.get("speech")
        if speech is None:
            return
        target = speech.get("target", "")
        params = speech.get("params", {})
        if target != "irc":
            assert "room" not in params or not params.get("room"), (
                f"{agent}: non-IRC adapter '{target}' has room='{params.get('room')}' "
                f"in gaze params -- likely issue #14 (should be empty params)"
            )


# ============================================================================
# CV: Config Validation -- awareness.json
# ============================================================================

class TestAwarenessConfig:
    """CV3-CV4: Validate awareness.json for all agents."""

    @pytest.fixture(params=get_agent_names())
    def agent(self, request):
        return request.param

    def test_cv3_direct_attach_adapters_exist(self, agent):
        """CV3: Every adapter in direct_attach must have an adapters/ directory."""
        awareness_path = os.path.join(agent_asdaaas_dir(agent), "awareness.json")
        if not os.path.exists(awareness_path):
            pytest.skip(f"{agent} has no awareness.json")
        awareness = read_json(awareness_path)
        for adapter in awareness.get("direct_attach", []):
            adapter_dir = os.path.join(agent_adapters_dir(agent), adapter)
            assert os.path.isdir(adapter_dir), (
                f"{agent}: direct_attach includes '{adapter}' but no adapter "
                f"directory at {adapter_dir}"
            )

    def test_cv4_background_channels_valid_modes(self, agent):
        """CV4: Background channels must use valid modes (doorbell/pending/drop)."""
        awareness_path = os.path.join(agent_asdaaas_dir(agent), "awareness.json")
        if not os.path.exists(awareness_path):
            pytest.skip(f"{agent} has no awareness.json")
        awareness = read_json(awareness_path)
        for channel, mode in awareness.get("background_channels", {}).items():
            assert mode in VALID_AWARENESS_MODES, (
                f"{agent}: background_channels['{channel}'] has invalid "
                f"mode '{mode}', expected one of {VALID_AWARENESS_MODES}"
            )
