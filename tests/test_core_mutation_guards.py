from pathlib import Path
import pytest
from dari_mcp_vps.security import assert_core_mutation_path, SecurityError

@pytest.mark.parametrize("name",[".env", ".env.local", "id_ed25519", "private.key", "state.db", ".git/config", "n8n/config.yaml", "IA-mcp-vps/config.yaml"])
def test_new_writer_denies_sensitive_paths(tmp_path,name):
    cfg={"allowed_paths":{"sample":{"root":str(tmp_path),"writable":True}}}
    with pytest.raises(SecurityError):assert_core_mutation_path(cfg,"sample",tmp_path/name)

def test_new_writer_read_only_scope_and_symlink_escape(tmp_path):
    cfg={"allowed_paths":{"sample":{"root":str(tmp_path),"writable":False}}}
    with pytest.raises(SecurityError):assert_core_mutation_path(cfg,"sample",tmp_path/"settings.yaml")
    cfg["allowed_paths"]["sample"]["writable"]=True
    link=tmp_path/"outside";link.symlink_to(tmp_path.parent,target_is_directory=True)
    with pytest.raises(SecurityError):assert_core_mutation_path(cfg,"sample",link/"settings.yaml")
    assert_core_mutation_path(cfg,"sample",tmp_path/"settings.yaml")
