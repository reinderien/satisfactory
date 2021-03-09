import pickle
import re
from dataclasses import dataclass
from itertools import count, chain
from pathlib import Path
from typing import ClassVar, Collection, Dict, Iterable, List, Pattern, Set, Tuple

from requests import Session

from .logs import logger


MAX_RECIPES = 50


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


@dataclass
class Recipe:
    name: str
    crafted_in: str
    tier: str
    time: float

    rates: Dict[str, float]

    _parse_re: ClassVar[Pattern] = re.compile(
        r'{{CraftingTable(.+?)}}', re.I | re.S,
    )

    base_power: float = None

    @property
    def first_output(self) -> str:
        return next(
            resource
            for resource, rate in self.rates.items()
            if rate > 0
        )

    def __str__(self):
        return self.name

    @classmethod
    def get_attrs(cls, page: str) -> Iterable[Dict[str, str]]:
        for match in cls._parse_re.finditer(page):
            recipe = match[1]
            yield dict(
                tuple(elm.strip() for elm in kv.split('='))
                for kv in recipe.split('|')[1:]
            )

    @classmethod
    def from_component_page(cls, page: str) -> Iterable['Recipe']:
        """
        {{CraftingTable
        | product = Cable
        | recipeName = Cable
        | researchTier = [[Tier 0]] - HUB Upgrade 2
        | craftedIn = Constructor
        | inCraftBench = 1
        | productCount = 1        (output per crafting time)
        | craftingTime = 2        (seconds)
        | craftingClicks = 1
        | quantity1 = 2           (input per crafting time)
        | ingredient1 = Wire
        }}
        """

        for attrs in cls.get_attrs(page):
            if attrs.get('alternateRecipe') == '1':
                continue

            t = float(attrs['craftingTime'])
            rates = {
                attrs['product']: float(attrs['productCount']) / t,
            }

            for i in count(1):
                name = attrs.get(f'ingredient{i}')
                if name:
                    rates[name] = -float(attrs[f'quantity{i}']) / t
                else:
                    break

            yield Recipe(
                name=attrs['recipeName'],
                crafted_in=attrs['craftedIn'],
                tier=attrs['researchTier'],
                time=t,
                rates=rates,
            )

    @classmethod
    def from_ore_page(cls, page: str) -> Iterable['Recipe']:
        if '[[Category:Ores]]' not in page:
            return ()

        '''
        The page contains

        {{CraftingTable
        | product = Copper Ore
        | recipeName = Copper Ore
        | researchTier = [[Tier 0]] - HUB Upgrade 2
        | craftedIn = Miner
        | productCount = 1
        | craftingTime = 1
        }}

        but in all ore cases this translates to

                Miner Mk.1  Miner Mk.2  Miner Mk.3
        Impure	30	60	120
        Normal	60	120	240
        Pure	120	240	480
        '''

        attrs = next(cls.get_attrs(page))

        for mark, purity, rate in (
                (1, 'Impure', 0.5),
                (1, 'Normal', 1.0),
                (1, 'Pure', 2.0),
                (2, 'Impure', 1.0),
                (2, 'Normal', 2.0),
                (2, 'Pure', 4.0),
                (3, 'Impure', 2.0),
                (3, 'Normal', 4.0),
                (3, 'Pure', 8.0),
        ):
            crafted_in = f'{attrs["craftedIn"]} Mk.{mark}'
            yield cls(
                name=f'{attrs["recipeName"]} from '
                     f'{crafted_in} on {purity} node',
                crafted_in=crafted_in,
                tier=attrs['researchTier'],
                time=float(attrs['craftingTime']),
                rates={
                    attrs['product']: rate,
                },
            )

    def secs_per_extra(self, rates: Dict[str, float], clock_scale: float) -> str:
        rate = rates[self.first_output] * clock_scale
        if rate < 1e-6:
            return '∞'
        return f'{1 / rate:.1f}'


def fill_ores(
    session: Session, recipes: Collection[Recipe],
) -> Iterable[Recipe]:
    known_products = set()
    all_inputs = set()
    for recipe in recipes:
        for product, quantity in recipe.rates.items():
            if quantity > 0:
                known_products.add(product)
            elif quantity < 0:
                all_inputs.add(product)

    missing_input_names = all_inputs - known_products
    inputs = get_api(session, titles='|'.join(missing_input_names))

    for title, missing_input in inputs:
        yield from Recipe.from_ore_page(missing_input)


def fetch_recipes(session: Session, names: List[str]) -> Iterable[Tuple[str, str]]:
    for start in range(0, len(names), MAX_RECIPES):
        name_segment = names[start: start+MAX_RECIPES]
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
            yield name, 1e6 * float(info['powerUsage'])


def get_recipes(tiers: Set[str]) -> Dict[str, Recipe]:
    with Session() as session:
        (_, component_text), = get_api(session, titles='Template:ItemNav')
        component_names = sorted(set(parse_template(component_text, tiers)))
        component_pages = fetch_recipes(session, component_names)

        recipes = list(chain.from_iterable(
            Recipe.from_component_page(page)
            for title, page in component_pages
        ))
        recipes.extend(fill_ores(session, recipes))

        powers = dict(fetch_powers(
            session,
            {recipe.crafted_in for recipe in recipes},
            tiers,
        ))

    above_tier = set()
    for recipe in recipes:
        power = powers.get(recipe.crafted_in)
        if power is None:
            above_tier.add(recipe.name)
        else:
            recipe.base_power = power

    return {
        r.name: r for r in recipes
        if r.name not in above_tier
    }


def load_recipes(tiers: Set[str]) -> Dict[str, Recipe]:
    logger.info(f'Loading recipe database for {len(tiers)} tiers...')

    fn = Path('.recipes')
    if fn.exists():
        with fn.open('rb') as f:
            return pickle.load(f)

    logger.info('Fetching recipe data from MediaWiki...')
    recipes = get_recipes(tiers)
    with fn.open('wb') as f:
        pickle.dump(recipes, f)
    logger.info(f'{len(recipes)} recipes loaded.')
    return recipes
