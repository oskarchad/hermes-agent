"""Read-only compatibility for task tool caps stored by the previous fork."""
from copy import deepcopy


def bounded_worker_config(cap, config):
    """Return a validated exact pick and private config; never expand a bad cap."""
    from agent.skill_utils import parse_config_string_list
    from hermes_cli.toolset_validation import saved_toolset_resolver
    from hermes_cli.tools_config import enabled_mcp_server_names
    from hermes_state_runtime import RuntimeStoreError

    if (not isinstance(cap, list) or len(cap) > 32
            or any(not isinstance(name, str) or not name.strip() or len(name) > 128 for name in cap)):
        raise RuntimeStoreError('invalid_kanban_toolsets')
    toolsets = sorted({name.strip() for name in cap} | {'context7', 'kanban'})
    known = saved_toolset_resolver(config)
    if any(not known(name) for name in toolsets) or 'context7' not in enabled_mcp_server_names(config):
        raise RuntimeStoreError('invalid_kanban_toolsets')
    frozen = deepcopy(config)
    agent = frozen.setdefault('agent', {})
    # These two workflow capabilities were mandatory under explicit task caps.
    # Keep every other profile disable, including tool-level composite exclusions.
    agent['disabled_toolsets'] = [name for name in parse_config_string_list(agent.get('disabled_toolsets'))
                                 if name not in {'kanban', 'context7', 'mcp-context7'}]
    return toolsets, frozen
