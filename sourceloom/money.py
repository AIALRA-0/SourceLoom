"""Native yuan estimates and subscription usage, without relabeling USD history."""
def usage_cost(usage,rates):
    if not rates or not {'prompt_tokens','completion_tokens'}<=set(usage):return None
    cached=min(usage['prompt_tokens'],max(0,usage.get('prompt_cache_hit_tokens',usage.get('prompt_tokens_details',{}).get('cached_tokens',0))))
    return ((usage['prompt_tokens']-cached)*rates['input']+cached*rates.get('cached_input',rates['input'])+usage['completion_tokens']*rates['output'])/1e6


def summary(costs):
    return {'known_cny':sum(c['body'].get('actual_cny') or 0 for c in costs),
            'reserved_cny':sum(c['body'].get('reserved_cny') or 0 for c in costs if c['actual'] is None),
            'legacy_currency_records':sum(c['actual'] not in (None,0) and c['body'].get('actual_cny') is None for c in costs),
            'subscription_calls':sum(c['body'].get('channel') in {'subscription','router','codex-cli'} for c in costs)}
