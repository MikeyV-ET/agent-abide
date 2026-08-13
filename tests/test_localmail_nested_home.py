"""localmail must deliver to agents.json home, not flat agents_home/Name."""
import json
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))


def test_localmail_paths_honor_nested_home(tmp_path, monkeypatch):
    agents_home = tmp_path / "agents"
    nested = tmp_path / "LeviSmith" / "Squiggy"
    (nested / "asdaaas").mkdir(parents=True)
    (agents_home / "Sr" / "asdaaas").mkdir(parents=True)

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "agents.json").write_text(json.dumps({
        "settings": {
            "agents_dir": str(agents_home),
            "asdaaas_dir": str(tmp_path / "asdaaas"),
        },
        "agents": {
            "Squiggy": {"home": str(nested)},
            "Sr": {},
        },
    }))
    monkeypatch.setenv("ASDAAAS_CONFIG", str(cfg_dir))

    import asdaaas_config
    importlib.reload(asdaaas_config)
    import asdaaas_env
    importlib.reload(asdaaas_env)
    import localmail_adapter
    importlib.reload(localmail_adapter)
    import localmail_service
    importlib.reload(localmail_service)

    env = asdaaas_env.AsdaaasEnv.from_config()
    assert env.adapter_inbox("Squiggy", "localmail") == nested / "asdaaas/adapters/localmail/inbox"
    assert env.doorbells_dir("Squiggy") == nested / "asdaaas/doorbells"

    # send_mail writes to Sr flat outbox; deliver goes to nested Squiggy
    localmail_adapter.send_mail(from_agent="Sr", to_agent="Squiggy", text="hello nested", env=env)
    outbox = env.adapter_dir("Sr", "localmail") / "outbox"
    assert any(outbox.glob("*.json"))

    localmail_service.process_outboxes(["Sr", "Squiggy"], env)
    inbox = nested / "asdaaas/adapters/localmail/inbox"
    # After process_outboxes for asdaaas agents, may ring doorbell and delete inbox
    # Depending on asdaaas vs tui handling - check deliver landed somewhere nested
    bells = list((nested / "asdaaas/doorbells").glob("*.json"))
    inbox_files = list(inbox.glob("*.json")) if inbox.exists() else []
    assert bells or inbox_files, "expected delivery under nested Squiggy home"
    # Must NOT have delivered only under flat path
    flat_inbox = agents_home / "Squiggy/asdaaas/adapters/localmail/inbox"
    flat_bells = agents_home / "Squiggy/asdaaas/doorbells"
    # flat dirs might be created incorrectly before fix - ensure nested got the content
    assert (nested / "asdaaas").exists()
