import re
from typing import Collection, Dict, Iterable, List, Set, Tuple

from requests import Session

from .logs import logger


MAX_TITLES = 50


class MediaWikiError(Exception):
    pass


def get_api(session: Session, **kwargs) -> Iterable[Tuple[str, str]]:
    params = {
        'action': 'query',
        'prop': 'revisions',
        'rvprop': 'content',
        'rvslots': '*',
        'format': 'json',
        **kwargs,
    }

    while True:
        with session.get(
            'https://satisfactory.gamepedia.com/api.php', params=params,
        ) as resp:
            resp.raise_for_status()
            data = resp.json()

        warnings = data.get('warnings')
        if warnings:
            for title, entries in warnings.items():
                for warning in entries.values():
                    logger.warning(f'%s: %s', title, warning)

        error = data.get('error')
        if error:
            raise MediaWikiError(
                f'{error["code"]}: {error["info"]}'
            )

        for page in data['query']['pages'].values():
            revisions = page.get('revisions')
            if revisions is None and 'missing' in page:
                logger.warning(f'Page "{page["title"]}" missing')
            else:
                revision, = revisions
                yield page['title'], revision['slots']['main']['*']

        if 'batchcomplete' in data:
            break

        params.update(data['continue'])


def parse_template(content: str, tiers: Set[str]) -> List[str]:
    start = content.index('\n| group2     = Components\n')
    end = content.index(f'\n}}', start)
    content = content[start: end]

    for tier_start in re.finditer(
        r'^   \| group\d = \[\[(.+)\]\]$', content, re.M,
    ):
        tier_name = tier_start[1]
        if tier_name not in tiers:
            continue

        tier_index = tier_start.end() + 1
        list_content = content[tier_index: content.find('\n', tier_index)]

        for name in re.finditer(
            r'(?<={{ItemLink\|)'
            r'[^|\}]+',
            list_content,
        ):
            yield name[0]


def fetch_recipes(session: Session, names: List[str]) -> Iterable[Tuple[str, str]]:
    for start in range(0, len(names), MAX_TITLES):
        name_segment = names[start: start + MAX_TITLES]
        yield from get_api(
            session,
            titles='|'.join(name_segment),
            # rvsection=2,  # does not work for Biomass
        )


def parse_box_attrs(content: str) -> Iterable[Tuple[str, str]]:
    for match in re.finditer(
        r'^'
        r'\s*\|'
        r'\s*(\w+?)'
        r'\s*='
        r'\s*([^=|]+?)'
        r'\s*$',
        content,
        re.M,
    ):
        yield match.groups()


def parse_infoboxes(page: str) -> Iterable[Dict[str, str]]:
    for box_match in re.finditer(
        r'^{{Infobox.*$', page, re.M,
    ):
        box_content = page[
            box_match.end() + 1:
            page.index('}}\n', box_match.end() + 1)
        ]
        yield dict(parse_box_attrs(box_content))


def fetch_power_info(
    session: Session,
    building_names: Collection[str],
) -> Iterable[Tuple[str, Dict[str, str]]]:
    non_miners = dict(
        get_api(
            session,
            titles='|'.join(
                b for b in building_names if 'Miner' not in b
            ),
        )
    )
    for name, page in non_miners.items():
        info, = parse_infoboxes(page)
        yield name, info

    if any('Miner' in b for b in building_names):
        (_, miner_page), = get_api(session, titles='Miner')
        for info in parse_infoboxes(miner_page):
            yield info['name'], info


def fetch_powers(
    session: Session,
    building_names: Collection[str],
    tiers: Set[str],
) -> Iterable[Tuple[str, float]]:
    for name, info in fetch_power_info(session, building_names):
        if any(
            tier in info['researchTier']
            for tier in tiers
        ):
            yield name, float(info['powerUsage'])
