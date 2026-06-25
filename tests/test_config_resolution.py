"""Tests for asdaaas_config.py — config loading, path resolution, per-agent settings.

Run: pytest tests/test_config_resolution.py -v
"""

import json
import os
import pytest
from pathlib import Path
from unittest.mock import patch


class TestConfigDefaults:
    """Default values when no config file exists."""

    def test_agents_home_default(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ASDAAAS_CONFIG", None)
            import importlib
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
            import asdaaas_config
            importlib.reload(asdaaas_config)
            cfg = asdaaas_config.AsdaaasConfig.__new__(asdaaas_config.AsdaaasConfig)
            cfg._data = {}
            cfg._agents_home = Path(os.path.expanduser("~/agents"))
            cfg._asdaaas_dir = Path(os.path.expanduser("~/asdaaas"))
            cfg._agents = {}
            cfg._grok_sessions_dir = Path.home() / ".grok" / "sessions"
            assert cfg.agents_home == Path.home() / "agents"
            assert cfg.asdaaas_dir == Path.home() / "asdaaas"

    def test_adapters_dir_derived(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
        from asdaaas_config import config
        assert config.adapters_dir == config.asdaaas_dir / "adapters"

    def test_issues_dir_derived(self):
        from asdaaas_config import config
        assert config.issues_dir == config.agents_home / "issues"

    def test_bugs_dir_is_issues_dir(self):
        from asdaaas_config import config
        assert config.bugs_dir == config.issues_dir


class TestConfigFromEnv:
    """Config loaded from ASDAAAS_CONFIG environment variable."""

    def test_env_config_overrides_defaults(self, tmp_path):
        config_data = {
            "agents_home": str(tmp_path / "my_agents"),
            "asdaaas_dir": str(tmp_path / "my_asdaaas"),
        }
        config_file = tmp_path / "test_config.json"
        config_file.write_text(json.dumps(config_data))

        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
        import importlib
        import asdaaas_config

        with patch.dict(os.environ, {"ASDAAAS_CONFIG": str(config_file)}):
            importlib.reload(asdaaas_config)
            cfg = asdaaas_config.config
            assert cfg.agents_home == tmp_path / "my_agents"
            assert cfg.asdaaas_dir == tmp_path / "my_asdaaas"

        # Reload with original env to restore singleton
        os.environ.pop("ASDAAAS_CONFIG", None)
        importlib.reload(asdaaas_config)

    def test_env_config_with_agents(self, tmp_path):
        config_data = {
            "agents_home": str(tmp_path / "agents"),
            "asdaaas_dir": str(tmp_path / "asdaaas"),
            "agents": {
                "TestBot": {
                    "yolo": False,
                    "mentor": "Sr",
                    "context_window": 100000,
                }
            }
        }
        config_file = tmp_path / "test_config.json"
        config_file.write_text(json.dumps(config_data))

        import importlib
        import asdaaas_config

        with patch.dict(os.environ, {"ASDAAAS_CONFIG": str(config_file)}):
            importlib.reload(asdaaas_config)
            cfg = asdaaas_config.config
            assert cfg.agent_yolo("TestBot") is False
            assert cfg.agent_mentor("TestBot") == "Sr"
            assert cfg.agent_context_window("TestBot") == 100000

        os.environ.pop("ASDAAAS_CONFIG", None)
        importlib.reload(asdaaas_config)


class TestAgentPaths:
    """Per-agent path resolution."""

    def test_agent_home_default(self):
        from asdaaas_config import config
        assert config.agent_home("Sr") == config.agents_home / "Sr"

    def test_agent_home_custom(self, tmp_path):
        config_data = {
            "agents_home": str(tmp_path),
            "asdaaas_dir": str(tmp_path / "asdaaas"),
            "agents": {
                "Custom": {"home": str(tmp_path / "custom_home")}
            }
        }
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps(config_data))

        import importlib
        import asdaaas_config
        with patch.dict(os.environ, {"ASDAAAS_CONFIG": str(config_file)}):
            importlib.reload(asdaaas_config)
            cfg = asdaaas_config.config
            assert cfg.agent_home("Custom") == tmp_path / "custom_home"

        os.environ.pop("ASDAAAS_CONFIG", None)
        importlib.reload(asdaaas_config)

    def test_agent_asdaaas_dir(self):
        from asdaaas_config import config
        assert config.agent_asdaaas_dir("Trip") == config.agent_home("Trip") / "asdaaas"

    def test_agent_doorbells_dir(self):
        from asdaaas_config import config
        assert config.agent_doorbells_dir("Trip") == config.agent_asdaaas_dir("Trip") / "doorbells"

    def test_agent_adapter_inbox(self):
        from asdaaas_config import config
        inbox = config.agent_adapter_inbox("Trip", "irc")
        assert inbox == config.agent_asdaaas_dir("Trip") / "adapters" / "irc" / "inbox"

    def test_agent_adapter_outbox(self):
        from asdaaas_config import config
        outbox = config.agent_adapter_outbox("Trip", "localmail")
        assert outbox == config.agent_asdaaas_dir("Trip") / "adapters" / "localmail" / "outbox"

    def test_agent_permissions_dir(self):
        from asdaaas_config import config
        assert config.agent_permissions_dir("Jr") == config.agent_asdaaas_dir("Jr") / "permissions"


class TestAgentSettings:
    """Per-agent settings with defaults."""

    def test_yolo_default_true(self):
        from asdaaas_config import config
        assert config.agent_yolo("NonexistentAgent") is True

    def test_mentor_default_none(self):
        from asdaaas_config import config
        assert config.agent_mentor("NonexistentAgent") is None

    def test_context_window_default_none(self):
        from asdaaas_config import config
        assert config.agent_context_window("NonexistentAgent") is None

    def test_allow_kinds_default_empty(self):
        from asdaaas_config import config
        assert config.agent_allow_kinds("NonexistentAgent") == []

    def test_sandbox_default_none(self):
        from asdaaas_config import config
        assert config.agent_sandbox("NonexistentAgent") is None

    def test_allow_rules_default_empty(self):
        from asdaaas_config import config
        assert config.agent_allow_rules("NonexistentAgent") == []

    def test_deny_rules_default_empty(self):
        from asdaaas_config import config
        assert config.agent_deny_rules("NonexistentAgent") == []

    def test_permission_mode_default_none(self):
        from asdaaas_config import config
        assert config.agent_permission_mode("NonexistentAgent") is None

    def test_reasoning_effort_default_none(self):
        from asdaaas_config import config
        assert config.agent_reasoning_effort("NonexistentAgent") is None

    def test_backend_default_grok(self):
        from asdaaas_config import config
        assert config.agent_backend("NonexistentAgent") == "grok"


class TestNormalization:
    """agents.json format normalized to config format."""

    def test_agents_json_settings_normalized(self, tmp_path):
        agents_data = {
            "settings": {
                "agents_dir": str(tmp_path / "agents"),
                "asdaaas_dir": str(tmp_path / "asdaaas"),
            }
        }
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps(agents_data))

        import importlib
        import asdaaas_config
        with patch.dict(os.environ, {"ASDAAAS_CONFIG": str(config_file)}):
            importlib.reload(asdaaas_config)
            cfg = asdaaas_config.config
            assert cfg.agents_home == tmp_path / "agents"
            assert cfg.asdaaas_dir == tmp_path / "asdaaas"

        os.environ.pop("ASDAAAS_CONFIG", None)
        importlib.reload(asdaaas_config)


class TestSessionsDir:
    """grok sessions directory resolution."""

    def test_explicit_sessions_dir(self, tmp_path):
        config_data = {
            "agents_home": str(tmp_path),
            "asdaaas_dir": str(tmp_path),
            "grok_sessions_dir": str(tmp_path / "my_sessions"),
        }
        config_file = tmp_path / "cfg.json"
        config_file.write_text(json.dumps(config_data))

        import importlib
        import asdaaas_config
        with patch.dict(os.environ, {"ASDAAAS_CONFIG": str(config_file)}):
            importlib.reload(asdaaas_config)
            assert asdaaas_config.config.grok_sessions_dir == tmp_path / "my_sessions"

        os.environ.pop("ASDAAAS_CONFIG", None)
        importlib.reload(asdaaas_config)

    def test_standard_sessions_dir_fallback(self):
        from asdaaas_config import config
        assert "sessions" in str(config.grok_sessions_dir)


class TestLegacyCompat:
    """Legacy compatibility aliases."""

    def test_hub_dir_is_asdaaas_dir(self):
        from asdaaas_config import config
        assert config.hub_dir == config.asdaaas_dir

    def test_inbox_dir(self):
        from asdaaas_config import config
        assert config.inbox_dir == config.asdaaas_dir / "inbox"

    def test_outbox_dir(self):
        from asdaaas_config import config
        assert config.outbox_dir == config.asdaaas_dir / "outbox"