"""Explicit installed-model routing, no downloads or silent substitution."""
from .ai_contracts import ROLES
from .signals import identifier


def configured_roles(settings, installed, factory):
    if settings.get('TRAD_LLM_ROUTING_ENABLED','0') != '1':
        return None
    models={m['name'] for m in installed if type(m) is dict and type(m.get('name')) is str}
    requested={role:settings.get('TRAD_'+role.upper()+'_MODEL',default) for role,default in
               zip(ROLES,('qwen3:8b','qwen3:14b','qwen3:8b'))}
    for value in requested.values():identifier(value)
    missing=set(requested.values())-models
    if missing:
        fallback=settings.get('TRAD_LLM_FALLBACK_MODEL')
        if not fallback or fallback not in models:
            raise ValueError('Requested role models are not installed; configure explicit fallback or install models manually')
        requested={role:fallback for role in ROLES}
    return {role:factory(model) for role,model in requested.items()}


def routing_settings(environment):
    """Explicit environment overrides optional research file; defaults stay off."""
    values={key:value for key,value in environment.items() if key in {
        'TRAD_LLM_ROUTING_ENABLED','TRAD_ANALYST_MODEL','TRAD_CRITIC_MODEL',
        'TRAD_SYNTHESIZER_MODEL','TRAD_LLM_FALLBACK_MODEL'}}
    path=environment.get('TRAD_MULTI_HORIZON_CONFIG')
    if path:
        from .config import MultiHorizonConfig
        cfg=MultiHorizonConfig.load(path)
        values.setdefault('TRAD_LLM_ROUTING_ENABLED','1' if cfg.routing_enabled else '0')
        for role in ROLES:values.setdefault('TRAD_'+role.upper()+'_MODEL',getattr(cfg,role+'_model'))
    return values
