import pickle
import re
from dataclasses import dataclass
from itertools import count, chain
from pathlib import Path
from typing import ClassVar, Collection, Dict, Iterable, List, Pattern

from requests import Session

from .logs import logger


def get_api(session: Session, **kwargs) -> Iterable[str]:
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

        for page in data['query']['pages'].values():
            revision, = page['revisions']
            yield revision['slots']['main']['*']

        if 'batchcomplete' in data:
            break

        params.update(data['continue'])


def parse_template(content: str, tier_before: int) -> List[str]:
    start = content.index('\n| group2     = Components\n')
    end = content.index(f'[[Tier {tier_before}]]', start)

    return re.findall(
        r'(?<={{ItemLink\|)'
        r'[\w\s]+',
        content[start: end],
    )


@dataclass
class Recipe:
    name: str
    crafted_in: str
    tier: str
    time: float

    rates: Dict[str, float]

    parse_re: ClassVar[Pattern] = re.compile(
        r'{{CraftingTable(.+?)}}', re.I | re.S,
    )

    BASE_POWERS: ClassVar[Dict[str, float]] = {
        'Smelter': 4e6,
        'Constructor': 4e6,
        'Assembler': 15e6,
        'Miner Mk. 1': 5e6,
        'Miner Mk. 2': 12e6,
        'Miner Mk. 3': 30e6,
    }

    @property
    def base_power(self) -> float:
        return self.BASE_POWERS[self.crafted_in]

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
        for match in cls.parse_re.finditer(page):
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
    def from_ore_page(cls, page: str, max_miner: int = 1) -> Iterable['Recipe']:
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
            if mark > max_miner:
                break
            crafted_in = f'{attrs["craftedIn"]} Mk. {mark}'
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


def fill_missing(
        session: Session, recipes: Collection[Recipe]
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

    for missing_input in inputs:
        yield from Recipe.from_ore_page(missing_input)


def get_recipes(tier_before: int) -> Dict[str, Recipe]:
    with Session() as session:
        component_text, = get_api(session, titles='Template:ItemNav')
        component_names = parse_template(component_text, tier_before)
        component_pages = get_api(
            session,
            titles='|'.join(component_names),
            # rvsection=2,  # does not work for Biomass
        )

        recipes = list(chain.from_iterable(
            Recipe.from_component_page(page)
            for page in component_pages
        ))
        recipes.extend(fill_missing(session, recipes))

    return {r.name: r for r in recipes}


def load_recipes(tier_before: int) -> Dict[str, Recipe]:
    logger.info(f'Loading recipe database up to tier {tier_before - 1}...')

    fn = Path('.recipes')
    if fn.exists():
        with fn.open('rb') as f:
            return pickle.load(f)

    logger.info('Fetching recipe data from MediaWiki...')
    recipes = get_recipes(tier_before)
    with fn.open('wb') as f:
        pickle.dump(recipes, f)
    logger.info(f'{len(recipes)} recipes loaded.')
    return recipes
