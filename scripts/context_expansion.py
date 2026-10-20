"""Bounded direct context expansion, ready for a future retrieval pipeline."""

HARD_LIMITS = {'max_seed_chunks': 8, 'max_added_chunks': 4,
               'max_total_chars': 6000, 'max_candidates': 64}


def validate_limits(limits: dict) -> None:
    for key, ceiling in HARD_LIMITS.items():
        if type(limits.get(key)) is not int or not 0 < limits[key] <= ceiling:
            raise ValueError(f'{key} must be an integer between 1 and {ceiling}')


def expand_context(seed_ids: list[str], chunks_by_id: dict, limits: dict) -> dict:
    """Read direct neighbors of original hits only; never walk neighbors recursively."""
    validate_limits(limits)
    seeds = list(dict.fromkeys(seed_ids))
    if not seeds or len(seeds) > limits['max_seed_chunks']:
        raise ValueError('Provide between 1 and max_seed_chunks distinct original hits')
    if any(seed not in chunks_by_id for seed in seeds):
        raise ValueError('Unknown original chunk ID')
    selected, omitted, added, seen = [], [], [], set(seeds)
    chars = 0
    for seed in seeds:
        size = len(chunks_by_id[seed]['text'])
        if chars + size <= limits['max_total_chars']:
            selected.append(seed)
            chars += size
        else:
            omitted.append(seed)
    candidates = []
    # Prioritize original hits, then interleave their direct neighbors fairly.
    neighbor_lists = [chunks_by_id[seed].get('related_chunk_ids', []) for seed in seeds]
    inspected = 0
    for depth in range(min(max(map(len, neighbor_lists), default=0), limits['max_candidates'])):
        for neighbors in neighbor_lists:
            if depth >= len(neighbors):
                continue
            if inspected == limits['max_candidates']:
                break
            inspected += 1
            candidate = neighbors[depth]
            if candidate not in chunks_by_id:
                raise ValueError(f'Dangling context relation: {candidate}')
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
        if inspected == limits['max_candidates']:
            break
    unexamined = sum(map(len, neighbor_lists)) - inspected
    for candidate in candidates:
        size = len(chunks_by_id[candidate]['text'])
        if len(added) >= limits['max_added_chunks'] or chars + size > limits['max_total_chars']:
            omitted.append(candidate)
        else:
            added.append(candidate)
            selected.append(candidate)
            chars += size
    # Only source text and citations enter the answer context, never the graph.
    payload = [{key: chunks_by_id[cid][key] for key in ('chunk_id', 'text', 'source_file', 'pdf_pages')}
               for cid in selected]
    return {'chunks': payload, 'seed_chunk_ids': seeds, 'added_chunk_ids': added,
            'omitted_chunk_ids': omitted, 'relations_not_examined': unexamined,
            'complete': not omitted and not unexamined, 'total_chars': chars,
            'limits': limits, 'expansion_hops': 1}
